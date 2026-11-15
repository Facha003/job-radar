#!/usr/bin/env python3
"""
Job Radar, versjon 3.
Watches career pages for target roles, alerts you, and writes docs/status.json
so the dashboard (docs/index.html) can show live state.

Install:     pip install -r requirements.txt
Configure:   edit config.json (targets, keywords)
Preview:     python job_radar.py            (local: reads everything, sends and saves nothing)
Debug:       python job_radar.py --dry-run  (same, plus every listing each source returned)
Real run:    python job_radar.py --write    (what GitHub Actions does: alert + save state)
Test alert:  python job_radar.py --test
State:       seen.json is created automatically. Delete it to re-alert on everything.
Status:      docs/status.json is rewritten every real run. The dashboard reads it.
Jobs:        docs/jobs.json remembers every wanted posting: first seen, deadline,
             open or closed. The dashboard's job list and history read it.

Designed to run on a schedule in GitHub Actions (see .github/workflows/radar_check.yml).
Polite by design: one request per target per run against public career pages and feeds.
Do NOT point this at finn.no (their terms prohibit scraping; use their alerts).

The kinds of source (the "type" field of a target in config.json):
  (none)           an HTML career page, read with BeautifulSoup
  webcruiter       the Webcruiter candidate JSON API (Vy, Avinor)
  teamtailor       a Teamtailor career site's RSS feed (Aviator)
  nav              NAV's official job vacancy feed: every publicly advertised
                   job in Norway, filtered down to your municipality and roles
  successfactors   the public SAP SuccessFactors job feed (Norwegian). Their own
                   career site sits behind a Cloudflare challenge that GitHub's
                   servers never pass; the feed is the same data, hosted by SAP.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "seen.json"
STATUS_PATH = BASE / "docs" / "status.json"
JOBS_PATH = BASE / "docs" / "jobs.json"
CALENDAR_PATH = BASE / "docs" / "frister.ics"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
}

FAIL_WARN_THRESHOLD = 5   # consecutive failures before you get a warning
MAX_ALERT_HISTORY = 25    # alerts kept in seen.json and shown on the dashboard
DIGEST_THRESHOLD = 5      # more hits than this in one run -> one summary ping, not a storm
REMIND_DAYS = (3, 1)       # remind this many days before a deadline, once each
MAX_JOB_HISTORY = 300     # postings remembered in docs/jobs.json (oldest closed ones go first)
MAX_DETAIL_READS = 6      # job ads opened per run to read deadline, percent and requirements
CLOSE_AFTER_MISSES = 2    # checks in a row a posting must be gone before it counts as closed
MAX_EVENTS = 150          # lines kept in the dashboard's log (status.json "events")
MAX_RUN_LOG = 200         # run times kept in status.json, so the dashboard can show real pace
DEFAULT_DASHBOARD = "https://facha003.github.io/job-radar/"

UNREADABLE_TAG = "  [kunne ikke lese annonsen, sjekk rollen selv]"
LOCATION_TAG = "  [sted ikke bekreftet, sjekk selv]"


class SourceError(Exception):
    """A source answered, but not with what we expect (layout or feed changed)."""


class Hit(NamedTuple):
    text: str
    link: str
    key: str


@dataclass
class CheckResult:
    hits: list            # new postings (not in seen.json) that should alert
    open: list            # every wanted posting live right now, see open_item()
    listings: int | None  # how many listings the source showed (parser health)


Log = Callable[[str], None]


def _quiet(_line: str) -> None:
    pass


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def fetch(method, url, **kwargs):
    """requests wrapper with one retry, so a transient blip does not mark a
    target as failed on the dashboard until the next scheduled run."""
    last_exc = None
    for attempt in range(2):
        if attempt:
            time.sleep(5)
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException as exc:
            last_exc = exc
            continue
        if (resp.status_code >= 500 or resp.status_code == 429) and attempt == 0:
            continue
        resp.raise_for_status()
        return resp
    raise last_exc


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat(timespec="seconds")


def today_oslo():
    """Deadlines are Norwegian calendar dates, so 'days left' is counted in Oslo."""
    try:
        from zoneinfo import ZoneInfo
        return now_utc().astimezone(ZoneInfo("Europe/Oslo")).date()
    except Exception:  # noqa: BLE001 - no tz database on this machine: UTC is at most 2 h off
        return now_utc().date()


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    """Write via a temp file so a crash mid-write can never leave half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def squash(text):
    return " ".join((text or "").split())


def lower_list(target, field):
    return [str(s).lower() for s in target.get(field, [])]


def short_error(exc):
    """One readable line for the log, the dashboard and the warning mail."""
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None):
        reason = getattr(resp, "reason", "") or ""
        return f"HTTP {resp.status_code} {reason}".strip()
    if isinstance(exc, requests.Timeout):
        return "tidsavbrudd (siden svarte ikke)"
    if isinstance(exc, requests.ConnectionError):
        return "fikk ikke kontakt med siden"
    text = squash(str(exc)) or type(exc).__name__
    if not isinstance(exc, (SourceError, requests.RequestException)):
        text = f"{type(exc).__name__}: {text}"
    return text[:200]


def dashboard_url(cfg):
    if cfg.get("dashboard_url"):
        return cfg["dashboard_url"]
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return DEFAULT_DASHBOARD


# --------------------------------------------------------------------------
# notification channels
# --------------------------------------------------------------------------

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


def telegram_conf(cfg):
    tg = cfg.get("telegram", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or tg.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")
    return (token, chat_id) if token and chat_id else None


def issue_conf():
    """(token, 'owner/repo') when running somewhere that can open GitHub issues.
    In GitHub Actions both come for free, so email works with zero secrets:
    GitHub mails the repo owner about every new issue."""
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return (token, repo) if token and "/" in repo else None


def smtp_ready(cfg):
    conf = smtp_conf(cfg)
    return bool(conf["user"] and conf["password"])


def email_mode(cfg):
    """'smtp' if you configured your own mail, else 'issue' when GitHub can
    mail you, else ''. Never both, so you never get double emails."""
    if smtp_ready(cfg):
        return "smtp"
    return "issue" if issue_conf() else ""


def any_channel(cfg):
    return bool(get_topic(cfg) or email_mode(cfg) or telegram_conf(cfg))


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


def create_issue(title, body, close=False):
    """Open a GitHub issue in this repo (GitHub then emails the owner).
    close=True closes it straight away: the mail still goes out, but
    housekeeping notices do not pile up as open issues.
    Returns the issue number, or None when it could not be created. Never raises."""
    api = issue_api()
    if not api:
        return None
    url, headers, owner = api
    try:
        resp = requests.post(url, headers=headers, json={"title": title[:240], "body": f"@{owner}\n{body}"}, timeout=30)
        resp.raise_for_status()
        number = int(resp.json()["number"])
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        print(f"::error::GitHub issue failed: {short_error(exc)}")
        return None
    if close:
        try:
            requests.patch(f"{url}/{number}", headers=headers, json={"state": "closed"}, timeout=30).raise_for_status()
        except requests.RequestException as exc:
            print(f"could not close the issue (the mail was still sent): {short_error(exc)}")
    return number


def issue_api():
    """(issues URL, headers, repo owner) or None when issues are not available."""
    conf = issue_conf()
    if not conf:
        return None
    token, repo = conf
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return f"{api}/repos/{repo}/issues", headers, repo.split("/", 1)[0]


def issue_is_closed(number):
    """True only when GitHub says the issue is closed. If we cannot tell,
    treat it as open: an extra reminder beats a missed deadline."""
    api = issue_api()
    if not api or not number:
        return False
    url, headers, _owner = api
    try:
        resp = requests.get(f"{url}/{number}", headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json().get("state") == "closed"
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"could not read issue #{number}: {short_error(exc)}")
        return False


def comment_issue(number, body):
    """Comment on an issue (GitHub emails the owner). True when posted. Never raises."""
    api = issue_api()
    if not api or not number:
        return False
    url, headers, owner = api
    try:
        requests.post(f"{url}/{number}/comments", headers=headers, json={"body": f"@{owner} {body}"}, timeout=30).raise_for_status()
    except requests.RequestException as exc:
        print(f"::error::GitHub comment on #{number} failed: {short_error(exc)}")
        return False
    return True


PUSH_HIT = (5, "rotating_light")       # a job you want: loudest
PUSH_REMINDER = (4, "alarm_clock")     # a deadline is close
PUSH_NOTICE = (3, "satellite")         # housekeeping: normal volume


def send_direct(cfg, title, message, url="", push=PUSH_HIT):
    """Push the alert through every channel you configured yourself:
    ntfy push, SMTP email, Telegram. Env vars override config.
    push is (ntfy priority, tag). Only job hits ring at full volume.
    Returns True when at least one of them delivered. Never raises."""
    sent = False

    topic = get_topic(cfg)
    if topic:
        try:
            payload = {
                "topic": topic,
                "title": title,
                "message": message,
                "priority": push[0],
                "tags": [push[1]],
            }
            if url:
                payload["click"] = url
            requests.post("https://ntfy.sh", json=payload, timeout=20).raise_for_status()
            sent = True
        except requests.RequestException as exc:
            print(f"ntfy failed: {short_error(exc)}")

    try:
        if send_email(cfg, title, message, url):
            sent = True
    except (smtplib.SMTPException, OSError) as exc:
        print(f"email failed: {exc}")

    tg = telegram_conf(cfg)
    if tg:
        try:
            requests.post(
                f"https://api.telegram.org/bot{tg[0]}/sendMessage",
                json={"chat_id": tg[1], "text": f"{title}\n\n{message}\n{url}"},
                timeout=20,
            ).raise_for_status()
            sent = True
        except requests.RequestException as exc:
            print(f"telegram failed: {short_error(exc)}")

    print(f"[ALERT] {title} | {message} | {url}")
    return sent


def send_notice(cfg, title, message, url=""):
    """Housekeeping mail (site unreachable, site back, weekly 'alive', test):
    every direct channel, plus a GitHub issue that is closed straight away
    when that is the email route. Returns True when something delivered."""
    sent = send_direct(cfg, title, message, url, push=PUSH_NOTICE)
    if email_mode(cfg) == "issue":
        body = message + (f"\n\n{url}" if url else "")
        if create_issue(title, body, close=True):
            sent = True
    return sent


def md_link(text, link):
    safe = link.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{text}]({safe})"


