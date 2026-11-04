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
import re
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
    ads = resp.json().get("Data", [])
    LISTING_COUNTS[name] = len(ads)
    for ad in ads:
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


LISTING_COUNTS = {}  # target name -> live listings seen this run (for the weekly alive mail)

DEADLINE_RES = [
    re.compile(r"s\u00f8knadsfrist:?\s*([^|]*?\d{4})", re.I),   # Jobbnorge: "Søknadsfrist: Fredag 6. november 2026"
    re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b"),                    # ReachMee row: "... Gardermoen 18.11.2026"
]


def find_deadline(row_text):
    for rx in DEADLINE_RES:
        m = rx.search(row_text)
        if m:
            return m.group(1).strip()
    return ""


def fetch_detail_text(link):
    """Lowercased text of one job ad, or None if it can't be read
    (network error, or a JS-only page with almost no text)."""
    try:
        resp = fetch("GET", link, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True).lower()
    return text if len(text) > 300 else None


def check_target(target, seen_keys):
    """Fetch one career page, return list of (text, link, key) for new keyword hits.

    Optional per-target fields in config.json (all backwards compatible):
      href_must_contain   link URL must contain one of these (location filter via URL)
      exclude_keywords    skip hits whose title contains any of these
      page_text_fallback  false = only alert on real job links, never on loose page text
      sanity_href         if no link on the page contains this, count the run as a failure
                          (catches a layout change instead of silently going quiet)
      location_must_contain  title or link must contain one of these (e.g. Gardermoen only)
      context_parent      match on the whole listing row (title + location + category),
                          not just the link text
      detail_trigger      only open the ad when the row has one of these words
                          (e.g. "sommer", "vikar"), so engineer/pilot ads aren't opened
      check_detail        if the title doesn't name the role, open the ad and require a
                          keyword in its body; unreadable ads still alert, flagged
    """
    name = target["name"]
    url = target["url"]
    keywords = [k.lower() for k in target["keywords"]]
    exclude = [k.lower() for k in target.get("exclude_keywords", [])]
    href_must = [s.lower() for s in target.get("href_must_contain", [])]
    use_fallback = target.get("page_text_fallback", True)
    sanity_href = target.get("sanity_href", "").lower()
    location_must = [s.lower() for s in target.get("location_must_contain", [])]
    check_detail = target.get("check_detail", False)
    use_context = target.get("context_parent", False)
    detail_trigger = [k.lower() for k in target.get("detail_trigger", [])]

    resp = fetch("GET", url, headers=HEADERS, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.find_all("a", href=True)

    if sanity_href and not any(sanity_href in a["href"].lower() for a in anchors):
        raise requests.RequestException(
            f"no links containing '{sanity_href}', page layout probably changed"
        )

    hits = []
    run_keys = set()
    any_link_matched = False
    listing_links = set()

    for a in anchors:
        text = " ".join(a.get_text(" ", strip=True).split())
        if len(text) < 4:
            continue
        link = urljoin(url, a["href"])
        if sanity_href and sanity_href in a["href"].lower():
            listing_links.add(link)
        low = text.lower()
        raw_row = text
        if use_context:
            # Job boards print location, role category and deadline in the same
            # row as the link (ISS: "Renholdsmedarbeider Stjørdal"). Match on that.
            row = a.find_parent(["li", "article", "tr"]) or a.parent
            raw_row = " ".join(row.get_text(" ", strip=True).split()) or text
            low = raw_row.lower()
        if href_must and not any(s in link.lower() for s in href_must):
            continue
        if location_must and not any(s in low or s in link.lower() for s in location_must):
            continue
        if exclude and any(e in low for e in exclude):
            continue
        key = f"{name}|{link}"
        if key in seen_keys or key in run_keys:
            if any(k in low for k in keywords):
                any_link_matched = True
            continue
        if any(k in low for k in keywords):
            pass  # role named in the title itself
        elif check_detail and (not detail_trigger or any(k in low for k in detail_trigger)):
            # ISS titles are vague ("Har du lyst på en aktiv arbeidshverdag ..."),
            # so the role is only stated inside the ad. Open it and look there.
            body = fetch_detail_text(link)
            if body is None:
                text += "  [kunne ikke lese annonsen, sjekk rollen selv]"
            elif not any(k in body for k in keywords):
                continue
        else:
            continue
        any_link_matched = True
        # run_keys: same job linked twice on one page (desktop + mobile) -> one alert
        run_keys.add(key)
        deadline = find_deadline(raw_row)
        hits.append((f"{text} · frist {deadline}" if deadline else text, link, key))

    LISTING_COUNTS[name] = len(listing_links)

    # Fallback: keyword appears in page text but not in any link
    # (covers pages that render listings without anchor titles).
    if use_fallback and not any_link_matched:
        page_text = soup.get_text(" ", strip=True).lower()
        for k in keywords:
            if k in page_text:
                key = f"{name}|pagetext|{k}"
                if key not in seen_keys:
                    hits.append(
                        (f"Keyword '{k}' appeared on the page (no direct link found). Open and check.", url, key)
                    )

    return hits


def weekly_heartbeat(cfg, run_report, sent_week, last_counts, force=False):
    """Once a week (first run on or after Monday morning) send a 'radar is alive'
    summary with live listing counts per company. A count that drops to 0 or
    halves since last week is flagged: that is what a quietly broken parser
    looks like. Returns the updated (sent_week, last_counts)."""
    now = datetime.now(timezone.utc)
    iso = now.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    # first run on or after Monday 06 UTC each week: if GitHub skips Monday,
    # the mail just comes Tuesday instead of being lost for the week
    if not force and (sent_week == week or (now.weekday() == 0 and now.hour < 6)):
        return sent_week, last_counts

    lines, warnings, counts = [], [], {}
    for t in run_report:
        n = t.get("listings")
        prev = last_counts.get(t["name"])
        counts[t["name"]] = n
        if not t["ok"]:
            state_txt = f"FEIL ({t['consecutive_failures']} på rad)"
            warnings.append(f"{t['name']}: klarer ikke lese siden")
        else:
            state_txt = f"{n} annonser" if n is not None else "OK"
            if n == 0:
                warnings.append(f"{t['name']}: 0 annonser. Åpne siden selv og sjekk")
            elif prev and n is not None and n < prev / 2:
                warnings.append(f"{t['name']}: falt fra {prev} til {n} annonser siden forrige uke")
        trend = f" (forrige uke {prev})" if prev is not None and prev != n else ""
        lines.append(f"- {t['name']}: {state_txt}{trend}")

    status = "SJEKK DETTE" if warnings else "alt ser normalt ut"
    title = f"Radar lever · uke {iso[1]} · {status}"
    body = "\n".join(lines)
    if warnings:
        body += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
    body += "\n\nIngen handling trengs hvis alt ser normalt ut."
    notify(cfg, title, body, "https://facha003.github.io/job-radar/")
    save_json(BASE / "heartbeat.json", {"title": title, "body": body})
    print(f"Heartbeat sent: {title}")
    return week, counts


def main():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json missing. Create it next to this script first.")

    state = load_json(STATE_PATH, {})
    seen_keys = set(state.get("seen", []))
    fail_counts = state.get("fail", {})
    alerts = state.get("alerts", [])

    if "--test" in sys.argv:
        title = "TEST · Servicemedarbeider (Gardermoen) · varslingen virker"
        link = "https://careers.sasgroup.net/search/?q=&locationsearch=Gardermoen"
        notify(
            cfg,
            "Jobbradar: test",
            "Varsling virker. Ekte varsler ser slik ut, med tittel, sted og frist.",
            link,
        )
        # Same file the workflow turns into an issue, so the test goes through
        # the real email path. seen.json and status.json are left untouched.
        save_json(BASE / "run_alerts.json", [
            {"time": now_iso(), "target": "TEST", "text": title, "link": link}
        ])
        return

    if "--dry-run" in sys.argv:
        # Scrape every target, print what WOULD alert (ignoring seen.json),
        # plus raw job links, so a new target can be checked from GitHub's
        # servers. Sends nothing and writes no state.
        save_json(BASE / "run_alerts.json", [])
        only = os.environ.get("DRY_RUN_TARGET", "").lower()
        for target in cfg.get("targets", []):
            if only and only not in target["name"].lower():
                continue
            print(f"\n===== {target['name']} =====")
            try:
                if target.get("type") == "webcruiter":
                    resp = fetch("POST", f"https://candidate.webcruiter.com/api/odvert/companysearch/{target['silo']}",
                                 headers={**HEADERS, "Accept": "application/json"}, json={"skip": 0, "take": 200}, timeout=30)
                    ads = resp.json().get("Data", [])
                    print(f"HTTP {resp.status_code}, {len(ads)} adverts")
                    for ad in ads:
                        print(f"  AD {ad.get('Heading')} | {ad.get('JobCategory')} | {ad.get('Workplace')} | {webcruiter_deadline(ad)}")
                else:
                    resp = fetch("GET", target["url"], headers=HEADERS, timeout=30)
                    soup = BeautifulSoup(resp.text, "html.parser")
                    print(f"HTTP {resp.status_code}, {len(resp.text)} bytes, title: {soup.title.string if soup.title else '-'}")
                    for a in soup.find_all("a", href=True)[:150]:
                        t = " ".join(a.get_text(" ", strip=True).split())
                        par = a.find_parent(["li", "article", "tr"]) or a.parent
                        ctx = " ".join(par.get_text(" ", strip=True).split())[:160]
                        print(f"  LINK {a['href'][:110]} | {t[:80]} | ctx: {ctx}")
                hits = (check_webcruiter if target.get("type") == "webcruiter" else check_target)(target, set())
                for text, link, _ in hits:
                    print(f"  WOULD ALERT: {text} -> {link}")
                print(f"  {len(hits)} would-alert hit(s), listings counted: {LISTING_COUNTS.get(target['name'])}")
            except Exception as exc:
                print(f"  ERROR: {exc}")
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
            "listings": LISTING_COUNTS.get(name),
        })
        print(f"[{name}] checked OK, {len(hits)} new hit(s)")

    alerts = alerts[:MAX_ALERT_HISTORY]
    heartbeat_week = state.get("heartbeat_week", "")
    last_counts = state.get("last_counts", {})
    heartbeat_week, last_counts = weekly_heartbeat(cfg, run_report, heartbeat_week, last_counts,
                                                     force="--heartbeat" in sys.argv)
    smtp = smtp_conf(cfg)
    channels = {
        # in Actions the issue->email path is always on, even without secrets
        "email": bool(smtp["user"] and smtp["password"]) or os.environ.get("GITHUB_ACTIONS") == "true",
        "push": bool(get_topic(cfg)),
    }
    save_json(STATE_PATH, {"seen": sorted(seen_keys), "fail": fail_counts, "alerts": alerts,
                           "heartbeat_week": heartbeat_week, "last_counts": last_counts})
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
