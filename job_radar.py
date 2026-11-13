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

Three kinds of source (the "type" field of a target in config.json):
  (none)           an HTML career page, read with BeautifulSoup
  webcruiter       the Webcruiter candidate JSON API (Vy)
  successfactors   the public SAP SuccessFactors job feed (Norwegian). Their own
                   career site sits behind a Cloudflare challenge that GitHub's
                   servers never pass; the feed is the same data, hosted by SAP.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
MAX_JOB_HISTORY = 300     # postings remembered in docs/jobs.json (oldest closed ones go first)
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
    Returns True when the issue was created. Never raises."""
    conf = issue_conf()
    if not conf:
        return False
    token, repo = conf
    owner = repo.split("/", 1)[0]
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.post(
            f"{api}/repos/{repo}/issues",
            headers=headers,
            json={"title": title[:240], "body": f"@{owner}\n{body}"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"::error::GitHub issue failed: {short_error(exc)}")
        return False
    if close:
        try:
            requests.patch(
                f"{api}/repos/{repo}/issues/{resp.json()['number']}",
                headers=headers,
                json={"state": "closed"},
                timeout=30,
            ).raise_for_status()
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            print(f"could not close the issue (the mail was still sent): {exc}")
    return True


def send_direct(cfg, title, message, url=""):
    """Push the alert through every channel you configured yourself:
    ntfy push, SMTP email, Telegram. Env vars override config.
    Returns True when at least one of them delivered. Never raises."""
    sent = False

    topic = get_topic(cfg)
    if topic:
        try:
            payload = {
                "topic": topic,
                "title": title,
                "message": message,
                "priority": 5,
                "tags": ["rotating_light"],
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
    sent = send_direct(cfg, title, message, url)
    if email_mode(cfg) == "issue":
        body = message + (f"\n\n{url}" if url else "")
        if create_issue(title, body, close=True):
            sent = True
    return sent


def md_link(text, link):
    safe = link.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
    return f"[{text}]({safe})"


def send_hits_issue(entries, close=False):
    """One issue (= one email) per run, however many hits it had."""
    title = entries[0]["text"]
    if len(entries) > 1:
        title = f"{title} (+{len(entries) - 1} til)"
    lines = [f"- {md_link(e['text'], e['link'])} — {e['target']}" for e in entries]
    stamp = now_utc().strftime("%d.%m.%Y %H:%M")
    body = "\n".join(lines) + f"\n\nRadaren fant dette {stamp} UTC. Triager på tavla, utrykning hvis HØY."
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
        link = ad.get("OpenAdvertUrl") or target["url"]
        key = f"{name}|webcruiter|{ad.get('Id') or link}"
        place = (ad.get("Workplace") or "").strip()
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
    jobs = parse_successfactors(resp.content.decode("utf-8", errors="replace"))
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
        open_matches.append(open_item(key, label, link))  # the feed states no deadline
        if key not in seen_keys:
            hits.append(Hit(label, link, key))
    return CheckResult(hits, open_matches, len(jobs))


SOURCES = {
    "webcruiter": check_webcruiter,
    "successfactors": check_successfactors,
}


def check(target, seen_keys, log: Log = _quiet):
    kind = target.get("type") or "html"
    if kind == "html":
        return check_html(target, seen_keys, log)
    if kind not in SOURCES:
        raise SourceError(f"unknown target type '{kind}' in config.json")
    return SOURCES[kind](target, seen_keys, log)


# --------------------------------------------------------------------------
# job memory (docs/jobs.json)
# --------------------------------------------------------------------------

def update_jobs(prev_jobs, checked, now, first_seen_by_link=None):
    """Fold this run into the list of every wanted posting ever seen.

    prev_jobs  the "jobs" list from the last docs/jobs.json
    checked    [(target, CheckResult or None, error)] as built by run_radar
    now        ISO timestamp of this run

    A posting listed now is open. One that was open and is gone from a source
    that answered is closed. A source that failed says nothing about its
    postings, so they are left exactly as they were."""
    first_seen_by_link = first_seen_by_link or {}
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
                    "first_seen": first_seen_by_link.get(item.get("link"), now),
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
        for job in jobs.values():
            if job.get("target") == name and job.get("state") == "open" and job["key"] not in live:
                job["state"] = "closed"
                job["closed_at"] = now

    ordered = sorted(jobs.values(), key=lambda j: (j.get("first_seen", ""), j["key"]))
    extra = len(ordered) - MAX_JOB_HISTORY
    if extra > 0:
        closed = sorted((j for j in ordered if j.get("state") == "closed"),
                        key=lambda j: (j.get("closed_at") or "", j["key"]))
        drop = {j["key"] for j in closed[:extra]}
        ordered = [j for j in ordered if j["key"] not in drop]
    return ordered


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
    prev_status = load_json(STATUS_PATH, {})
    prev_open = {t["name"]: t.get("open", []) for t in prev_status.get("targets", [])}

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
            result = check(target, seen_keys)
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
        direct_ok = send_hits_direct(cfg, name, result.hits, target["url"]) if result.hits else False
        pending.extend((name, hit, direct_ok) for hit in result.hits)
        checked.append((target, result, ""))
        print(f"[{name}] checked OK, {len(result.hits)} new hit(s)")

    # one GitHub issue (= one email) for the whole run
    issue_ok = False
    if pending and email_mode(cfg) == "issue":
        stamp = now_iso()
        issue_ok = send_hits_issue([
            {"time": stamp, "target": name, "text": hit.text, "link": hit.link}
            for name, hit, _ in pending
        ])

    # A hit only counts as seen once it actually reached you. If every channel
    # failed, it stays unseen and alerts again on the next run.
    undelivered = 0
    for name, hit, direct_ok in pending:
        if direct_ok or issue_ok or not channels_exist:
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
    })
    stamp = now_iso()
    jobs = update_jobs(
        load_json(JOBS_PATH, {}).get("jobs", []), checked, stamp,
        first_seen_by_link={a["link"]: a["time"] for a in reversed(alerts) if a.get("link") and a.get("time")},
    )
    save_json(JOBS_PATH, {"generated": stamp, "jobs": jobs})
    save_json(STATUS_PATH, {
        "generated": stamp,
        "channels": {"email": bool(email_mode(cfg)), "push": bool(get_topic(cfg))},
        "targets": run_report,
        "recent_alerts": alerts,
        "runs": (prev_status.get("runs", []) + [stamp])[-MAX_RUN_LOG:],
    })
    delivered = len(pending) - undelivered
    open_now = sum(1 for j in jobs if j.get("state") == "open")
    print(f"Done. {delivered} new alert(s) this run, {open_now} wanted job(s) open. "
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