def send_hits_issue(entries, close=False):
    """One issue (= one email) for these hits. Returns its number or None.
    A normal run opens one issue per job, so each job has its own thread that
    you close when you are done with it (which also stops deadline reminders).
    A flood above the digest threshold goes in a single issue."""
    title = entries[0]["text"]
    if len(entries) > 1:
        title = f"{title} (+{len(entries) - 1} til)"
    lines = [f"- {md_link(e['text'], e['link'])} — {e['target']}" for e in entries]
    stamp = now_utc().strftime("%d.%m.%Y %H:%M")
    body = ("\n".join(lines) + f"\n\nRadaren fant dette {stamp} UTC. "
            "Lukk denne saken når du er ferdig med stillingen, da slutter også påminnelsene om frist.")
    return create_issue(f"🚨 MULIG TREFF: {title}", body, close=close)


def send_hits_direct(cfg, name, hits, target_url):
    """Per-hit pings on the direct channels (one summary above the digest
    threshold). Returns True only when every ping was delivered."""
    if len(hits) > DIGEST_THRESHOLD:
        preview = "\n".join(f"- {h.text}" for h in hits[:DIGEST_THRESHOLD])
        return send_direct(
            cfg,
            f"POSSIBLE MATCH: {name} ({len(hits)} treff)",
            f"{preview}\n… og {len(hits) - DIGEST_THRESHOLD} til. Åpne siden.",
            target_url,
        )
    delivered = True
    for hit in hits:
        if not send_direct(cfg, f"POSSIBLE MATCH: {name}", hit.text, hit.link):
            delivered = False
    return delivered


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

DEADLINE_RES = [
    re.compile(r"søknadsfrist:?\s*([^|]*?\d{4})", re.I),   # Jobbnorge: "Søknadsfrist: Fredag 6. november 2026"
    re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b"),                    # ReachMee row: "... Gardermoen 18.11.2026"
]


def find_deadline(row_text):
    for rx in DEADLINE_RES:
        m = rx.search(row_text)
        if m:
            return m.group(1).strip()
    return ""


MONTHS = {
    "januar": 1, "februar": 2, "mars": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
    "august": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
}
DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})")
DATE_DOTS_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
DATE_WORDS_RE = re.compile(r"\b(\d{1,2})\.?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", re.I)


def parse_deadline(text):
    """A deadline as written on a career page -> 'YYYY-MM-DD', or None when
    there is no real date in it ("snarest", empty, nonsense)."""
    text = text or ""
    try:
        m = DATE_ISO_RE.search(text)
        if m:
            found = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        else:
            m = DATE_DOTS_RE.search(text)
            if m:
                found = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            else:
                m = DATE_WORDS_RE.search(text)
                if not m:
                    return None
                found = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    except ValueError:  # 31.02.2026 and friends
        return None
    return found.isoformat() if found.year > 1970 else None


def open_item(key, title, link, label=None, deadline=None):
    """One wanted posting that is live right now.
    text      what alerts and the dashboard show ("Title · frist 27.11.2026")
    title     the same without the deadline part
    deadline  'YYYY-MM-DD' when the source states a real date, else None"""
    return {"key": key, "text": label or title, "title": title, "link": link, "deadline": deadline}


def fetch_detail_text(link):
    """Lowercased text of one job ad, or None if it can't be read
    (network error, or a JS-only page with almost no text)."""
    try:
        resp = fetch("GET", link, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True).lower()
    return text if len(text) > 300 else None


