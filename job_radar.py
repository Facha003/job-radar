#!/usr/bin/env python3
"""
Job Radar for Fardin, versjon 2.
Watches career pages for target roles, pings your phone, and writes
docs/status.json so the dashboard (docs/index.html) can show live state.

Install:    pip install requests beautifulsoup4
Configure:  edit config.json (targets, keywords, ntfy topic)
Run once:   python job_radar.py
Test ping:  python job_radar.py --test
State:      seen.json is created automatically. Delete it to re-alert on everything.
Status:     docs/status.json is rewritten every run. The dashboard reads it.

Designed to run every 30 minutes via GitHub Actions (see radar_check.yml).
Polite by design: one GET request per target per run against public career pages.
Do NOT point this at finn.no (their terms prohibit scraping; use their alerts).
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "seen.json"
STATUS_PATH = BASE / "docs" / "status.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
}

FAIL_WARN_THRESHOLD = 5   # consecutive failures before you get a warning ping
MAX_ALERT_HISTORY = 25    # alerts kept in seen.json and shown on the dashboard
DIGEST_THRESHOLD = 5      # more hits than this in one run -> one summary ping, not a storm


def fetch(method, url, **kwargs):
    """requests wrapper with one retry, so a transient blip does not mark a
    target as failed on the dashboard until the next cron run."""
    last_exc = None
    for attempt in range(2):
        if attempt:
            time.sleep(5)
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            continue
        if resp.status_code >= 500 and attempt == 0:
            continue
        resp.raise_for_status()
        return resp
    raise last_exc


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_topic(cfg):
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", "")
    return "" if "CHANGE" in topic else topic.strip()


def get_email(cfg):
    email = os.environ.get("ALERT_EMAIL") or cfg.get("email", "")
    return email.strip() if "@" in email else ""


def smtp_conf(cfg):
    smtp = cfg.get("smtp", {})
    return {
        "host": os.environ.get("SMTP_HOST") or smtp.get("host") or "smtp.gmail.com",
        "port": int(os.environ.get("SMTP_PORT") or smtp.get("port") or 465),
        "user": os.environ.get("SMTP_USER") or smtp.get("user", ""),
        "password": os.environ.get("SMTP_PASS") or smtp.get("pass", ""),
    }


def send_email(cfg, title, message, url=""):
    """Direct email over SMTP (Gmail: use an app password). Recipient is the
    'email' field in config, defaulting to the SMTP user itself.
    Returns False when SMTP is not configured."""
    conf = smtp_conf(cfg)
    to = get_email(cfg) or conf["user"]
    if not (conf["user"] and conf["password"] and to):
        return False

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = conf["user"]
    msg["To"] = to
    msg.set_content(message + (f"\n\n{url}" if url else ""))

    if conf["port"] == 465:
        server = smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=20)
    else:
        server = smtplib.SMTP(conf["host"], conf["port"], timeout=20)
    with server:
        if conf["port"] != 465:
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()
        server.login(conf["user"], conf["password"])
        server.send_message(msg)
    return True


def notify(cfg, title, message, url=""):
    """Send alerts: ntfy push, SMTP email, Telegram. Env vars override config.
    In GitHub Actions the workflow additionally opens an issue per run with new
    hits (see radar_check.yml) — GitHub emails the repo owner about new issues,
    so email alerts work there with zero secrets configured."""
    sent = False

    topic = get_topic(cfg)
    if topic:
        try:
            requests.post(
                "https://ntfy.sh",
                json={
                    "topic": topic,
                    "title": title,
                    "message": message,
                    "click": url,
                    "priority": 5,
                    "tags": ["rotating_light"],
                },
                timeout=20,
            ).raise_for_status()
            sent = True
        except requests.RequestException as exc:
            print(f"ntfy failed: {exc}")

    try:
        if send_email(cfg, title, message, url):
            sent = True
    except (smtplib.SMTPException, OSError) as exc:
        print(f"email failed: {exc}")

    tg = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": f"{title}\n\n{message}\n{url}"},
                timeout=20,
            ).raise_for_status()
            sent = True
        except requests.RequestException as exc:
            print(f"telegram failed: {exc}")

    if not sent and os.environ.get("GITHUB_ACTIONS") != "true":
        print("WARNING: no notification channel configured. Set smtp/ntfy_topic in config.json.")
    print(f"[ALERT] {title} | {message} | {url}")


def webcruiter_deadline(ad):
    """'frist 07.08' from ApplicationDeadline; Webcruiter uses year 1 for no deadline."""
    try:
        dt = datetime.fromisoformat(ad.get("ApplicationDeadline") or "")
    except ValueError:
        return "frist ukjent"
    if dt.year <= 1970:
        return "frist: snarest"
    return f"frist {dt.strftime('%d.%m')}"


def check_webcruiter(target, seen_keys):
    """Query the Webcruiter candidate API for every advert in a company silo.
    Covers listings the career page only renders with JavaScript."""
    name = target["name"]
    keywords = [k.lower() for k in target["keywords"]]

    resp = fetch(
        "POST",
        f"https://candidate.webcruiter.com/api/odvert/companysearch/{target['silo']}",
        headers={**HEADERS, "Accept": "application/json"},
        json={"skip": 0, "take": 200},
        timeout=30,
    )

    hits = []
    for ad in resp.json().get("Data", []):
        heading = (ad.get("Heading") or "").strip()
        haystack = f"{heading} {ad.get('JobCategory') or ''}".lower()
        if not any(k in haystack for k in keywords):
            continue
        link = ad.get("OpenAdvertUrl") or target["url"]
        key = f"{name}|webcruiter|{ad.get('Id') or link}"
        if key not in seen_keys:
            place = (ad.get("Workplace") or "").strip()
            parts = [heading, f"({place})" if place else "", "·", webcruiter_deadline(ad)]
            hits.append((" ".join(p for p in parts if p), link, key))
    return hits


def check_target(target, seen_keys):
    """Fetch one career page, return list of (text, link, key) for new keyword hits."""
    name = target["name"]
    url = target["url"]
    keywords = [k.lower() for k in target["keywords"]]

    resp = fetch("GET", url, headers=HEADERS, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    hits = []
    any_link_matched = False

    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split())
        if len(text) < 4:
            continue
        low = text.lower()
        if any(k in low for k in keywords):
            any_link_matched = True
            link = urljoin(url, a["href"])
            key = f"{name}|{link}"
            if key not in seen_keys:
                hits.append((text, link, key))

    # Fallback: keyword appears in page text but not in any link
    # (covers pages that render listings without anchor titles).
    if not any_link_matched:
        page_text = soup.get_text(" ", strip=True).lower()
        for k in keywords:
            if k in page_text:
                key = f"{name}|pagetext|{k}"
                if key not in seen_keys:
                    hits.append(
                        (f"Keyword '{k}' appeared on the page (no direct link found). Open and check.", url, key)
                    )

    return hits


def main():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json missing. Create it next to this script first.")

    state = load_json(STATE_PATH, {})
    seen_keys = set(state.get("seen", []))
    fail_counts = state.get("fail", {})
    alerts = state.get("alerts", [])

    if "--test" in sys.argv:
        notify(
            cfg,
            "Jobbradar: test",
            "Varsling virker. Ekte varsler ser slik ut, med tittel, sted og frist.",
            "https://www.vy.no/vygruppen/karriere-i-vy/ledige-stillinger",
        )
        return

    run_report = []
    run_alerts = []  # only this run's hits; the workflow turns them into a GitHub issue

    for target in cfg.get("targets", []):
        name = target["name"]
        try:
            if target.get("type") == "webcruiter":
                hits = check_webcruiter(target, seen_keys)
            else:
                hits = check_target(target, seen_keys)
        except requests.RequestException as exc:
            fail_counts[name] = fail_counts.get(name, 0) + 1
            print(f"[{name}] fetch failed ({fail_counts[name]} in a row): {exc}")
            if fail_counts[name] == FAIL_WARN_THRESHOLD:
                notify(
                    cfg,
                    f"Job Radar: {name} unreachable",
                    f"Failed {FAIL_WARN_THRESHOLD} checks in a row. Site moved or blocked. Check config.json.",
                    target["url"],
                )
            run_report.append({
                "name": name,
                "url": target["url"],
                "ok": False,
                "consecutive_failures": fail_counts[name],
                "new_hits": 0,
                "total_seen": sum(1 for k in seen_keys if k.startswith(name + "|")),
            })
            continue

        fail_counts[name] = 0
        if len(hits) > DIGEST_THRESHOLD:
            preview = "\n".join(f"- {text}" for text, _, _ in hits[:DIGEST_THRESHOLD])
            notify(
                cfg,
                f"POSSIBLE MATCH: {name} ({len(hits)} treff)",
                f"{preview}\n… og {len(hits) - DIGEST_THRESHOLD} til. Åpne siden.",
                target["url"],
            )
        else:
            for text, link, _ in hits:
                notify(cfg, f"POSSIBLE MATCH: {name}", text, link)
        for text, link, key in hits:
            seen_keys.add(key)
            entry = {"time": now_iso(), "target": name, "text": text, "link": link}
            alerts.insert(0, entry)
            run_alerts.append(entry)

        run_report.append({
            "name": name,
            "url": target["url"],
            "ok": True,
            "consecutive_failures": 0,
            "new_hits": len(hits),
            "total_seen": sum(1 for k in seen_keys if k.startswith(name + "|")),
        })
        print(f"[{name}] checked OK, {len(hits)} new hit(s)")

    alerts = alerts[:MAX_ALERT_HISTORY]
    smtp = smtp_conf(cfg)
    channels = {
        # in Actions the issue->email path is always on, even without secrets
        "email": bool(smtp["user"] and smtp["password"]) or os.environ.get("GITHUB_ACTIONS") == "true",
        "push": bool(get_topic(cfg)),
    }
    save_json(STATE_PATH, {"seen": sorted(seen_keys), "fail": fail_counts, "alerts": alerts})
    save_json(BASE / "run_alerts.json", run_alerts)
    save_json(STATUS_PATH, {
        "generated": now_iso(),
        "channels": channels,
        "targets": run_report,
        "recent_alerts": alerts,
    })
    print(f"Done. {len(run_alerts)} new alert(s) this run. Status written to docs/status.json")


if __name__ == "__main__":
    main()