def check_html(target, seen_keys, log: Log = _quiet):
    """Fetch one HTML career page and filter its job links.

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
    keywords = lower_list(target, "keywords")
    exclude = lower_list(target, "exclude_keywords")
    href_must = lower_list(target, "href_must_contain")
    use_fallback = target.get("page_text_fallback", True)
    sanity_href = target.get("sanity_href", "").lower()
    location_must = lower_list(target, "location_must_contain")
    check_detail = target.get("check_detail", False)
    use_context = target.get("context_parent", False)
    detail_trigger = lower_list(target, "detail_trigger")

    resp = fetch("GET", url, headers=HEADERS, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    anchors = soup.find_all("a", href=True)
    page_title = squash(soup.title.get_text()) if soup.title else "-"
    log(f"HTTP {resp.status_code}, {len(resp.text)} bytes, title: {page_title}")

    if sanity_href and not any(sanity_href in a["href"].lower() for a in anchors):
        raise SourceError(f"no links containing '{sanity_href}', page layout probably changed")

    hits = []
    open_matches = []
    run_keys = set()
    any_link_matched = False
    listing_links = set()

    for index, a in enumerate(anchors):
        text = squash(a.get_text(" ", strip=True))
        row = a.find_parent(["li", "article", "tr"]) or a.parent
        row_text = squash(row.get_text(" ", strip=True)) if row is not None else ""
        if index < 150:
            log(f"  LINK {a['href'][:110]} | {text[:80]} | ctx: {row_text[:160]}")
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
            raw_row = row_text or text
            low = raw_row.lower()
        if href_must and not any(s in link.lower() for s in href_must):
            continue
        if location_must and not any(s in low or s in link.lower() for s in location_must):
            continue
        if exclude and any(e in low for e in exclude):
            continue
        key = f"{name}|{link}"
        if key in run_keys:
            continue  # same job linked twice on one page (desktop + mobile, "Se stillingen")
        if any(k in low for k in keywords):
            pass  # role named in the title itself
        elif check_detail and (not detail_trigger or any(k in low for k in detail_trigger)):
            # ISS titles are vague ("Har du lyst på en aktiv arbeidshverdag ..."),
            # so the role is only stated inside the ad. Open it and look there.
            body = fetch_detail_text(link)
            if body is None:
                text += UNREADABLE_TAG
            elif not any(k in body for k in keywords):
                continue
        else:
            continue
        any_link_matched = True
        run_keys.add(key)
        deadline = find_deadline(raw_row)
        label = f"{text} · frist {deadline}" if deadline else text
        open_matches.append(open_item(key, text, link, label, parse_deadline(deadline)))
        if key not in seen_keys:
            hits.append(Hit(label, link, key))

    # Fallback: keyword appears in page text but not in any link
    # (covers pages that render listings without anchor titles).
    if use_fallback and not any_link_matched:
        page_text = soup.get_text(" ", strip=True).lower()
        for k in keywords:
            if k in page_text:
                key = f"{name}|pagetext|{k}"
                if key not in seen_keys:
                    hits.append(Hit(
                        f"Keyword '{k}' appeared on the page (no direct link found). Open and check.",
                        url, key,
                    ))

    # without a sanity_href there is no way to tell job links from menu links
    listings = len(listing_links) if sanity_href else None
    return CheckResult(hits, open_matches, listings)


def webcruiter_deadline(ad):
    """'frist 07.08' from ApplicationDeadline; Webcruiter uses year 1 for no deadline."""
    try:
        dt = datetime.fromisoformat(ad.get("ApplicationDeadline") or "")
    except (ValueError, TypeError):
        return "frist ukjent"
    if dt.year <= 1970:
        return "frist: snarest"
    return f"frist {dt.strftime('%d.%m')}"


def check_webcruiter(target, seen_keys, log: Log = _quiet):
    """Query the Webcruiter candidate API for every advert in a company silo.
    Covers listings the career page only renders with JavaScript."""
    name = target["name"]
    keywords = lower_list(target, "keywords")
    exclude = lower_list(target, "exclude_keywords")
    location_must = lower_list(target, "location_must_contain")

    resp = fetch(
        "POST",
        f"https://candidate.webcruiter.com/api/odvert/companysearch/{target['silo']}",
        headers={**HEADERS, "Accept": "application/json"},
        json={"skip": 0, "take": 200},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError as exc:
        raise SourceError("Webcruiter did not answer with JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("Data", []), list):
        raise SourceError("Webcruiter answer has no 'Data' list, API probably changed")
    ads = [ad for ad in data.get("Data", []) if isinstance(ad, dict)]
    log(f"HTTP {resp.status_code}, {len(ads)} adverts")

    hits = []
    open_matches = []
    for ad in ads:
        heading = (ad.get("Heading") or "").strip()
        log(f"  AD {heading} | {ad.get('JobCategory')} | {ad.get('Workplace')} | {webcruiter_deadline(ad)}")
        haystack = f"{heading} {ad.get('JobCategory') or ''}".lower()
        if not any(k in haystack for k in keywords):
            continue
        place = (ad.get("Workplace") or "").strip()
        if exclude and any(e in haystack for e in exclude):
            continue
        if location_must and not any(s in f"{haystack} {place.lower()}" for s in location_must):
            continue
        link = ad.get("OpenAdvertUrl") or target["url"]
        key = f"{name}|webcruiter|{ad.get('Id') or link}"
        parts = [heading, f"({place})" if place else "", "·", webcruiter_deadline(ad)]
        label = " ".join(p for p in parts if p)
        title = f"{heading} ({place})" if place else heading
        open_matches.append(open_item(key, title, link, label, parse_deadline(ad.get("ApplicationDeadline") or "")))
        if key not in seen_keys:
            hits.append(Hit(label, link, key))
    return CheckResult(hits, open_matches, len(ads))


SF_JOB_RE = re.compile(r"<Job>(.*?)</Job>", re.S)
SF_DESC_RE = re.compile(r"<Job-Description>.*?</Job-Description>", re.S)
SF_CDATA_RE = re.compile(r"<!\[CDATA\[(.*)\]\]>", re.S)



# --------------------------------------------------------------------------
# reading one job ad: deadline, percent, contract, requirements
# --------------------------------------------------------------------------

def fetch_ad_text(link):
    """Readable text of one job ad (lowercased, scripts and styles removed),
    or None when it cannot be read."""
    try:
        resp = fetch("GET", link, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    for junk in soup(["script", "style", "noscript", "template"]):
        junk.decompose()
    text = squash(soup.get_text(" ", strip=True)).lower()
    return text if len(text) > 300 else None


# a date only counts as the deadline when one of these labels stands right before it
FACT_DEADLINE_RE = re.compile(
    r"(?:application deadline|deadline for applications?|søknadsfrist|ansøgningsfrist|frist for (?:å søke|søknad)"
    r"|apply by|apply before|closing date|\bfrist)\s*[:\-–]?\s*(?=(.{0,45}))")
# ... and the date has to be the very next thing (a weekday in front is fine), so
# "Søknadsfrist: snarest. Oppstart 01.02.2027" does not turn the start date into a deadline
FACT_DATE_NEXT_RE = re.compile(
    r"(?:(?:man|tirs|ons|tors|fre|lør|søn)dag|(?:mon|tues|wednes|thurs|fri|satur|sun)day)?,?\s*(?:den\s+)?"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}\.?\s+(?:" + "|".join(MONTHS) + r")\s+\d{4})")
FACT_PERCENT_RANGE_RE = re.compile(
    r"\b(\d{2,3})\s?(?:-|–|til|to)\s?(\d{2,3})\s?%\s*(?:stilling|fast|permanent|contract|position|vikar|deltid|part)")
FACT_PERCENT_RES = [
    re.compile(r"\b(\d{2,3})\s?%\s*(?:stilling|fast|permanent|contract|position|vikar|deltid|part|midlertidig|temporary|fte)"),
    re.compile(r"(?:stillingsprosent|stillingsstørrelse|stillingsbrøk|omfang|percentage of full[- ]time)\s*:?\s*(\d{2,3})\s?%?"),
    re.compile(r"(?:stilling(?:en|er)? (?:på|i)|contracts? of|position of)\s*(\d{2,3})\s?%"),
]
FACT_FULLTIME_RE = re.compile(r"\b(?:heltid|fulltid|full[- ]time)\b")
FACT_PARTTIME_RE = re.compile(r"\b(?:deltid|part[- ]time)\b")
FACT_CONTRACT_LABEL_RE = re.compile(
    r"(?:type of contract|contract type|employment type|ansettelsesform|stillingstype|ansettelsestype)\s*:?\s*([^.:]{0,40})")
FACT_CONTRACTS = [   # (what the dashboard shows, words that mean it)
    ("TILKALLING", re.compile(r"\b(?:tilkalling\w*|ekstrahjelp\w*|on[- ]call)\b")),
    ("SESONG", re.compile(r"\b(?:sesong\w*|sommerjobb\w*|sommervikar\w*|seasonal|summer (?:job|season|temp)\w*)\b")),
    ("VIKARIAT", re.compile(r"\b(?:vikariat\w*|vikar|midlertidig\w*|temporary|engasjement\w*|fixed[- ]term)\b")),
    ("FAST", re.compile(r"\b(?:fast(?:e)? (?:stilling\w*|ansettelse\w*|ansatt\w*)|permanent\w*)\b")),
]
_LANG_CUE = r"(?:behersk\w*|snakk\w*|flytende|språk\w*|muntlig|skriftlig|fluent|proficien\w*|speak\w*|written|verbal|oral|language\w*|skills)"
FACT_TAGS = [
    ("NORSK", re.compile(rf"{_LANG_CUE}[^.]{{0,40}}\b(?:norsk|skandinavisk|scandinavian|norwegian language)"
                         r"|\b(?:norsk|skandinavisk|scandinavian|scaninavian)\w*[^.]{0,30}(?:muntlig|skriftlig|språk|language|speaking|speaker)"
                         r"|norwegian (?:language|speaking|speaker)")),
    ("ENGELSK", re.compile(rf"{_LANG_CUE}[^.]{{0,40}}\b(?:engelsk|english)"
                           r"|\b(?:engelsk|english)\w*[^.]{0,30}(?:muntlig|skriftlig|språk|language|speaking|skills)")),
    ("FØRERKORT", re.compile(r"førerkort|driver['’]?s licen[cs]e|driving licen[cs]e")),
    ("BAKGRUNNSSJEKK", re.compile(r"bakgrunnssjekk|vandel|politiattest|background (?:check|documentation)|security clearance|criminal record")),
    ("SKIFT", re.compile(r"\b(?:skift\w*|turnus\w*|shifts?|shift work|rotating shift\w*)\b")),
    ("18 ÅR", re.compile(r"\b(?:fylt 18|18 år|18 years|minimum 18|at least 18)\b")),
]


def extract_facts(text):
    """What one job ad says, as short facts for the dashboard.

    deadline  'YYYY-MM-DD', only from a date that follows a deadline label
    percent   "75 %", "50–100 %", "100 %" (full time) or "DELTID"
    contract  FAST / VIKARIAT / SESONG / TILKALLING
    tags      requirements the ad mentions (language, licence, background check …)

    Nothing personal is kept: no names, phone numbers or addresses."""
    text = squash(text).lower()
    facts = {"deadline": None, "percent": "", "contract": "", "tags": []}
    for m in FACT_DEADLINE_RE.finditer(text):
        date_next = FACT_DATE_NEXT_RE.match(m.group(1))
        found = parse_deadline(date_next.group(1)) if date_next else None
        if found:
            facts["deadline"] = found
            break

    def sane(*numbers):
        return all(10 <= int(n) <= 100 for n in numbers)

    m = FACT_PERCENT_RANGE_RE.search(text)
    if m and sane(m.group(1), m.group(2)) and int(m.group(1)) < int(m.group(2)):
        facts["percent"] = f"{int(m.group(1))}–{int(m.group(2))} %"
    else:
        for rx in FACT_PERCENT_RES:
            m = rx.search(text)
            if m and sane(m.group(1)):
                facts["percent"] = f"{int(m.group(1))} %"
                break
        else:
            full, part = FACT_FULLTIME_RE.search(text), FACT_PARTTIME_RE.search(text)
            if full and not part:
                facts["percent"] = "100 %"
            elif part and not full:
                facts["percent"] = "DELTID"

    labelled = FACT_CONTRACT_LABEL_RE.search(text)
    if labelled:
        facts["contract"] = next((name for name, rx in FACT_CONTRACTS if rx.search(labelled.group(1))), "")
    if not facts["contract"]:   # no label: the kind that is named first in the ad
        first = [(rx.search(text).start(), name) for name, rx in FACT_CONTRACTS if rx.search(text)]
        facts["contract"] = min(first)[1] if first else ""
    facts["tags"] = [name for name, rx in FACT_TAGS if rx.search(text)]
    return facts


def facts_line(deadline, details):
    """'frist 20.11.2026 · 75 % · fast' for an alert; '' when nothing is known."""
    parts = []
    if deadline:
        parts.append("frist " + date.fromisoformat(deadline).strftime("%d.%m.%Y"))
    parts += [v for v in ((details or {}).get("percent"), ((details or {}).get("contract") or "").lower()) if v]
    return " · ".join(parts)


def read_details(result, prev_jobs, today, budget, list_url=""):
    """Open the ad of each wanted posting that has not been read yet, and put
    what it says on the open item (and on the alert text of a new hit).

    A posting is read when it is new, has no details, or still has no
    deadline; one with nothing found is tried again at most once a day.
    A posting whose link is just the list page (list_url) has no ad of its
    own to read: the page would hand it some other job's deadline.
    Returns how many ads were opened. A read that fails changes nothing."""
    def page(link):
        return (link or "").split("#")[0].rstrip("/")

    known = {j.get("key"): j for j in prev_jobs if isinstance(j, dict)}
    stamp = today.isoformat()
    used = 0
    for item in result.open:
        old = known.get(item.get("key")) or {}
        has_deadline = item.get("deadline") or old.get("deadline")
        if item.get("details"):
            continue   # the source handed the ad over itself (NAV)
        if old.get("details") and has_deadline:
            continue
        if old.get("details_tried") == stamp:
            item["details_tried"] = stamp
            continue
        link = item.get("detail_link") or item.get("link") or ""
        if not page(link) or page(link) == page(list_url):
            continue
        if used >= budget:
            break
        used += 1
        item["details_tried"] = stamp
        text = fetch_ad_text(link)
        if not text:
            continue
        facts = extract_facts(text)
        item["details"] = {"percent": facts["percent"], "contract": facts["contract"], "tags": facts["tags"], "read": stamp}
        found_here = None
        if not has_deadline and facts["deadline"]:
            found_here = item["deadline"] = facts["deadline"]
        extra = facts_line(found_here, item["details"])
        if extra:
            result.hits = [h._replace(text=f"{h.text} · {extra}") if h.key == item.get("key") else h for h in result.hits]
    return used


def sf_field(block, tag):
    m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
    if not m:
        return ""
    value = m.group(1).strip()
    cdata = SF_CDATA_RE.fullmatch(value)
    return (cdata.group(1) if cdata else value).strip()


def parse_successfactors(feed_text):
    """Jobs from a SuccessFactors 'job_listing_summary' XML feed.
    Read with regexes on purpose: the feed is not well-formed XML
    (it contains tags with empty names), so a real XML parser rejects it."""
    if "<Job-Listing" not in feed_text:
        raise SourceError("no <Job-Listing> in the feed, feed address or format probably changed")
    jobs = []
    for block in SF_JOB_RE.findall(feed_text):
        title = squash(html.unescape(re.sub(r"<[^>]+>", " ", sf_field(block, "JobTitle"))))
        if not title:
            continue
        rest = SF_DESC_RE.sub("", block)
        categories = [
            squash(html.unescape(SF_CDATA_RE.sub(r"\1", v)))
            for v in re.findall(r"<value>(.*?)</value>", rest, re.S)
        ]
        body = BeautifulSoup(sf_field(block, "Job-Description"), "html.parser").get_text(" ", strip=True)
        jobs.append({
            "id": sf_field(rest, "ReqId"),
            "title": title,
            "category": " ".join(c for c in categories if c),
            "body": squash(body).lower(),
        })
    return jobs


def check_successfactors(target, seen_keys, log: Log = _quiet):
    """Read the public SuccessFactors job feed (every open job in the company).

    Fields in config.json:
      feed       feed address (…career?company=X&career_ns=job_listing_summary&resultType=XML)
      job_link   link to one job, "{id}" is replaced by the requisition id
      detail_link  where the radar itself reads one ad (deadline, percent …),
                 when job_link is behind a bot wall
      keywords / exclude_keywords / check_detail / detail_trigger
                 as for HTML targets, matched on title + field of work; the ad
                 body comes with the feed, so check_detail costs no extra request
      location_must_contain  one of these must be in the title or the ad body
      location_elsewhere     the feed has no location field. A role match that
                 never mentions your location still alerts, flagged, unless its
                 TITLE names one of these other places
    """
    name = target["name"]
    keywords = lower_list(target, "keywords")
    exclude = lower_list(target, "exclude_keywords")
    location_must = lower_list(target, "location_must_contain")
    elsewhere = lower_list(target, "location_elsewhere")
    check_detail = target.get("check_detail", False)
    detail_trigger = lower_list(target, "detail_trigger")

    resp = fetch("GET", target["feed"], headers=HEADERS, timeout=40)
    # the feed is served without a charset; it is UTF-8
    feed_text = resp.content.decode("utf-8", errors="replace")
    if "<Job-Listing" not in feed_text and "maintenance" in (str(getattr(resp, "url", "")) + feed_text[:3000]).lower():
        # SAP takes the whole service down for release upgrades (seen 13.11.2026, about 15 hours)
        raise SourceError("SAP SuccessFactors er nede for planlagt vedlikehold, feeden kommer tilbake av seg selv")
    jobs = parse_successfactors(feed_text)
    log(f"HTTP {resp.status_code}, {len(resp.content)} bytes, {len(jobs)} jobs in feed")

    hits = []
    open_matches = []
    run_keys = set()
    for job in jobs:
        title = job["title"]
        log(f"  JOB {job['id'] or '-'} | {title} | {job['category']}")
        title_low = title.lower()
        low = f"{title} {job['category']}".lower()
        body = job["body"]
        label = title
        if exclude and any(e in low for e in exclude):
            continue
        if any(k in low for k in keywords):
            pass  # role named in the title itself
        elif check_detail and (not detail_trigger or any(k in low for k in detail_trigger)):
            if not body:
                label += UNREADABLE_TAG
            elif not any(k in body for k in keywords):
                continue
        else:
            continue
        if location_must and not any(s in low or s in body for s in location_must):
            if any(s in title_low for s in elsewhere):
                continue  # the title itself says the job is somewhere else
            label += LOCATION_TAG
        ident = job["id"] or title_low
        key = f"{name}|successfactors|{ident}"
        if key in run_keys:
            continue
        run_keys.add(key)
        link = target["job_link"].replace("{id}", job["id"]) if job["id"] and target.get("job_link") else target["url"]
        item = open_item(key, label, link)  # the feed states no deadline; the ad itself does
        if job["id"] and target.get("detail_link"):
            item["detail_link"] = target["detail_link"].replace("{id}", job["id"])
        open_matches.append(item)
        if key not in seen_keys:
            hits.append(Hit(label, link, key))
    return CheckResult(hits, open_matches, len(jobs))


RSS_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S)


def rss_text(block, tag):
    """Text of every <tag> in an RSS block, CDATA and entities resolved."""
    out = []
    for raw in re.findall(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", block, re.S):
        cdata = SF_CDATA_RE.fullmatch(raw.strip())
        out.append(squash(html.unescape(cdata.group(1) if cdata else raw)))
    return [t for t in out if t]


def check_teamtailor(target, seen_keys, log: Log = _quiet):
    """Read a Teamtailor career site's RSS feed (…/jobs.rss), which carries a
    structured location (city, country) for every job.

    Fields in config.json: feed, keywords, exclude_keywords, check_detail,
    detail_trigger as for the other sources (matched on title + department,
    the ad body comes with the feed); location_must_contain is matched on the
    job's city, country and office name, and on the title."""
    name = target["name"]
    keywords = lower_list(target, "keywords")
    exclude = lower_list(target, "exclude_keywords")
    location_must = lower_list(target, "location_must_contain")
    check_detail = target.get("check_detail", False)
    detail_trigger = lower_list(target, "detail_trigger")

    resp = fetch("GET", target["feed"], headers=HEADERS, timeout=30)
    feed_text = resp.content.decode("utf-8", errors="replace")
    if "<rss" not in feed_text[:2000] and "<channel" not in feed_text[:2000]:
        raise SourceError("the feed is not RSS any more, feed address probably changed")
    items = RSS_ITEM_RE.findall(feed_text)
    log(f"HTTP {resp.status_code}, {len(resp.content)} bytes, {len(items)} jobs in feed")

    hits, open_matches, run_keys = [], [], set()
    for item in items:
        rest = re.sub(r"<description>.*?</description>", "", item, flags=re.S)
        title = (rss_text(rest, "title") or [""])[0]
        link = (rss_text(rest, "link") or [target["url"]])[0]
        guid = (rss_text(rest, "guid") or [link])[0]
        places = rss_text(rest, "tt:city") + rss_text(rest, "tt:country") + rss_text(rest, "tt:name")
        department = " ".join(rss_text(rest, "tt:department"))
        log(f"  JOB {title} | {department} | {', '.join(places)}")
        if not title:
            continue
        low = f"{title} {department}".lower()
        where = " ".join(places).lower()
        label = title
        if exclude and any(e in low for e in exclude):
            continue
        if location_must and not any(s in where or s in low for s in location_must):
            continue
        if any(k in low for k in keywords):
            pass
        elif check_detail and (not detail_trigger or any(k in low for k in detail_trigger)):
            body = " ".join(BeautifulSoup(d, "html.parser").get_text(" ", strip=True) for d in rss_text(item, "description")).lower()
            if not body:
                label += UNREADABLE_TAG
            elif not any(k in body for k in keywords):
                continue
        else:
            continue
        key = f"{name}|teamtailor|{guid}"
        if key in run_keys:
            continue
        run_keys.add(key)
        city = (rss_text(rest, "tt:city") or [""])[0]
        full = f"{label} ({city})" if city and city.lower() not in label.lower() else label
        open_matches.append(open_item(key, full, link))
        if key not in seen_keys:
            hits.append(Hit(full, link, key))
    return CheckResult(hits, open_matches, len(items))


NAV_API = "https://pam-stilling-feed.nav.no"
NAV_AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/"
NAV_MAX_PAGES = 20        # feed pages (1000 entries each) read per run, at most
NAV_MAX_DETAILS = 25      # single ads opened per run, at most
NAV_BOOTSTRAP_DAYS = 45   # first run: how far back to start reading
NAV_DRY_RUN_DAYS = 7      # dry run: a recent slice is enough to judge the filter


def nav_token():
    """NAV's feed needs a bearer token. NAV_FEED_TOKEN is your own (registered
    with NAV); without it the public token NAV publishes is fetched each run,
    since NAV rotates it."""
    own = os.environ.get("NAV_FEED_TOKEN", "").strip()
    if own:
        return own
    text = fetch("GET", f"{NAV_API}/api/publicToken", headers={"User-Agent": HEADERS["User-Agent"]}, timeout=30).text
    token = text.split()[-1] if text.split() else ""
    if token.count(".") != 2:
        raise SourceError("could not read NAV's public feed token")
    return token


def nav_facts(content):
    """Percent, contract and requirements from a NAV ad (its deadline has a field of its own)."""
    words = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", v)) for v in content.values() if isinstance(v, str))
    facts = extract_facts(words)
    return {"percent": facts["percent"], "contract": facts["contract"], "tags": facts["tags"],
            "read": today_oslo().isoformat()}


def check_nav(target, seen_keys, log: Log = _quiet, memory=None):
    """Follow NAV's official job vacancy feed (https://navikt.github.io/pam-stilling-feed/).

    The feed is a running log of every change to every ad in Norway, oldest
    first, so this source keeps a bookmark (the feed page to continue from) and
    its own list of matching ads that are still active, in `memory`, which
    run_radar stores in seen.json. memory=None (dry run) reads a recent slice.

    Fields in config.json:
      municipals             ad's municipality must be one of these (e.g. ULLENSAKER) …
      location_must_contain  … or the title must contain one of these
      keywords               role words that are enough on their own (flyekspeditør, check-in)
      keywords_at_airport    everyday words (kundeservice, servicemedarbeider) that only
                             count when the ad itself is at the airport: one of
      airport_words          must be in its title, employer, work address or text.
                             The municipality also holds a whole town of shops.
      exclude_keywords       matched on the ad title
      exclude_employers      skip employers the radar already follows directly
    """
    name = target["name"]
    keywords = lower_list(target, "keywords")
    exclude = lower_list(target, "exclude_keywords")
    municipals = {str(m).upper() for m in target.get("municipals", [])}
    location_must = lower_list(target, "location_must_contain")
    skip_employers = lower_list(target, "exclude_employers")
    soft_keywords = lower_list(target, "keywords_at_airport")
    airport_words = lower_list(target, "airport_words")

    def wanted(entry):
        """'yes', 'airport' (yes if the ad turns out to be at the airport) or ''."""
        title = (entry.get("title") or "").lower()
        employer = (entry.get("businessName") or "").lower()
        if (entry.get("municipal") or "").upper() not in municipals and not any(s in title for s in location_must):
            return ""
        if any(e in employer for e in skip_employers) or any(e in title for e in exclude):
            return ""
        if any(k in title for k in keywords):
            return "yes"
        if any(k in title for k in soft_keywords):
            return "yes" if any(w in f"{title} {employer}" for w in airport_words) else "airport"
        return ""

    def at_airport(content):
        places = " ".join(" ".join(str(v) for v in loc.values() if v) for loc in content.get("workLocations") or [] if isinstance(loc, dict))
        text = BeautifulSoup(content.get("description") or "", "html.parser").get_text(" ", strip=True)
        blob = f"{content.get('title')} {(content.get('employer') or {}).get('name')} {places} {text}".lower()
        return any(w in blob for w in airport_words)

    headers = {"Accept": "application/json", "Authorization": f"Bearer {nav_token()}", "User-Agent": HEADERS["User-Agent"]}
    dry = memory is None
    state = memory or {}
    open_ads = {k: dict(v) for k, v in (state.get("open") or {}).items()}
    cursor = state.get("cursor")
    if cursor:
        url, extra = f"{NAV_API}/api/v1/feed/{cursor}", {}
    else:
        since = now_utc() - timedelta(days=NAV_DRY_RUN_DAYS if dry else NAV_BOOTSTRAP_DAYS)
        url, extra = f"{NAV_API}/api/v1/feed", {"If-Modified-Since": format_datetime(since, usegmt=True)}

    pages = entries = 0
    caught_up = False
    while pages < NAV_MAX_PAGES:
        resp = fetch("GET", url, headers={**headers, **extra}, timeout=40)
        extra = {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise SourceError("NAV feed did not answer with JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SourceError("NAV feed answer has no 'items' list, API probably changed")
        pages += 1
        for item in data["items"]:
            entry = (item.get("_feed_entry") if isinstance(item, dict) else None) or {}
            uuid = entry.get("uuid")
            if not uuid:
                continue
            entries += 1
            verdict = wanted(entry) if entry.get("status") == "ACTIVE" else ""
            if not verdict:
                open_ads.pop(uuid, None)   # stopped, expired, or edited so it no longer matches
                continue
            known = open_ads.get(uuid)
            if known and known.get("changed") == entry.get("sistEndret"):
                continue
            open_ads[uuid] = {
                "pending": verdict == "airport",   # not shown until the ad itself confirms the airport
                "title": squash(entry.get("title")),
                "employer": squash(entry.get("businessName")),
                "changed": entry.get("sistEndret"),
                "link": NAV_AD_URL + uuid,
                "due": (known or {}).get("due", ""),
                "deadline": (known or {}).get("deadline"),
                "expires": (known or {}).get("expires"),
                "detailed": False,
            }
        if data.get("next_url") and data.get("next_id"):
            cursor = data["next_id"]
            url = NAV_API + data["next_url"]
        else:
            cursor = data.get("id") or cursor   # the newest page: read it again next run, it grows
            caught_up = True
            break
    log(f"read {pages} feed page(s), {entries} ad changes" + ("" if caught_up else ", still catching up (continues next run)"))

    # open the few ads that matched, for deadline and expiry
    opened = 0
    for uuid, ad in sorted(open_ads.items()):
        if ad.get("detailed") or opened >= NAV_MAX_DETAILS:
            continue
        opened += 1
        try:
            detail = fetch("GET", f"{NAV_API}/api/v1/feedentry/{uuid}", headers=headers, timeout=40).json()
        except (requests.RequestException, ValueError):
            continue   # try again next run; the ad still alerts with what the feed gave us
        content = detail.get("ad_content") if isinstance(detail, dict) else None
        if not isinstance(content, dict) or detail.get("status") != "ACTIVE":
            open_ads.pop(uuid, None)
            continue
        if ad.get("pending"):
            if at_airport(content):
                ad["pending"] = False
            else:   # a shop in town: remember the verdict so the ad is not opened again every run
                ad.update({"detailed": True, "rejected": True, "expires": content.get("expires")})
                continue
        ad.update({
            "title": squash(content.get("title")) or ad["title"],
            "employer": squash((content.get("employer") or {}).get("name")) or ad["employer"],
            "due": squash(content.get("applicationDue")),
            "deadline": parse_deadline(content.get("applicationDue")),
            "expires": content.get("expires"),
            "link": content.get("link") or ad["link"],
            "detailed": True,
            "facts": nav_facts(content),
        })

    now = now_utc()
    for uuid, ad in list(open_ads.items()):
        try:
            if ad.get("expires") and datetime.fromisoformat(ad["expires"]) < now:
                open_ads.pop(uuid)
        except (ValueError, TypeError):
            pass

    hits, open_matches = [], []
    for uuid, ad in sorted(open_ads.items()):
        if ad.get("pending") or ad.get("rejected"):
            continue
        title = f"{ad['title']} ({ad['employer']})" if ad.get("employer") else ad["title"]
        label = f"{title} · frist {ad['due']}" if ad.get("due") else title
        key = f"{name}|nav|{uuid}"
        log(f"  MATCH {label}")
        item = open_item(key, title, ad["link"], label, ad.get("deadline"))
        if ad.get("facts"):
            item["details"] = ad["facts"]
            extra = facts_line(None, ad["facts"])
            label = f"{label} · {extra}" if extra else label
        open_matches.append(item)
        if key not in seen_keys:
            hits.append(Hit(label, ad["link"], key))
    if memory is not None:   # only now, so a failed read never leaves a half-updated bookmark
        memory.clear()
        memory.update({"cursor": cursor, "open": open_ads})
    return CheckResult(hits, open_matches, None)


SOURCES = {
    "webcruiter": check_webcruiter,
    "successfactors": check_successfactors,
    "teamtailor": check_teamtailor,
}


def check(target, seen_keys, log: Log = _quiet, memory=None):
    """memory: a dict the source may keep between runs (None in a dry run)."""
    kind = target.get("type") or "html"
    if kind == "html":
        return check_html(target, seen_keys, log)
    if kind == "nav":
        return check_nav(target, seen_keys, log, memory)
    if kind not in SOURCES:
        raise SourceError(f"unknown target type '{kind}' in config.json")
    return SOURCES[kind](target, seen_keys, log)


# --------------------------------------------------------------------------
# job memory (docs/jobs.json)
# --------------------------------------------------------------------------

def update_jobs(prev_jobs, checked, now, first_seen_hint=None, issues=None):
    """Fold this run into the list of every wanted posting ever seen.

    prev_jobs  the "jobs" list from the last docs/jobs.json
    checked    [(target, CheckResult or None, error)] as built by run_radar
    now        ISO timestamp of this run
    first_seen_hint  {link or "target|title": time} from earlier alerts, so a
               posting alerted before jobs.json existed keeps its real date

    A posting listed now is open. One that was open and is gone from a source
    that answered CLOSE_AFTER_MISSES checks in a row is closed: a source can
    answer "OK" with an empty list for a moment, and that must not close a
    live job. A source that failed says nothing about its postings, so they
    are left exactly as they were."""
    hint = first_seen_hint or {}
    issues = issues or {}
    jobs = {j["key"]: dict(j) for j in prev_jobs if isinstance(j, dict) and j.get("key")}
    for target, result, _error in checked:
        if result is None:
            continue
        name = target.get("name", "?")
        live = set()
        for item in result.open:
            key = item.get("key")
            if not key:
                continue
            live.add(key)
            job = jobs.get(key)
            if job is None:
                job = jobs[key] = {
                    "key": key,
                    "target": name,
                    "first_seen": hint.get(item.get("link")) or hint.get(f"{name}|{item.get('title')}") or now,
                    "deadline": None,
                }
            job.update({
                "target": name,
                "text": item.get("text", ""),
                "title": item.get("title") or item.get("text", ""),
                "link": item.get("link", ""),
                # a deadline once known is kept if a later read cannot find it
                "deadline": item.get("deadline") or job.get("deadline"),
                "last_seen": now,
                "state": "open",
                "closed_at": None,
            })
            job.pop("missed", None)
            for field in ("details", "details_tried"):
                if item.get(field):
                    job[field] = item[field]
            if key in issues:
                job["issue"] = issues[key]   # the GitHub issue that alerted this job
        for job in jobs.values():
            if job.get("target") == name and job.get("state") == "open" and job["key"] not in live:
                job["missed"] = job.get("missed", 0) + 1
                if job["missed"] >= CLOSE_AFTER_MISSES:
                    job["state"] = "closed"
                    job["closed_at"] = now
                    job.pop("missed", None)

    ordered = sorted(jobs.values(), key=lambda j: (j.get("first_seen", ""), j["key"]))
    extra = len(ordered) - MAX_JOB_HISTORY
    if extra > 0:
        closed = sorted((j for j in ordered if j.get("state") == "closed"),
                        key=lambda j: (j.get("closed_at") or "", j["key"]))
        drop = {j["key"] for j in closed[:extra]}
        ordered = [j for j in ordered if j["key"] not in drop]
    return ordered


# --------------------------------------------------------------------------
# deadline reminders and the calendar feed
# --------------------------------------------------------------------------

def days_left(job, today):
    try:
        return (date.fromisoformat(job.get("deadline") or "") - today).days
    except ValueError:
        return None


def due_reminders(jobs, today, skip_keys=()):
    """[(job, days left, level)] for open jobs whose deadline is close.
    level is the REMIND_DAYS step being served (3, then 1). Each step is sent
    once. A job alerted in this very run (skip_keys) waits until the next run:
    you just got its alert."""
    due = []
    for job in jobs:
        left = days_left(job, today)
        if job.get("state") != "open" or left is None or left < 0 or job["key"] in skip_keys:
            continue
        done = job.get("reminded", [])
        # the tightest step that applies; once a tighter one is sent the looser ones are moot
        level = next((d for d in sorted(REMIND_DAYS) if left <= d), None)
        if level is None or any(d <= level for d in done):
            continue
        due.append((job, left, level))
    return due


def reminder_text(left):
    return "FRIST I DAG" if left == 0 else "Frist i morgen" if left == 1 else f"Frist om {left} dager"


def send_reminders(cfg, jobs, today, skip_keys=()):
    """Send what due_reminders() found and mark each as sent on the job.
    Closing a job's GitHub issue means "done with this one": no reminder.
    Returns [(job, days left)] for the reminders that went out."""
    sent = []
    for job, left, level in due_reminders(jobs, today, skip_keys):
        number = job.get("issue")
        if number and issue_is_closed(number):
            continue
        when = date.fromisoformat(job["deadline"]).strftime("%d.%m")
        title = f"⏰ {reminder_text(left)} ({when}): {job.get('title') or job.get('text', '')}"
        delivered = send_direct(cfg, title, f"{job.get('target', '')}. Søknadsfristen er {when}.", job.get("link", ""), push=PUSH_REMINDER)
        if email_mode(cfg) == "issue":
            line = f"**{reminder_text(left)} ({when})**: {md_link(job.get('title') or job.get('text', ''), job.get('link', ''))}"
            if number:
                ok = comment_issue(number, f"⏰ {line}\n\nFerdig med denne? Lukk saken, så slutter påminnelsene.")
            else:  # alerted before jobs had their own issue: a notice of its own
                ok = bool(create_issue(title, f"⏰ {line} — {job.get('target', '')}", close=True))
            delivered = delivered or ok
        if delivered or not any_channel(cfg):
            job["reminded"] = sorted(set(job.get("reminded", [])) | {level})
            sent.append((job, left))
    return sent


def late_deadlines(prev_jobs, jobs, skip_keys=()):
    """Open jobs the radar already knew without a deadline, that have one now."""
    had_none = {j.get("key") for j in prev_jobs if isinstance(j, dict) and not j.get("deadline")}
    return [j for j in jobs if j.get("state") == "open" and j.get("deadline")
            and j["key"] in had_none and j["key"] not in skip_keys]


def send_deadline_found(cfg, jobs, today):
    """Tell you about a deadline the radar found after the job was alerted.
    Skipped when the deadline is so close that a reminder goes out anyway, or
    already passed. Returns the jobs it told you about."""
    told = []
    for job in jobs:
        left = days_left(job, today)
        if left is None or left <= max(REMIND_DAYS):
            continue
        number = job.get("issue")
        if number and issue_is_closed(number):
            continue
        when = date.fromisoformat(job["deadline"]).strftime("%d.%m.%Y")
        name = job.get("title") or job.get("text", "")
        title = f"📅 Frist funnet ({when}): {name}"
        delivered = send_direct(cfg, title, f"{job.get('target', '')}. Radaren leste annonsen: søknadsfristen er {when}.",
                                job.get("link", ""), push=PUSH_REMINDER)
        if email_mode(cfg) == "issue":
            line = f"**Frist funnet: {when}** ({reminder_text(left).lower()}): {md_link(name, job.get('link', ''))}"
            if number:
                ok = comment_issue(number, f"📅 {line}\n\nRadaren leste selve annonsen. Du får påminnelse 3 dager og 1 dag før.")
            else:
                ok = bool(create_issue(title, f"📅 {line} — {job.get('target', '')}", close=True))
            delivered = delivered or ok
        if delivered:
            told.append(job)
    return told


def ics_escape(text):
    return (str(text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r", "").replace("\n", "\\n"))


def ics_fold(line):
    """RFC 5545: lines are at most 75 bytes; continuation lines start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return [line]
    out, chunk = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(chunk) + len(b) > (75 if not out else 74):
            out.append(chunk.decode("utf-8"))
            chunk = b""
        chunk += b
    out.append(chunk.decode("utf-8"))
    return [out[0]] + [" " + part for part in out[1:]]


def build_calendar(jobs, labels=None):
    """iCalendar text: one all-day event on the deadline of every open job.
    Subscribe to docs/frister.ics once and the deadlines follow you."""
    labels = labels or {}
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//jobbradar//frister//NO", "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH", "X-WR-CALNAME:Jobbradar frister", "X-WR-TIMEZONE:Europe/Oslo",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
    ]
    for job in sorted(jobs, key=lambda j: (j.get("deadline") or "", j.get("key", ""))):
        if job.get("state") != "open":
            continue
        try:
            day = date.fromisoformat(job.get("deadline") or "")
        except ValueError:
            continue
        try:
            stamp = datetime.fromisoformat(job.get("first_seen", "")).astimezone(timezone.utc)
        except (ValueError, TypeError):
            stamp = datetime(day.year, 1, 1, tzinfo=timezone.utc)
        title = job.get("title") or job.get("text", "")
        who = labels.get(job.get("target"), job.get("target", ""))
        uid = hashlib.sha1(job["key"].encode("utf-8")).hexdigest()[:20]
        end = date.fromordinal(day.toordinal() + 1)
        lines += [
            "BEGIN:VEVENT",
            f"UID:{uid}@jobbradar",
            f"DTSTAMP:{stamp.strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}",
            f"SUMMARY:{ics_escape('Søknadsfrist: ' + title + (f' ({who})' if who else ''))}",
            f"DESCRIPTION:{ics_escape(job.get('link', ''))}",
            f"URL:{job.get('link', '')}",
            "TRANSP:TRANSPARENT",
            "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_escape('Søknadsfrist i morgen: ' + title)}",
            "TRIGGER:-PT15H",   # 09:00 the day before
            "END:VALARM",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(part for line in lines for part in ics_fold(line)) + "\r\n"


def save_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))   # bytes: keep the CRLF line ends exactly
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# the dashboard's log and trend lines (status.json "events" and "history")
# --------------------------------------------------------------------------

def event(t, kind, target, text, link=""):
    return {"t": t, "type": kind, "target": target, "text": text, "link": link}


def build_events(jobs, run_report, fail_before, new_hits, reminded, stamp, found=(), prev_listings=None):
    """What happened in this run, as log lines for the dashboard.

    jobs         the job list after update_jobs()
    run_report   the per-target entries written to status.json
    fail_before  {target name: failures in a row} as it was before this run
    new_hits     [(target name, Hit)] that were delivered this run
    reminded     [(job, days left)] from send_reminders()
    found        jobs whose deadline the radar found in this run (late_deadlines)
    prev_listings  {target name: listings at the last check}

    Types: new, closed, fail (only the first failure in a row), back, empty
    (a source that had listings answers with none), deadline, reminder, and
    one closing "check" line that sums the run up."""
    prev_listings = prev_listings or {}
    out = [event(stamp, "new", name, hit.text, hit.link) for name, hit in new_hits]
    out += [event(stamp, "closed", j.get("target", ""), j.get("title") or j.get("text", ""), j.get("link", ""))
            for j in jobs if j.get("state") == "closed" and j.get("closed_at") == stamp]
    for r in run_report:
        before = fail_before.get(r["name"], 0)
        if not r["ok"] and before == 0:
            out.append(event(stamp, "fail", r["name"], r.get("error") or "svarer ikke", r.get("url", "")))
        elif r["ok"] and before > 0:
            out.append(event(stamp, "back", r["name"], f"virker igjen etter {before} feilet sjekk" + ("" if before == 1 else "er"), r.get("url", "")))
        if r["ok"] and r.get("listings") == 0 and (prev_listings.get(r["name"]) or 0) > 0:
            out.append(event(stamp, "empty", r["name"], f"0 stillinger på siden, var {prev_listings[r['name']]}. Kan være en feil hos kilden", r.get("url", "")))
    for j in found:
        when = date.fromisoformat(j["deadline"]).strftime("%d.%m.%Y")
        out.append(event(stamp, "deadline", j.get("target", ""), f"Frist funnet {when}: {j.get('title') or j.get('text', '')}", j.get("link", "")))
    out += [event(stamp, "reminder", j.get("target", ""), f"{reminder_text(left)}: {j.get('title') or j.get('text', '')}", j.get("link", ""))
            for j, left in reminded]
    failing = sum(1 for r in run_report if not r["ok"])
    open_now = sum(1 for j in jobs if j.get("state") == "open")
    summary = f"{len(run_report)} kilder sjekket, {open_now} ledig" + (f", {failing} feilet" if failing else "")
    out.append(event(stamp, "check", "", summary))
    return out


def seed_events(alerts, jobs):
    """First run with a log: start it from what the radar already remembers,
    so the dashboard does not open on an empty log."""
    seeded = [event(a["time"], "new", a.get("target", ""), a.get("text", ""), a.get("link", "")) for a in alerts if a.get("time")]
    seeded += [event(j["closed_at"], "closed", j.get("target", ""), j.get("title") or j.get("text", ""), j.get("link", ""))
               for j in jobs if j.get("state") == "closed" and j.get("closed_at")]
    return sorted(seeded, key=lambda e: e["t"], reverse=True)


def hiring_windows(cfg):
    """The "windows" list from config.json (when each employer usually hires:
    your own estimates), cleaned for the dashboard. opens is 'YYYY-MM-DD', or
    None for an employer that hires all year."""
    out = []
    for w in cfg.get("windows") or []:
        if not isinstance(w, dict) or not squash(str(w.get("label") or "")):
            continue
        try:
            opens = date.fromisoformat(str(w.get("opens"))).isoformat()
        except ValueError:
            opens = None
        out.append({"label": squash(str(w["label"])), "note": squash(str(w.get("note") or "")),
                    "target": str(w.get("target") or ""), "opens": opens})
    return out


def history_entry(run_report, stamp):
    """Listing count per source for this run: the dashboard's trend lines."""
    return {
        "t": stamp,
        "n": {r["id"]: r.get("listings") for r in run_report if r["ok"]},
        "fail": [r["id"] for r in run_report if not r["ok"]],
    }


# --------------------------------------------------------------------------
# weekly "radar alive" mail
# --------------------------------------------------------------------------

def weekly_heartbeat(cfg, run_report, sent_week, last_counts, force=False):
    """Once a week (first run on or after Monday morning) send a 'radar is alive'
    summary with live listing counts per company. A count that drops to 0 or
    halves since last week is flagged: that is what a quietly broken parser
    looks like. Returns the updated (sent_week, last_counts)."""
    now = now_utc()
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
        # a failed check has no count; keep last week's so the trend survives
        counts[t["name"]] = n if t["ok"] else prev
        if not t["ok"]:
            state_txt = f"FEIL ({t['consecutive_failures']} på rad)"
            warnings.append(f"{t['name']}: klarer ikke lese siden")
        else:
            state_txt = f"{n} annonser" if n is not None else "OK"
            if n == 0:
                warnings.append(f"{t['name']}: 0 annonser. Åpne siden selv og sjekk")
            elif prev and n is not None and n < prev / 2:
                warnings.append(f"{t['name']}: falt fra {prev} til {n} annonser siden forrige uke")
        trend = f" (forrige uke {prev})" if t["ok"] and prev is not None and prev != n else ""
        lines.append(f"- {t['name']}: {state_txt}{trend}")

    status = "SJEKK DETTE" if warnings else "alt ser normalt ut"
    title = f"Radar lever · uke {iso[1]} · {status}"
    body = "\n".join(lines)
    if warnings:
        body += "\n\n⚠️ " + "\n⚠️ ".join(warnings)
    body += "\n\nIngen handling trengs hvis alt ser normalt ut."
    delivered = send_notice(cfg, title, body, dashboard_url(cfg))
    if not delivered and any_channel(cfg):
        print("::warning::Heartbeat was not delivered, trying again next run")
        return sent_week, last_counts
    print(f"Heartbeat sent: {title}")
    return week, counts


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def run_test(cfg):
    """Send one fake alert through the real paths. Touches no state."""
    link = "https://careers.sasgroup.net/search/?q=&locationsearch=Gardermoen"
    sent = send_direct(
        cfg,
        "Jobbradar: test",
        "Varsling virker. Ekte varsler ser slik ut, med tittel, sted og frist.",
        link,
    )
    if email_mode(cfg) == "issue":
        entry = {
            "time": now_iso(),
            "target": "TEST",
            "text": "TEST · Servicemedarbeider (Gardermoen) · varslingen virker",
            "link": link,
        }
        # same code path as a real hit; closed at once so tests don't pile up
        if send_hits_issue([entry], close=True):
            sent = True
    if not sent:
        print("::error::Test alert was NOT delivered on any channel.")
        if not any_channel(cfg):
            print("No notification channel is configured here. In GitHub Actions email works "
                  "out of the box; locally set smtp or ntfy_topic in config.json.")
        return 1
    print("Test alert delivered.")
    return 0


def run_dry(cfg, only="", dump=True):
    """Read every source and print what WOULD alert (ignoring seen.json).
    Sends nothing and writes nothing. Returns 1 if any source failed."""
    failed = 0
    for target in cfg.get("targets", []):
        if only and only not in target["name"].lower():
            continue
        print(f"\n===== {target['name']} =====")
        try:
            result = check(target, set(), print if dump else _quiet)
        except Exception as exc:  # noqa: BLE001 - report every kind of failure
            failed += 1
            print(f"  ERROR: {short_error(exc)}")
            continue
        for hit in result.hits:
            print(f"  WOULD ALERT: {hit.text} -> {hit.link}")
        print(f"  {len(result.hits)} would-alert hit(s), listings counted: {result.listings}")
    return 1 if failed else 0


def run_radar(cfg, force_heartbeat=False):
    state = load_json(STATE_PATH, {})
    seen_keys = set(state.get("seen", []))
    fail_counts = state.get("fail", {})
    alerts = state.get("alerts", [])
    source_memory = state.get("sources", {})   # bookmarks some sources keep between runs
    fail_before = dict(fail_counts)
    prev_status = load_json(STATUS_PATH, {})
    prev_open = {t["name"]: t.get("open", []) for t in prev_status.get("targets", [])}
    prev_listings = {t["name"]: t.get("listings") for t in prev_status.get("targets", []) if t.get("ok")}
    prev_jobs = load_json(JOBS_PATH, {}).get("jobs", [])
    reads_left = MAX_DETAIL_READS

    channels_exist = any_channel(cfg)
    if not channels_exist:
        print("WARNING: no notification channel configured. Hits are only printed here. "
              "Set smtp/ntfy_topic in config.json, or run in GitHub Actions.")

    checked = []   # (target, result or None, error text)
    pending = []   # (target name, Hit, delivered on a direct channel?)

    for target in cfg.get("targets", []):
        name = target.get("name", "?")
        before = fail_counts.get(name, 0)
        try:
            result = check(target, seen_keys, memory=source_memory.setdefault(name, {}))
        except Exception as exc:  # noqa: BLE001 - one broken target must never stop the others
            error = short_error(exc)
            fail_counts[name] = before + 1
            print(f"[{name}] check failed ({fail_counts[name]} in a row): {error}")
            if fail_counts[name] == FAIL_WARN_THRESHOLD:
                send_notice(
                    cfg,
                    f"Jobbradar: {name} svarer ikke",
                    f"{FAIL_WARN_THRESHOLD} sjekker på rad har feilet. Siste feil: {error}.\n"
                    "Siden kan være flyttet, nede eller blokkere radaren. "
                    "Sjekk siden selv til dette er løst, og se config.json.",
                    target.get("url", ""),
                )
            checked.append((target, None, error))
            continue

        fail_counts[name] = 0
        if before >= FAIL_WARN_THRESHOLD:
            send_notice(
                cfg,
                f"Jobbradar: {name} virker igjen",
                f"Radaren leser siden igjen etter {before} feilede sjekker på rad.",
                target.get("url", ""),
            )
        try:   # read the ads themselves; never let that break a source that answered
            reads_left -= read_details(result, prev_jobs, today_oslo(), reads_left, target.get("url", ""))
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] could not read job ads: {short_error(exc)}")
        direct_ok = send_hits_direct(cfg, name, result.hits, target["url"]) if result.hits else False
        pending.extend((name, hit, direct_ok) for hit in result.hits)
        checked.append((target, result, ""))
        print(f"[{name}] checked OK, {len(result.hits)} new hit(s)")

    # One GitHub issue (= one email) per job; a flood shares a single issue.
    issue_of = {}   # hit key -> issue number
    if pending and email_mode(cfg) == "issue":
        entries = [{"target": name, "text": hit.text, "link": hit.link} for name, hit, _ in pending]
        if len(pending) > DIGEST_THRESHOLD:
            number = send_hits_issue(entries)
            if number:
                issue_of = {hit.key: number for _, hit, _ in pending}
        else:
            for (_, hit, _), entry in zip(pending, entries):
                number = send_hits_issue([entry])
                if number:
                    issue_of[hit.key] = number

    # A hit only counts as seen once it actually reached you. If every channel
    # failed, it stays unseen and alerts again on the next run.
    undelivered = 0
    delivered_hits = []
    for name, hit, direct_ok in pending:
        if direct_ok or hit.key in issue_of or not channels_exist:
            delivered_hits.append((name, hit))
            seen_keys.add(hit.key)
            alerts.insert(0, {"time": now_iso(), "target": name, "text": hit.text, "link": hit.link})
        else:
            undelivered += 1
    if undelivered:
        print(f"::error::{undelivered} hit(s) could not be delivered on any channel. "
              "They are NOT marked as seen and will alert again on the next run.")
    alerts = alerts[:MAX_ALERT_HISTORY]

    run_report = []
    for target, result, error in checked:
        name = target.get("name", "?")
        entry = {
            "name": name,
            "id": target.get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"),
            "label": target.get("label") or name,
            "sub": target.get("sub", ""),
            "url": target.get("url", ""),
            "ok": result is not None,
            "consecutive_failures": fail_counts.get(name, 0),
            "new_hits": len(result.hits) if result else 0,
            "total_seen": sum(1 for k in seen_keys if k.startswith(name + "|")),
        }
        if result is not None:
            entry["listings"] = result.listings
            entry["open"] = result.open
        else:
            entry["error"] = error
            entry["open"] = prev_open.get(name, [])  # keep showing the last known state
        run_report.append(entry)

    heartbeat_week, last_counts = weekly_heartbeat(
        cfg, run_report, state.get("heartbeat_week", ""), state.get("last_counts", {}),
        force=force_heartbeat,
    )
    save_json(STATE_PATH, {
        "seen": sorted(seen_keys),
        "fail": fail_counts,
        "alerts": alerts,
        "heartbeat_week": heartbeat_week,
        "last_counts": last_counts,
        "sources": {k: v for k, v in source_memory.items() if v},
    })
    stamp = now_iso()
    hint = {}
    for a in reversed(alerts):  # oldest last, so the earliest alert wins
        if a.get("time"):
            hint[a.get("link")] = a["time"]
            hint[f"{a.get('target')}|{a.get('text')}"] = a["time"]
    jobs = update_jobs(prev_jobs, checked, stamp, first_seen_hint=hint, issues=issue_of)
    # a posting missing once is still open (see update_jobs): keep it on the dashboard's lists too
    for entry in run_report:
        if entry["ok"]:
            listed = {o.get("key") for o in entry["open"]}
            entry["open"] = entry["open"] + [
                open_item(j["key"], j.get("title") or j.get("text", ""), j.get("link", ""), j.get("text"), j.get("deadline"))
                for j in jobs if j.get("target") == entry["name"] and j.get("state") == "open" and j["key"] not in listed]
    new_keys = {hit.key for _, hit, _ in pending}
    found = late_deadlines(prev_jobs, jobs, skip_keys=new_keys)
    send_deadline_found(cfg, found, today_oslo())
    reminded = send_reminders(cfg, jobs, today_oslo(), skip_keys=new_keys)
    save_json(JOBS_PATH, {"generated": stamp, "jobs": jobs})
    save_text(CALENDAR_PATH, build_calendar(jobs, {t.get("name"): t.get("label") or t.get("name") for t in cfg.get("targets", [])}))
    old_events = prev_status["events"] if isinstance(prev_status.get("events"), list) else seed_events(alerts[len(delivered_hits):], prev_jobs)
    events = build_events(jobs, run_report, fail_before, delivered_hits, reminded, stamp,
                          found=found, prev_listings=prev_listings) + old_events
    save_json(STATUS_PATH, {
        "generated": stamp,
        "channels": {"email": bool(email_mode(cfg)), "push": bool(get_topic(cfg))},
        "targets": run_report,
        "recent_alerts": alerts,
        "runs": (prev_status.get("runs", []) + [stamp])[-MAX_RUN_LOG:],
        "calendar": True,   # docs/frister.ics exists; the dashboard shows a subscribe link
        "windows": hiring_windows(cfg),
        "events": events[:MAX_EVENTS],                                                              # newest first
        "history": (prev_status.get("history", []) + [history_entry(run_report, stamp)])[-MAX_RUN_LOG:],  # oldest first
    })
    delivered = len(pending) - undelivered
    open_now = sum(1 for j in jobs if j.get("state") == "open")
    print(f"Done. {delivered} new alert(s) and {len(reminded)} deadline reminder(s) this run, {open_now} wanted job(s) open. "
          "Status written to docs/status.json and docs/jobs.json")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Job Radar: watch career pages, alert on wanted roles.")
    parser.add_argument("--test", action="store_true", help="send a test alert through the real channels, touch no state")
    parser.add_argument("--dry-run", action="store_true", help="read every source, print every listing and what would alert; send and save nothing")
    parser.add_argument("--heartbeat", action="store_true", help="also send the weekly 'radar alive' mail now")
    parser.add_argument("--write", action="store_true", help="real run outside GitHub Actions: send alerts and save state")
    args = parser.parse_args(argv)

    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        sys.exit("config.json missing. Create it next to this script first.")

    if args.test:
        return run_test(cfg)
    if args.dry_run:
        return run_dry(cfg, only=os.environ.get("DRY_RUN_TARGET", "").strip().lower())
    if args.write or os.environ.get("GITHUB_ACTIONS") == "true":
        return run_radar(cfg, force_heartbeat=args.heartbeat)

    # Plain local run: look, don't touch. seen.json and docs/status.json belong
    # to the GitHub runs; rewriting them from a laptop makes the dashboard lie.
    print("Lokal forhåndsvisning: leser alle kildene, sender ingenting og lagrer ingenting.\n"
          "Treffene under er alt som er ledig nå, også det du allerede er varslet om.\n"
          "Ekte kjøring: --write. Se alt kildene svarer: --dry-run.")
    return run_dry(cfg, dump=False)


if __name__ == "__main__":
    sys.exit(main())
