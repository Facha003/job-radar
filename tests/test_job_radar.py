"""Tests for job_radar.py. No network: every HTTP call is faked.

Run:  pip install -r requirements-dev.txt && pytest
The fixtures are small hand-written pages shaped like the real career sites.
"""
import json
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import job_radar as jr  # noqa: E402

CONFIG = json.loads((Path(jr.__file__).parent / "config.json").read_text(encoding="utf-8"))


def target(prefix):
    return next(t for t in CONFIG["targets"] if t["name"].startswith(prefix))


class Resp:
    def __init__(self, text="", status=200, data=None):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status
        self.reason = "Forbidden" if status == 403 else "OK"
        self._data = data

    def json(self):
        if self._data is not None:
            return self._data
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


def serve(monkeypatch, pages):
    """pages: {url: html | Exception}. Unknown URLs raise ConnectionError."""
    calls = []

    def fake_fetch(method, url, **kwargs):
        calls.append(url)
        page = pages.get(url)
        if page is None:
            raise requests.ConnectionError(f"no fixture for {url}")
        if isinstance(page, Exception):
            raise page
        return page if isinstance(page, Resp) else Resp(page)

    monkeypatch.setattr(jr, "fetch", fake_fetch)
    return calls


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """No real channels, no real files, no real network, no sleeping."""
    for var in ("NTFY_TOPIC", "ALERT_EMAIL", "SMTP_USER", "SMTP_PASS", "SMTP_HOST", "SMTP_PORT",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_TOKEN", "GITHUB_REPOSITORY", "NAV_FEED_TOKEN",
                "GITHUB_ACTIONS", "GITHUB_API_URL", "DRY_RUN_TARGET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(jr, "STATE_PATH", tmp_path / "seen.json")
    monkeypatch.setattr(jr, "STATUS_PATH", tmp_path / "docs" / "status.json")
    monkeypatch.setattr(jr, "JOBS_PATH", tmp_path / "docs" / "jobs.json")
    monkeypatch.setattr(jr, "CALENDAR_PATH", tmp_path / "docs" / "frister.ics")
    monkeypatch.setattr(jr.time, "sleep", lambda s: None)

    def no_network(*a, **k):
        raise AssertionError("test tried to use the real network")

    monkeypatch.setattr(jr.requests, "request", no_network)
    monkeypatch.setattr(jr.requests, "post", no_network)
    monkeypatch.setattr(jr.requests, "patch", no_network)
    monkeypatch.setattr(jr.requests, "get", no_network)


# ---------------------------------------------------------------- HTML: SAS

SAS_URL = "https://careers.sasgroup.net/search/?q=&locationsearch=Gardermoen"


def sas_row(title, slug, place="Gardermoen, NO"):
    # the real tile repeats the title link for desktop and mobile layouts
    link = f'<a class="jobTitle-link" href="/job/{slug}/">{title}</a>'
    return (f'<li class="job-tile"><span class="sr-only">Title</span>{link}'
            f'<span>Locations</span><div>{place}</div>{link}</li>')


def sas_page(*rows):
    return f"<html><head><title>SAS jobs</title></head><body><a href='/'>Home page</a><ul>{''.join(rows)}</ul></body></html>"


def test_sas_role_in_title_alerts_once_despite_duplicate_links(monkeypatch):
    serve(monkeypatch, {SAS_URL: sas_page(
        sas_row("Servicemedarbeider sommer 2027", "Gardermoen-Servicemedarbeider-sommer/111"),
        sas_row("MAINTENANCE SUPPORT ENGINEER", "Gardermoen-MAINTENANCE-SUPPORT-ENGINEER/222"),
    )})
    result = jr.check(target("SAS"), set())
    assert [h.link for h in result.hits] == ["https://careers.sasgroup.net/job/Gardermoen-Servicemedarbeider-sommer/111/"]
    assert result.hits[0].key == "SAS Ground Handling|https://careers.sasgroup.net/job/Gardermoen-Servicemedarbeider-sommer/111/"
    assert result.listings == 2
    assert len(result.open) == 1


def test_sas_unwanted_roles_and_other_airports_stay_quiet(monkeypatch):
    calls = serve(monkeypatch, {SAS_URL: sas_page(
        sas_row("Lastemedarbeider rampe sommer", "Gardermoen-Lastemedarbeider/1"),
        sas_row("Supervisor Passenger Service", "Gardermoen-Supervisor/2"),
        sas_row("Servicemedarbeider check-in", "Bergen-Servicemedarbeider/3", place="Bergen, NO"),
        sas_row("Cargo agent", "Gardermoen-Cargo/4"),
        sas_row("Licensed Engineer B1", "Gardermoen-Engineer/5"),
    )})
    result = jr.check(target("SAS"), set())
    assert result.hits == [] and result.open == []
    assert result.listings == 5
    assert calls == [SAS_URL]  # no ad was opened: nothing had a detail trigger word


def test_sas_vague_title_is_resolved_by_opening_the_ad(monkeypatch):
    base = "https://careers.sasgroup.net/job/"
    serve(monkeypatch, {
        SAS_URL: sas_page(
            sas_row("Sommerjobb på flyplassen 2027", "Gardermoen-Sommerjobb/10"),
            sas_row("Sommerjobb i hangaren 2027", "Gardermoen-Hangar/11"),
            sas_row("Vikar til travel avdeling", "Gardermoen-Vikar/12"),
        ),
        base + "Gardermoen-Sommerjobb/10/": "<p>" + "Vi søker folk til innsjekk og gate. " * 20 + "</p>",
        base + "Gardermoen-Hangar/11/": "<p>" + "Du skal skifte hjul og bremser på fly. " * 20 + "</p>",
        base + "Gardermoen-Vikar/12/": requests.ConnectionError("down"),
    })
    result = jr.check(target("SAS"), set())
    texts = [h.text for h in result.hits]
    assert texts == [
        "Sommerjobb på flyplassen 2027",
        "Vikar til travel avdeling" + jr.UNREADABLE_TAG,  # unreadable ad still alerts, flagged
    ]


def test_seen_postings_stay_open_but_do_not_alert_again(monkeypatch):
    serve(monkeypatch, {SAS_URL: sas_page(sas_row("Servicemedarbeider", "Gardermoen-Servicemedarbeider/111"))})
    seen = {"SAS Ground Handling|https://careers.sasgroup.net/job/Gardermoen-Servicemedarbeider/111/"}
    result = jr.check(target("SAS"), seen)
    assert result.hits == []
    assert [o["text"] for o in result.open] == ["Servicemedarbeider"]


def test_layout_change_is_a_failure_not_silence(monkeypatch):
    serve(monkeypatch, {SAS_URL: "<html><body><a href='/about'>About us</a></body></html>"})
    with pytest.raises(jr.SourceError, match="layout probably changed"):
        jr.check(target("SAS"), set())


# ---------------------------------------------------------------- HTML: ISS

ISS_URL = "https://iss.attract.reachmee.com/jobs"


def iss_row(num, title, role, place, deadline="27.11.2026"):
    return (f'<li class="at-jobs-list-item"><h6><a href="https://iss.attract.reachmee.com/jobs/{num}-x">{title}</a></h6>'
            f"<div>{role}</div><div>{place}</div><div>{deadline}</div></li>")


def test_iss_matches_on_the_row_not_only_the_title(monkeypatch):
    ad = "https://iss.attract.reachmee.com/jobs/"
    rows = [
        iss_row(1, "Utforsk en meningsfull karriere ved Oslo Lufthavn!", "Assistansetjenesten", "Gardermoen", "18.11.2026"),
        iss_row(2, "Har du lyst på en aktiv arbeidshverdag?", "Renholdsmedarbeider", "Gardermoen"),
        iss_row(3, "Bli en del av PRM-teamet", "Assistanse", "Stjørdal"),
        iss_row(4, "Har du lyst på en aktiv arbeidshverdag på Oslo lufthavn?", "Servicemedarbeider", "Ullensaker"),
        iss_row(5, "Ny hverdag på Oslo lufthavn?", "Servicemedarbeider", "Ullensaker"),
    ]
    serve(monkeypatch, {
        ISS_URL: "<ul>" + "".join(rows) + "</ul>",
        ad + "4-x": "<p>" + "Du hjelper passasjerer med redusert mobilitet gjennom terminalen. " * 10 + "</p>",
        ad + "5-x": "<p>" + "Du fyller varer i kiosken på flyplassen hver dag hele uka. " * 10 + "</p>",
    })
    result = jr.check(target("ISS"), set())
    assert [h.text for h in result.hits] == [
        "Utforsk en meningsfull karriere ved Oslo Lufthavn! · frist 18.11.2026",   # role in the row
        "Har du lyst på en aktiv arbeidshverdag på Oslo lufthavn? · frist 27.11.2026",  # role only inside the ad
    ]
    assert result.listings == 5


# ------------------------------------------------------------ HTML: Widerøe

WID_URL = "https://jobbiwideroe.no/karriere-i-wideroe/bakkepersonell/"


def wid_row(num, title, place, deadline):
    href = f"https://www.jobbnorge.no/ledige-stillinger/stilling/{num}/x"
    return (f'<li class="jobs__item"><h3><a href="{href}">{title}</a></h3><p><span>Arbeidsgiver:</span> Widerøe Ground Handling</p>'
            f"<p><span>Arbeidssted:</span> {place}</p><p><span>Søknadsfrist:</span> {deadline}</p>"
            f'<a href="{href}"><span class="sr-only">Se stillingen</span></a></li>')


def test_wideroe_location_filter_and_deadline(monkeypatch):
    serve(monkeypatch, {WID_URL: "<ul>" + wid_row(1, "Flyekspeditør", "Florø lufthavn, Florø", "Søndag 15. november 2026")
                        + wid_row(2, "Flyekspeditør", "Oslo lufthavn, Gardermoen", "Fredag 6. november 2026") + "</ul>"})
    result = jr.check(target("Widerøe"), set())
    assert [(h.text, h.link) for h in result.hits] == [
        ("Flyekspeditør · frist Fredag 6. november 2026", "https://www.jobbnorge.no/ledige-stillinger/stilling/2/x"),
    ]
    assert result.listings == 2


# ---------------------------------------------------- HTML: generic options

def test_page_text_fallback_and_missing_sanity_href(monkeypatch):
    t = {"name": "Kiosk", "url": "https://example.test/jobs", "keywords": ["kundevert"]}
    serve(monkeypatch, {t["url"]: "<html><body><a href='/x'>Les mer her</a><p>Vi trenger en kundevert nå</p></body></html>"})
    result = jr.check(t, set())
    assert [h.key for h in result.hits] == ["Kiosk|pagetext|kundevert"]
    assert result.listings is None  # cannot count listings without a sanity_href
    assert jr.check(t, {"Kiosk|pagetext|kundevert"}).hits == []
    assert jr.check({**t, "page_text_fallback": False}, set()).hits == []


def test_unknown_target_type_is_reported():
    with pytest.raises(jr.SourceError, match="unknown target type"):
        jr.check({"name": "X", "type": "nope", "url": "u", "keywords": []}, set())


# ------------------------------------------------------------ Webcruiter (Vy)

VY_API = "https://candidate.webcruiter.com/api/odvert/companysearch/23151000"


def test_webcruiter_matches_heading_and_category(monkeypatch):
    ads = [
        {"Id": "1", "Heading": "Kundeveileder", "JobCategory": "Kundeservice", "Workplace": "Oslo",
         "OpenAdvertUrl": "https://vy.test/1", "ApplicationDeadline": "2026-08-26T00:00:00"},
        {"Id": "2", "Heading": "Bussjåfør i Uddevalla", "JobCategory": "Bussjåfør", "Workplace": "Uddevalla"},
        {"Id": "3", "Heading": "Bli med på laget", "JobCategory": "Stasjonsvert", "Workplace": "",
         "ApplicationDeadline": "0001-01-01T00:00:00"},
        {"Id": "4", "Heading": None, "JobCategory": None},
    ]
    serve(monkeypatch, {VY_API: Resp(data={"Data": ads})})
    result = jr.check(target("Vy"), {"Vy karriere|webcruiter|3"})
    assert [tuple(h) for h in result.hits] == [
        ("Kundeveileder (Oslo) · frist 26.08", "https://vy.test/1", "Vy karriere|webcruiter|1"),
    ]
    assert [o["text"] for o in result.open] == ["Kundeveileder (Oslo) · frist 26.08", "Bli med på laget · frist: snarest"]
    assert result.listings == 4


def test_webcruiter_api_change_is_a_failure(monkeypatch):
    serve(monkeypatch, {VY_API: Resp(data=["unexpected"])})
    with pytest.raises(jr.SourceError):
        jr.check(target("Vy"), set())


# ------------------------------------------------ SuccessFactors (Norwegian)

def sf_job(title, req, body, field="Ground Handling"):
    # copies the real feed, including the tag with an empty name that makes
    # it invalid XML
    return (f"<Job>\n<JobTitle><![CDATA[{title}]]></JobTitle>\n<Job-Description><![CDATA[<p>Location: [[mfield1]]</p>"
            f"<p>{body}</p>]]></Job-Description>\n<ReqId>{req}</ReqId>\n<>802</>\n<filter1>\n<label>Field of Work</label>\n"
            f"<value>{field}</value>\n</filter1>\n</Job>\n")


def sf_feed(*jobs):
    return '<?xml version="1.0" encoding="UTF-8"?>\n<Job-Listing>\n' + "".join(jobs) + "</Job-Listing>"


def nor():
    return target("Norwegian")


def test_norwegian_target_uses_the_feed_not_the_blocked_site():
    t = nor()
    assert t["type"] == "successfactors"
    assert "successfactors" in t["feed"] and "careers.norwegian.com" not in t["feed"]
    assert "{id}" in t["job_link"]


def test_successfactors_real_world_mix(monkeypatch):
    feed = sf_feed(
        sf_job("Lead Customer Service Agent, OSL", 3133, "Join our ground handling team at Oslo Airport (OSL)."),
        sf_job("Customer Service Agent ALC ( Alicante Airport)", 3134, "Join our team in Alicante."),
        sf_job("Operations Service Agent Copenhagen Airport", 3138, "Kastrup based."),
        sf_job("Ramp Agent, Oslo Airport", 3135, "Loading aircraft at Oslo Airport (OSL)."),
        sf_job("Cabin Crew Oslo", 3002, "Become cabin crew in Oslo!", field="Cabin Crew"),
        sf_job("Senior Advisor Operations Performance &amp; Analytics", 3142, "Based in Oslo.", field="Administration and Other roles"),
        sf_job("Team Leader Customer Service OSL", 3200, "Lead the team at OSL."),
    )
    calls = serve(monkeypatch, {nor()["feed"]: feed})
    result = jr.check(nor(), set())
    assert [tuple(h) for h in result.hits] == [(
        "Lead Customer Service Agent, OSL",
        "https://careers.norwegian.com/job-invite/3133/",
        "Norwegian (Red Handling)|successfactors|3133",
    )]
    assert result.listings == 7
    assert calls == [nor()["feed"]]  # exactly one request, nothing behind Cloudflare


def test_successfactors_seen_key_stops_repeat_alert(monkeypatch):
    serve(monkeypatch, {nor()["feed"]: sf_feed(sf_job("Customer Service Agent, OSL", 3133, "Oslo Airport."))})
    result = jr.check(nor(), {"Norwegian (Red Handling)|successfactors|3133"})
    assert result.hits == [] and len(result.open) == 1


def test_successfactors_location_never_fails_silently(monkeypatch):
    feed = sf_feed(
        sf_job("Customer Service Agent", 1, "Join our station team and help our passengers."),        # no place anywhere
        sf_job("Customer Service Agent Kristiansand", 2, "Join our station team."),                    # place we never listed
        sf_job("Customer Service Agent Bergen", 3, "Join our station team."),                          # known other place
        sf_job("Customer Service Agent", 4, "You will work at Gardermoen."),                           # place only in the ad
        sf_job("Customer Service Agent Oslo and Bergen", 5, "Two stations."),                          # ours wins over elsewhere
    )
    serve(monkeypatch, {nor()["feed"]: feed})
    texts = [h.text for h in jr.check(nor(), set()).hits]
    assert texts == [
        "Customer Service Agent" + jr.LOCATION_TAG,
        "Customer Service Agent Kristiansand" + jr.LOCATION_TAG,
        "Customer Service Agent",
        "Customer Service Agent Oslo and Bergen",
    ]


def test_successfactors_vague_title_checks_the_ad_body(monkeypatch):
    feed = sf_feed(
        sf_job("Summer season 2027 at OSL", 1, "You will work with check-in and gate at Oslo Airport."),
        sf_job("Summer season 2027 in the hangar OSL", 2, "You will change wheels and brakes."),
        sf_job("Join us at OSL", 3, "You will work with check-in and gate."),  # no trigger word: body not consulted
    )
    serve(monkeypatch, {nor()["feed"]: feed})
    assert [h.text for h in jr.check(nor(), set()).hits] == ["Summer season 2027 at OSL"]


def test_successfactors_broken_feed_is_a_failure(monkeypatch):
    serve(monkeypatch, {nor()["feed"]: "<html><body>Service unavailable</body></html>"})
    with pytest.raises(jr.SourceError, match="Job-Listing"):
        jr.check(nor(), set())


def test_successfactors_maintenance_page_is_named_as_such(monkeypatch):
    # what SAP really served on 13.11.2026: a redirect to an HTML page, status 200
    page = "<html><head><title>Maintenance</title></head><body>The SAP SuccessFactors HCM Suite is currently undergoing 2H 2026 Release Upgrade.</body></html>"
    serve(monkeypatch, {nor()["feed"]: page})
    with pytest.raises(jr.SourceError, match="planlagt vedlikehold"):
        jr.check(nor(), set())


def test_successfactors_empty_feed_is_valid_but_zero(monkeypatch):
    serve(monkeypatch, {nor()["feed"]: sf_feed()})
    result = jr.check(nor(), set())
    assert result.hits == [] and result.listings == 0  # the weekly mail flags the 0


# ------------------------------------------------------------------- fetch

def test_fetch_retries_once_on_server_error(monkeypatch):
    answers = [Resp(status=503), Resp("fine")]
    monkeypatch.setattr(jr.requests, "request", lambda *a, **k: answers.pop(0))
    assert jr.fetch("GET", "https://example.test").text == "fine"


def test_fetch_gives_up_on_403_without_hammering(monkeypatch):
    calls = []

    def blocked(*a, **k):
        calls.append(1)
        return Resp(status=403)

    monkeypatch.setattr(jr.requests, "request", blocked)
    with pytest.raises(requests.HTTPError) as err:
        jr.fetch("GET", "https://example.test")
    assert len(calls) == 1
    assert jr.short_error(err.value) == "HTTP 403 Forbidden"


def test_fetch_retries_then_raises_on_network_error(monkeypatch):
    calls = []

    def down(*a, **k):
        calls.append(1)
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(jr.requests, "request", down)
    with pytest.raises(requests.ConnectionError):
        jr.fetch("GET", "https://example.test")
    assert len(calls) == 2


# ------------------------------------------------------------ notifications

class GitHub:
    """Fake GitHub issues API."""

    BASE = "https://api.github.com/repos/someone/job-radar/issues"

    def __init__(self, monkeypatch, fail=False):
        self.created, self.closed, self.fail = [], [], fail
        self.comments = []          # (issue number, body)
        self.closed_by_user = set() # issue numbers you closed yourself
        self.fail_titles = ()       # issue titles containing one of these fail
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/job-radar")
        monkeypatch.setattr(jr.requests, "post", self.post)
        monkeypatch.setattr(jr.requests, "patch", self.patch)
        monkeypatch.setattr(jr.requests, "get", self.get)

    def post(self, url, headers=None, json=None, timeout=None):
        assert headers["Authorization"] == "Bearer t0ken"
        if self.fail:
            return Resp(status=403)
        if url.endswith("/comments"):
            self.comments.append((int(url.split("/")[-2]), json["body"]))
            return Resp(data={}, status=201)
        assert url == self.BASE
        if any(part in json["title"] for part in self.fail_titles):
            return Resp(status=403)
        self.created.append(json)
        return Resp(data={"number": 40 + len(self.created)}, status=201)

    def get(self, url, headers=None, timeout=None):
        if self.fail:
            return Resp(status=403)
        number = int(url.rsplit("/", 1)[1])
        return Resp(data={"number": number, "state": "closed" if number in self.closed_by_user else "open"})

    def patch(self, url, headers=None, json=None, timeout=None):
        assert json == {"state": "closed"}
        self.closed.append(url.rsplit("/", 1)[1])
        return Resp(data={})


def test_email_mode_prefers_smtp_and_never_doubles(monkeypatch):
    assert jr.email_mode({}) == ""
    GitHub(monkeypatch)
    assert jr.email_mode({}) == "issue"
    monkeypatch.setenv("SMTP_USER", "me@example.test")
    monkeypatch.setenv("SMTP_PASS", "app-password")
    assert jr.email_mode({}) == "smtp"


def test_hits_issue_format(monkeypatch):
    gh = GitHub(monkeypatch)
    entries = [
        {"target": "SAS Ground Handling", "text": "Servicemedarbeider (sommer)", "link": "https://x.test/job/A (1)/"},
        {"target": "ISS portal", "text": "PRM", "link": "https://y.test/2"},
    ]
    assert jr.send_hits_issue(entries) == 41
    issue = gh.created[0]
    assert issue["title"] == "🚨 MULIG TREFF: Servicemedarbeider (sommer) (+1 til)"
    assert issue["body"].startswith("@someone\n- [Servicemedarbeider (sommer)](https://x.test/job/A%20%281%29/) — SAS Ground Handling\n- [PRM](https://y.test/2) — ISS portal\n")
    assert gh.closed == []  # real hits stay open until you have triaged them


def test_notice_opens_and_closes_an_issue(monkeypatch):
    gh = GitHub(monkeypatch)
    assert jr.send_notice({}, "Jobbradar: X svarer ikke", "5 sjekker på rad har feilet.", "https://x.test") is True
    assert gh.created[0]["title"] == "Jobbradar: X svarer ikke"
    assert "https://x.test" in gh.created[0]["body"]
    assert gh.closed == ["41"]


def test_issue_failure_is_reported_not_raised(monkeypatch, capsys):
    GitHub(monkeypatch, fail=True)
    assert jr.create_issue("t", "b") is None
    assert "::error::GitHub issue failed: HTTP 403" in capsys.readouterr().out


def test_ntfy_push_payload(monkeypatch):
    sent = []
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    monkeypatch.setattr(jr.requests, "post", lambda url, json=None, timeout=None: sent.append((url, json)) or Resp())
    assert jr.send_direct({}, "POSSIBLE MATCH: SAS", "Servicemedarbeider", "https://x.test") is True
    assert sent[0][0] == "https://ntfy.sh"
    assert sent[0][1]["topic"] == "my-topic" and sent[0][1]["click"] == "https://x.test"
    assert jr.get_topic({"ntfy_topic": "CHANGE ME"}) == "my-topic"
    monkeypatch.delenv("NTFY_TOPIC")
    assert jr.get_topic({"ntfy_topic": "CHANGE ME - sett topic"}) == ""


def test_smtp_email_is_built_and_sent(monkeypatch):
    box = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            box.append((host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            box.append((user, password))

        def send_message(self, msg):
            box.append(msg)

    monkeypatch.setattr(jr.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "me@example.test")
    monkeypatch.setenv("SMTP_PASS", "pw")
    monkeypatch.setenv("ALERT_EMAIL", "to@example.test")
    assert jr.send_direct({}, "POSSIBLE MATCH: SAS", "Servicemedarbeider", "https://x.test") is True
    assert box[0] == ("smtp.gmail.com", 465) and box[1] == ("me@example.test", "pw")
    assert box[2]["To"] == "to@example.test" and box[2]["Subject"] == "POSSIBLE MATCH: SAS"
    assert "https://x.test" in box[2].get_content()


# ---------------------------------------------------------------- full runs

def fake_sources(monkeypatch, behaviour):
    """behaviour: {target name: CheckResult | Exception | callable}"""
    def fake_check(t, seen_keys, log=None, memory=None):
        b = behaviour[t["name"]]
        if callable(b):
            b = b(seen_keys)
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(jr, "check", fake_check)


def two_targets():
    return {"targets": [
        {"name": "Alpha", "url": "https://alpha.test/jobs", "keywords": ["x"]},
        {"name": "Beta", "url": "https://beta.test/jobs", "keywords": ["x"]},
    ]}


def hit(name, n):
    return jr.Hit(f"Job {n}", f"https://{name.lower()}.test/job/{n}", f"{name}|https://{name.lower()}.test/job/{n}")


def result_with(*hits, listings=3, deadline=None):
    return jr.CheckResult(list(hits), [jr.open_item(h.key, h.text, h.link, deadline=deadline) for h in hits], listings)


def state():
    return json.loads(jr.STATE_PATH.read_text(encoding="utf-8"))


def status():
    return json.loads(jr.STATUS_PATH.read_text(encoding="utf-8"))


def no_heartbeat():
    jr.STATE_PATH.write_text(json.dumps({"heartbeat_week": this_week()}), encoding="utf-8")


def this_week():
    iso = jr.now_utc().isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def test_run_opens_one_issue_per_job_and_saves_state(monkeypatch):
    gh = GitHub(monkeypatch)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1), hit("Alpha", 2)), "Beta": result_with(hit("Beta", 9))})
    assert jr.run_radar(two_targets()) == 0
    assert [i["title"] for i in gh.created] == ["🚨 MULIG TREFF: Job 1", "🚨 MULIG TREFF: Job 2", "🚨 MULIG TREFF: Job 9"]
    assert "Lukk denne saken når du er ferdig" in gh.created[0]["body"] and gh.closed == []
    assert set(state()["seen"]) == {hit("Alpha", 1).key, hit("Alpha", 2).key, hit("Beta", 9).key}
    st = status()
    assert st["channels"] == {"email": True, "push": False}
    assert [(t["id"], t["label"], t["sub"]) for t in st["targets"]] == [("alpha", "Alpha", ""), ("beta", "Beta", "")]
    assert [(t["name"], t["ok"], t["new_hits"], t["total_seen"], t["listings"]) for t in st["targets"]] == [
        ("Alpha", True, 2, 2, 3), ("Beta", True, 1, 1, 3)]
    assert [a["text"] for a in st["recent_alerts"]] == ["Job 9", "Job 2", "Job 1"]
    jobs = json.loads(jr.JOBS_PATH.read_text(encoding="utf-8"))["jobs"]
    assert {j["title"]: j["issue"] for j in jobs} == {"Job 1": 41, "Job 2": 42, "Job 9": 43}   # each job knows its thread


def test_a_flood_of_hits_shares_one_issue(monkeypatch):
    gh = GitHub(monkeypatch)
    no_heartbeat()
    many = [hit("Alpha", n) for n in range(jr.DIGEST_THRESHOLD + 1)]
    fake_sources(monkeypatch, {"Alpha": result_with(*many), "Beta": result_with()})
    jr.run_radar(two_targets())
    assert [i["title"] for i in gh.created] == [f"🚨 MULIG TREFF: Job 0 (+{len(many) - 1} til)"]
    assert len(state()["seen"]) == len(many)
    assert {j["issue"] for j in json.loads(jr.JOBS_PATH.read_text(encoding="utf-8"))["jobs"]} == {41}


def test_only_the_hit_whose_issue_failed_is_retried(monkeypatch):
    gh = GitHub(monkeypatch)
    gh.fail_titles = ("Job 2",)
    no_heartbeat()
    fake_sources(monkeypatch, {
        "Alpha": lambda seen: result_with(*[h for h in (hit("Alpha", 1), hit("Alpha", 2)) if h.key not in seen]),
        "Beta": result_with(),
    })
    jr.run_radar(two_targets())
    assert state()["seen"] == [hit("Alpha", 1).key]
    gh.fail_titles = ()
    jr.run_radar(two_targets())
    assert state()["seen"] == [hit("Alpha", 1).key, hit("Alpha", 2).key]
    assert [i["title"] for i in gh.created] == ["🚨 MULIG TREFF: Job 1", "🚨 MULIG TREFF: Job 2"]


def test_run_second_time_is_silent(monkeypatch):
    gh = GitHub(monkeypatch)
    no_heartbeat()
    fake_sources(monkeypatch, {
        "Alpha": lambda seen: result_with(*[h for h in [hit("Alpha", 1)] if h.key not in seen]),
        "Beta": result_with(),
    })
    jr.run_radar(two_targets())
    jr.run_radar(two_targets())
    assert len(gh.created) == 1
    assert status()["targets"][0]["new_hits"] == 0 and status()["targets"][0]["total_seen"] == 1


def test_undelivered_hits_are_retried_next_run(monkeypatch, capsys):
    gh = GitHub(monkeypatch, fail=True)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1)), "Beta": result_with()})
    jr.run_radar(two_targets())
    assert state()["seen"] == [] and state()["alerts"] == []
    assert "could not be delivered" in capsys.readouterr().out
    gh.fail = False
    jr.run_radar(two_targets())
    assert state()["seen"] == [hit("Alpha", 1).key]
    assert len(gh.created) == 1


def test_one_broken_target_never_stops_the_others(monkeypatch):
    gh = GitHub(monkeypatch)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": AttributeError("'NoneType' object has no attribute 'get'"), "Beta": result_with(hit("Beta", 9))})
    assert jr.run_radar(two_targets()) == 0
    alpha, beta = status()["targets"]
    assert alpha["ok"] is False and alpha["consecutive_failures"] == 1 and alpha["error"].startswith("AttributeError")
    assert beta["ok"] is True and beta["new_hits"] == 1
    assert len(gh.created) == 1


def test_failure_warning_is_really_delivered_and_recovery_too(monkeypatch):
    gh = GitHub(monkeypatch)
    no_heartbeat()
    blocked = requests.HTTPError("403 Client Error", response=Resp(status=403))
    fake_sources(monkeypatch, {"Alpha": blocked, "Beta": result_with()})
    for _ in range(jr.FAIL_WARN_THRESHOLD - 1):
        jr.run_radar(two_targets())
    assert gh.created == []
    jr.run_radar(two_targets())                      # failure number 5
    assert [i["title"] for i in gh.created] == ["Jobbradar: Alpha svarer ikke"]
    assert "HTTP 403 Forbidden" in gh.created[0]["body"] and gh.closed == ["41"]
    jr.run_radar(two_targets())                      # number 6: no repeat mail
    assert len(gh.created) == 1
    alpha = status()["targets"][0]
    assert alpha["consecutive_failures"] == 6 and alpha["error"] == "HTTP 403 Forbidden"

    fake_sources(monkeypatch, {"Alpha": result_with(), "Beta": result_with()})
    jr.run_radar(two_targets())
    assert [i["title"] for i in gh.created] == ["Jobbradar: Alpha svarer ikke", "Jobbradar: Alpha virker igjen"]
    assert status()["targets"][0]["consecutive_failures"] == 0


def test_failed_target_keeps_showing_last_known_open_jobs(monkeypatch):
    GitHub(monkeypatch)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1)), "Beta": result_with()})
    jr.run_radar(two_targets())
    fake_sources(monkeypatch, {"Alpha": requests.ConnectionError("down"), "Beta": result_with()})
    jr.run_radar(two_targets())
    alpha = status()["targets"][0]
    assert alpha["ok"] is False and [o["text"] for o in alpha["open"]] == ["Job 1"]


def test_many_hits_become_one_digest_push(monkeypatch):
    pushes = []
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    monkeypatch.setattr(jr.requests, "post", lambda url, json=None, timeout=None: pushes.append(json) or Resp())
    no_heartbeat()
    many = [hit("Alpha", n) for n in range(jr.DIGEST_THRESHOLD + 2)]
    fake_sources(monkeypatch, {"Alpha": result_with(*many), "Beta": result_with(hit("Beta", 1))})
    jr.run_radar(two_targets())
    assert [p["title"] for p in pushes] == [f"POSSIBLE MATCH: Alpha ({len(many)} treff)", "POSSIBLE MATCH: Beta"]
    assert len(state()["seen"]) == len(many) + 1
    assert status()["channels"] == {"email": False, "push": True}


def test_no_channel_at_all_still_records_hits(monkeypatch, capsys):
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1)), "Beta": result_with()})
    jr.run_radar(two_targets())
    assert state()["seen"] == [hit("Alpha", 1).key]
    assert "no notification channel configured" in capsys.readouterr().out


# ----------------------------------------------------------- weekly heartbeat

def report(name, ok=True, listings=5, fails=0):
    return {"name": name, "ok": ok, "listings": listings if ok else None, "consecutive_failures": fails}


def at(monkeypatch, y, m, d, hour=12):
    from datetime import datetime, timezone
    monkeypatch.setattr(jr, "now_utc", lambda: datetime(y, m, d, hour, tzinfo=timezone.utc))


def test_heartbeat_once_per_week_with_warnings(monkeypatch):
    gh = GitHub(monkeypatch)
    at(monkeypatch, 2026, 11, 17)  # Tuesday, ISO week 47
    rep = [report("Alpha", listings=2), report("Beta", listings=0), report("Gamma", ok=False, fails=7), report("Delta", listings=9)]
    week, counts = jr.weekly_heartbeat({}, rep, "2026-W46", {"Alpha": 10, "Beta": 4, "Gamma": 6, "Delta": 9})
    assert week == "2026-W47"
    assert counts == {"Alpha": 2, "Beta": 0, "Gamma": 6, "Delta": 9}  # failed target keeps last week's count
    issue = gh.created[0]
    assert issue["title"] == "Radar lever · uke 47 · SJEKK DETTE"
    assert "- Alpha: 2 annonser (forrige uke 10)" in issue["body"]
    assert "Alpha: falt fra 10 til 2 annonser" in issue["body"]
    assert "Beta: 0 annonser." in issue["body"]
    assert "- Gamma: FEIL (7 på rad)" in issue["body"]
    assert "- Delta: 9 annonser\n" in issue["body"]
    assert gh.closed == ["41"]
    # same week again: nothing
    assert jr.weekly_heartbeat({}, rep, week, counts) == (week, counts)
    assert len(gh.created) == 1


def test_heartbeat_waits_for_monday_morning_and_retries_if_undelivered(monkeypatch):
    gh = GitHub(monkeypatch)
    at(monkeypatch, 2026, 11, 16, hour=3)   # Monday 03 UTC: too early
    assert jr.weekly_heartbeat({}, [report("Alpha")], "2026-W46", {}) == ("2026-W46", {})
    assert gh.created == []
    at(monkeypatch, 2026, 11, 16, hour=7)
    gh.fail = True
    assert jr.weekly_heartbeat({}, [report("Alpha")], "2026-W46", {})[0] == "2026-W46"  # not marked sent
    gh.fail = False
    assert jr.weekly_heartbeat({}, [report("Alpha")], "2026-W46", {}) == ("2026-W47", {"Alpha": 5})
    assert gh.created[0]["title"] == "Radar lever · uke 47 · alt ser normalt ut"


# ------------------------------------------------------------- command line

def write_config(monkeypatch, tmp_path, cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(jr, "CONFIG_PATH", path)


def test_plain_local_run_touches_nothing(monkeypatch, tmp_path, capsys):
    write_config(monkeypatch, tmp_path, two_targets())
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1)), "Beta": requests.ConnectionError("down")})
    assert jr.main([]) == 1   # one source failed
    out = capsys.readouterr().out
    assert "Lokal forhåndsvisning" in out and "WOULD ALERT: Job 1" in out and "ERROR: fikk ikke kontakt" in out
    assert not jr.STATE_PATH.exists() and not jr.STATUS_PATH.exists()


def test_actions_run_and_write_flag_do_save(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, two_targets())
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(), "Beta": result_with()})
    assert jr.main(["--write"]) == 0
    assert jr.STATUS_PATH.exists()
    jr.STATUS_PATH.unlink()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert jr.main([]) == 0
    assert jr.STATUS_PATH.exists()


def test_dry_run_can_be_limited_to_one_target(monkeypatch, tmp_path, capsys):
    write_config(monkeypatch, tmp_path, two_targets())
    monkeypatch.setenv("DRY_RUN_TARGET", "bet")
    fake_sources(monkeypatch, {"Alpha": AssertionError("must not be checked"), "Beta": result_with(hit("Beta", 2))})
    assert jr.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "===== Beta =====" in out and "Alpha" not in out
    assert not jr.STATE_PATH.exists()


def test_test_alert_goes_through_the_hit_path_and_closes(monkeypatch, tmp_path):
    write_config(monkeypatch, tmp_path, two_targets())
    gh = GitHub(monkeypatch)
    assert jr.main(["--test"]) == 0
    assert gh.created[0]["title"].startswith("🚨 MULIG TREFF: TEST · ")
    assert gh.closed == ["41"]
    assert not jr.STATE_PATH.exists()


def test_test_alert_fails_loudly_when_nothing_delivers(monkeypatch, tmp_path, capsys):
    write_config(monkeypatch, tmp_path, two_targets())
    assert jr.main(["--test"]) == 1
    assert "NOT delivered" in capsys.readouterr().out


# ------------------------------------------------------------------- config

def test_real_config_is_well_formed():
    names = [t["name"] for t in CONFIG["targets"]]
    assert len(names) == len(set(names)) == 8
    assert len({t["id"] for t in CONFIG["targets"]}) == 8
    for t in CONFIG["targets"]:
        assert t["url"].startswith("https://") and t["keywords"]
        assert t.get("type") in (None, "webcruiter", "successfactors", "teamtailor", "nav")
        for field in ("keywords", "exclude_keywords", "location_must_contain", "location_elsewhere", "detail_trigger", "href_must_contain",
                      "municipals", "exclude_employers"):
            assert all(isinstance(s, str) and s.strip() for s in t.get(field, [])), (t["name"], field)
    # a place we want must never also be listed as "elsewhere"
    n = nor()
    assert not [e for e in n["location_elsewhere"] if any(w in e or e in w for w in n["location_must_contain"])]


# ------------------------------------------------------- deadlines as dates

@pytest.mark.parametrize("text, expected", [
    ("27.11.2026", "2026-11-27"),                           # ISS / ReachMee
    ("7.1.2027", "2027-01-07"),
    ("Søndag 15. november 2026", "2026-11-15"),             # Jobbnorge
    ("Fredag 6. NOVEMBER 2026", "2026-11-06"),
    ("1 mars 2027", "2027-03-01"),
    ("2026-08-26T00:00:00", "2026-08-26"),                  # Webcruiter
    ("2026-12-24T23:59:00+01:00", "2026-12-24"),
    ("0001-01-01T00:00:00", None),                          # Webcruiter's "no deadline"
    ("31.02.2026", None),
    ("snarest", None), ("", None), (None, None),
])
def test_parse_deadline(text, expected):
    assert jr.parse_deadline(text) == expected


def test_open_items_carry_key_title_and_deadline(monkeypatch):
    serve(monkeypatch, {WID_URL: "<ul>" + wid_row(2, "Flyekspeditør", "Oslo lufthavn, Gardermoen", "Fredag 6. november 2026") + "</ul>"})
    assert jr.check(target("Widerøe"), set()).open == [{
        "key": "Widerøe Ground Handling|https://www.jobbnorge.no/ledige-stillinger/stilling/2/x",
        "text": "Flyekspeditør · frist Fredag 6. november 2026",
        "title": "Flyekspeditør",
        "link": "https://www.jobbnorge.no/ledige-stillinger/stilling/2/x",
        "deadline": "2026-11-06",
    }]
    ads = [{"Id": "1", "Heading": "Kundeveileder", "Workplace": "Oslo", "OpenAdvertUrl": "https://vy.test/1",
            "ApplicationDeadline": "2026-08-26T00:00:00"},
           {"Id": "2", "Heading": "Kundevert", "ApplicationDeadline": "0001-01-01T00:00:00"}]
    serve(monkeypatch, {VY_API: Resp(data={"Data": ads})})
    vy = jr.check(target("Vy"), set()).open
    assert [(o["key"], o["title"], o["deadline"]) for o in vy] == [
        ("Vy karriere|webcruiter|1", "Kundeveileder (Oslo)", "2026-08-26"),
        ("Vy karriere|webcruiter|2", "Kundevert", None),
    ]
    serve(monkeypatch, {nor()["feed"]: sf_feed(sf_job("Customer Service Agent, OSL", 3133, "Oslo Airport."))})
    assert jr.check(nor(), set()).open == [{
        "key": "Norwegian (Red Handling)|successfactors|3133", "text": "Customer Service Agent, OSL",
        "title": "Customer Service Agent, OSL", "link": "https://careers.norwegian.com/job-invite/3133/", "deadline": None,
    }]


# ------------------------------------------------------------- job memory

T1, T2, T3 = "2026-11-10T08:00:00+00:00", "2026-11-11T08:00:00+00:00", "2026-11-12T08:00:00+00:00"
ALPHA, BETA = {"name": "Alpha"}, {"name": "Beta"}


def seen_open(name, *nums, deadline=None):
    return jr.CheckResult([], [jr.open_item(f"{name}|{n}", f"Job {n}", f"https://{name}.test/{n}", deadline=deadline) for n in nums], 5)


def states(jobs):
    return {j["key"]: j["state"] for j in jobs}


def test_jobs_open_close_and_reopen():
    jobs = jr.update_jobs([], [(ALPHA, seen_open("Alpha", 1, 2), "")], T1)
    assert states(jobs) == {"Alpha|1": "open", "Alpha|2": "open"}
    assert jobs[0]["first_seen"] == T1 and jobs[0]["closed_at"] is None

    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 2), "")], T2)      # job 1 is gone
    by = {j["key"]: j for j in jobs}
    assert states(jobs) == {"Alpha|1": "closed", "Alpha|2": "open"}
    assert by["Alpha|1"]["closed_at"] == T2 and by["Alpha|1"]["last_seen"] == T1
    assert by["Alpha|2"]["first_seen"] == T1 and by["Alpha|2"]["last_seen"] == T2

    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 1, 2), "")], T3)   # job 1 is back
    by = {j["key"]: j for j in jobs}
    assert by["Alpha|1"]["state"] == "open" and by["Alpha|1"]["closed_at"] is None
    assert by["Alpha|1"]["first_seen"] == T1


def test_jobs_failed_source_never_closes_anything():
    jobs = jr.update_jobs([], [(ALPHA, seen_open("Alpha", 1), ""), (BETA, seen_open("Beta", 7), "")], T1)
    jobs = jr.update_jobs(jobs, [(ALPHA, None, "HTTP 403 Forbidden"), (BETA, seen_open("Beta"), "")], T2)
    by = {j["key"]: j for j in jobs}
    assert by["Alpha|1"]["state"] == "open" and by["Alpha|1"]["last_seen"] == T1   # untouched
    assert by["Beta|7"]["state"] == "closed"


def test_jobs_keep_a_known_deadline_and_use_alert_time_as_first_seen():
    jobs = jr.update_jobs([], [(ALPHA, seen_open("Alpha", 1, deadline="2026-12-01"), "")], T2,
                          first_seen_hint={"https://Alpha.test/1": T1})
    assert jobs[0]["first_seen"] == T1 and jobs[0]["deadline"] == "2026-12-01"
    moved = jr.update_jobs([], [(ALPHA, seen_open("Alpha", 1), "")], T2, first_seen_hint={"Alpha|Job 1": T1})
    assert moved[0]["first_seen"] == T1   # same employer and title, new link
    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 1), "")], T3)      # deadline unreadable this time
    assert jobs[0]["deadline"] == "2026-12-01"


def test_jobs_history_is_capped_dropping_oldest_closed_first(monkeypatch):
    monkeypatch.setattr(jr, "MAX_JOB_HISTORY", 3)
    jobs = jr.update_jobs([], [(ALPHA, seen_open("Alpha", 1, 2, 3), "")], T1)
    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 2, 3), "")], T2)          # 1 closes first
    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 3, 4, 5), "")], T3)       # 2 closes, 4 and 5 are new
    # five known, room for three: both closed ones go, open jobs are never dropped
    assert states(jobs) == {"Alpha|3": "open", "Alpha|4": "open", "Alpha|5": "open"}

    monkeypatch.setattr(jr, "MAX_JOB_HISTORY", 4)
    jobs = jr.update_jobs([], [(ALPHA, seen_open("Alpha", 1, 2, 3), "")], T1)
    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 2, 3), "")], T2)
    jobs = jr.update_jobs(jobs, [(ALPHA, seen_open("Alpha", 3, 4, 5), "")], T3)
    assert states(jobs) == {"Alpha|2": "closed", "Alpha|3": "open", "Alpha|4": "open", "Alpha|5": "open"}  # the older closed one went


def test_jobs_ignore_garbage_in_the_old_file():
    jobs = jr.update_jobs([None, "x", {"no": "key"}, {"key": "Alpha|9", "target": "Alpha", "state": "open"}],
                          [(ALPHA, seen_open("Alpha", 1), "")], T1)
    assert states(jobs) == {"Alpha|9": "closed", "Alpha|1": "open"}


def test_run_writes_jobs_file_and_run_log(monkeypatch):
    GitHub(monkeypatch)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1), deadline="2026-12-01"), "Beta": result_with()})
    jr.run_radar(two_targets())
    data = json.loads(jr.JOBS_PATH.read_text(encoding="utf-8"))
    assert [(j["key"], j["state"], j["deadline"], j["target"]) for j in data["jobs"]] == [
        (hit("Alpha", 1).key, "open", "2026-12-01", "Alpha")]
    assert data["generated"] == status()["generated"]
    assert status()["runs"] == [status()["generated"]]

    fake_sources(monkeypatch, {"Alpha": result_with(), "Beta": requests.ConnectionError("down")})
    jr.run_radar(two_targets())
    data = json.loads(jr.JOBS_PATH.read_text(encoding="utf-8"))
    assert data["jobs"][0]["state"] == "closed"
    assert len(status()["runs"]) == 2


def test_run_log_is_capped(monkeypatch):
    monkeypatch.setattr(jr, "MAX_RUN_LOG", 3)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(), "Beta": result_with()})
    for _ in range(5):
        jr.run_radar(two_targets())
    assert len(status()["runs"]) == 3


def test_real_config_has_stable_ids_for_saved_notes():
    # the dashboard stores your notes per employer under these ids; changing one orphans the notes
    assert [t["id"] for t in CONFIG["targets"]][:5] == ["vy", "iss", "sas", "norwegian", "wideroe"]
    assert all(t.get("label") for t in CONFIG["targets"])


# ------------------------------------------------------ deadline reminders

from datetime import date as _date  # noqa: E402

TODAY = _date(2026, 11, 20)


def djob(n, deadline, reminded=(), issue=None, state="open"):
    job = {"key": f"Alpha|{n}", "target": "Alpha", "title": f"Job {n}", "text": f"Job {n}", "link": f"https://alpha.test/{n}",
           "deadline": deadline, "state": state, "reminded": list(reminded)}
    if issue:
        job["issue"] = issue
    return job


@pytest.mark.parametrize("deadline, reminded, expected", [
    ("2026-11-27", [], None),            # a week away: nothing yet
    ("2026-11-24", [], None),            # 4 days
    ("2026-11-23", [], (3, 3)),          # 3 days: first reminder
    ("2026-11-22", [], (2, 3)),          # run was skipped on day 3: still sent
    ("2026-11-22", [3], None),           # already reminded at this step
    ("2026-11-21", [3], (1, 1)),         # tomorrow: second reminder
    ("2026-11-21", [], (1, 1)),          # first seen late: only the last reminder
    ("2026-11-20", [3], (0, 1)),         # today, the day-before run was skipped
    ("2026-11-20", [3, 1], None),        # both sent
    ("2026-11-21", [1], None),
    ("2026-11-23", [1], None),           # deadline moved later after the last reminder: no step backwards
    ("2026-11-19", [], None),            # deadline passed
    (None, [], None), ("tull", [], None),
])
def test_due_reminders(deadline, reminded, expected):
    due = jr.due_reminders([djob(1, deadline, reminded)], TODAY)
    assert [(left, level) for _, left, level in due] == ([expected] if expected else [])


def test_no_reminder_for_closed_postings_or_jobs_alerted_this_run():
    jobs = [djob(1, "2026-11-21", state="closed"), djob(2, "2026-11-21")]
    assert jr.due_reminders(jobs, TODAY, skip_keys={"Alpha|2"}) == []
    assert len(jr.due_reminders(jobs, TODAY)) == 1


def test_reminder_is_a_comment_on_the_jobs_own_issue(monkeypatch):
    gh = GitHub(monkeypatch)
    jobs = [djob(1, "2026-11-21", issue=41)]
    assert jr.send_reminders({}, jobs, TODAY) == 1
    assert gh.created == []
    number, body = gh.comments[0]
    assert number == 41 and body.startswith("@someone ⏰ **Frist i morgen (21.11)**: [Job 1](https://alpha.test/1)")
    assert jobs[0]["reminded"] == [1]
    assert jr.send_reminders({}, jobs, TODAY) == 0 and len(gh.comments) == 1   # once


def test_closing_the_issue_stops_reminders(monkeypatch):
    gh = GitHub(monkeypatch)
    gh.closed_by_user = {41}
    jobs = [djob(1, "2026-11-21", issue=41), djob(2, "2026-11-23", issue=42)]
    assert jr.send_reminders({}, jobs, TODAY) == 1
    assert [n for n, _ in gh.comments] == [42]
    assert jobs[0]["reminded"] == []   # reopen the issue and the reminder comes after all


def test_reminder_for_a_job_without_its_own_issue_is_a_closed_notice(monkeypatch):
    gh = GitHub(monkeypatch)
    jobs = [djob(1, "2026-11-20")]
    assert jr.send_reminders({}, jobs, TODAY) == 1
    assert gh.created[0]["title"] == "⏰ FRIST I DAG (20.11): Job 1" and gh.closed == ["41"]


def test_undelivered_reminder_is_tried_again(monkeypatch):
    gh = GitHub(monkeypatch, fail=True)
    jobs = [djob(1, "2026-11-21", issue=41)]
    assert jr.send_reminders({}, jobs, TODAY) == 0 and jobs[0]["reminded"] == []
    gh.fail = False
    assert jr.send_reminders({}, jobs, TODAY) == 1 and jobs[0]["reminded"] == [1]


def test_reminder_push_is_quieter_than_a_hit_and_louder_than_housekeeping(monkeypatch):
    pushes = []
    monkeypatch.setenv("NTFY_TOPIC", "my-topic")
    monkeypatch.setattr(jr.requests, "post", lambda url, json=None, timeout=None: pushes.append(json) or Resp())
    jr.send_reminders({}, [djob(1, "2026-11-23")], TODAY)
    jr.send_direct({}, "POSSIBLE MATCH: SAS", "x")
    jr.send_notice({}, "Radar lever", "x")
    assert [(p["priority"], p["tags"]) for p in pushes] == [(4, ["alarm_clock"]), (5, ["rotating_light"]), (3, ["satellite"])]
    assert pushes[0]["title"] == "⏰ Frist om 3 dager (23.11): Job 1"


def test_run_reminds_on_a_later_run_not_with_the_first_alert(monkeypatch):
    gh = GitHub(monkeypatch)
    no_heartbeat()
    monkeypatch.setattr(jr, "today_oslo", lambda: TODAY)
    source = {"Alpha": lambda seen: jr.CheckResult(
        [h for h in [hit("Alpha", 1)] if h.key not in seen],
        [jr.open_item(hit("Alpha", 1).key, "Job 1", hit("Alpha", 1).link, deadline="2026-11-21")], 3), "Beta": result_with()}
    fake_sources(monkeypatch, source)
    jr.run_radar(two_targets())
    assert len(gh.created) == 1 and gh.comments == []          # the alert itself, no reminder on top
    jr.run_radar(two_targets())
    assert [n for n, _ in gh.comments] == [41]                 # next run: reminder on that job's issue
    jr.run_radar(two_targets())
    assert len(gh.comments) == 1
    assert json.loads(jr.JOBS_PATH.read_text(encoding="utf-8"))["jobs"][0]["reminded"] == [1]


# ----------------------------------------------------------- calendar feed

def test_calendar_has_one_all_day_event_per_open_job_with_a_deadline():
    jobs = [
        dict(djob(1, "2026-11-27"), first_seen="2026-11-10T08:00:00+00:00"),
        djob(2, None), djob(3, "2026-11-25", state="closed"),
        dict(djob(4, "2026-12-31"), title="Servicemedarbeider; sommer, 2027 " + "æøå " * 20),
    ]
    ics = jr.build_calendar(jobs, {"Alpha": "ALPHA · AS"})
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "\n" not in ics.replace("\r\n", "")                      # CRLF everywhere
    assert ics.count("BEGIN:VEVENT") == 2
    assert "DTSTART;VALUE=DATE:20261127\r\nDTEND;VALUE=DATE:20261128" in ics
    assert "DTSTAMP:20261110T080000Z" in ics
    assert "SUMMARY:Søknadsfrist: Job 1 (ALPHA · AS)" in ics
    assert "DTSTART;VALUE=DATE:20261231\r\nDTEND;VALUE=DATE:20270101" in ics   # year rollover
    assert all(len(line.encode("utf-8")) <= 75 for line in ics.split("\r\n"))   # folded
    unfolded = ics.replace("\r\n ", "")
    assert "SUMMARY:Søknadsfrist: Servicemedarbeider\\; sommer\\, 2027 æøå" in unfolded
    assert jr.build_calendar(jobs, {"Alpha": "ALPHA · AS"}) == ics       # stable: no churn in git


def test_run_writes_the_calendar(monkeypatch):
    GitHub(monkeypatch)
    no_heartbeat()
    fake_sources(monkeypatch, {"Alpha": result_with(hit("Alpha", 1), deadline="2026-12-01"), "Beta": result_with()})
    jr.run_radar(two_targets())
    ics = jr.CALENDAR_PATH.read_bytes().decode("utf-8")
    assert "DTSTART;VALUE=DATE:20261201" in ics and "\r\n" in ics
    assert status()["calendar"] is True


# ------------------------------------------------------- Avinor (Webcruiter)

AVINOR_API = "https://candidate.webcruiter.com/api/odvert/companysearch/2061"


def test_avinor_only_passenger_roles_at_oslo_airport(monkeypatch):
    ads = [
        {"Id": "1", "Heading": "Terminalvert Oslo lufthavn", "JobCategory": "Lufthavnoperativt", "Workplace": "Gardermoen",
         "OpenAdvertUrl": "https://avinor.test/1", "ApplicationDeadline": "2026-12-01T00:00:00"},
        {"Id": "2", "Heading": "Lufthavnbetjent", "JobCategory": "Lufthavnoperativt", "Workplace": "Hammerfest"},
        {"Id": "3", "Heading": "Kundeservice passasjerer", "JobCategory": "Annet", "Workplace": "Bergen"},          # wrong airport
        {"Id": "4", "Heading": "Leder kundeservice", "JobCategory": "Ledelse", "Workplace": "Gardermoen"},          # management
        {"Id": "5", "Heading": "Teknisk interessert? Bli VVS-tekniker på Norges viktigste flyplass!", "JobCategory": "Annet", "Workplace": "Gardermoen"},
        {"Id": "6", "Heading": "Bli flygeleder 2027", "JobCategory": "Annet", "Workplace": "Oslo"},
    ]
    serve(monkeypatch, {AVINOR_API: Resp(data={"Data": ads})})
    result = jr.check(target("Avinor"), set())
    assert [(h.text, h.key) for h in result.hits] == [("Terminalvert Oslo lufthavn (Gardermoen) · frist 01.12", "Avinor|webcruiter|1")]
    assert result.open[0]["deadline"] == "2026-12-01" and result.listings == 6


# -------------------------------------------------- Aviator (Teamtailor RSS)

def tt_item(title, guid, city, country, department="Ground Handling", body="Join us."):
    return (f"<item><title>{title}</title><description><![CDATA[<p>{body}</p>]]></description><pubDate>Tue, 10 Nov 2026 13:25:31 +0100</pubDate>"
            f"<link>https://careers.no.aviator.eu/jobs/{guid}</link><remoteStatus>none</remoteStatus><guid>{guid}</guid>"
            f"<tt:locations><tt:location><tt:name>Aviator {city}</tt:name><tt:city>{city}</tt:city><tt:country>{country}</tt:country></tt:location></tt:locations>"
            f"<tt:department>{department}</tt:department></item>")


def tt_feed(*items):
    return '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel><title>Jobs</title>' + "".join(items) + "</channel></rss>"


def test_aviator_feed_location_and_roles(monkeypatch):
    feed = tt_feed(
        tt_item("Passenger Service Agent", "g1", "Gardermoen", "Norway"),
        tt_item("Passenger Service Agent", "g2", "Stockholm-Arlanda", "Sweden"),
        tt_item("Flygplanslastare Arlanda", "g3", "Stockholm-Arlanda", "Sweden"),
        tt_item("Ramp Agent &amp; loader", "g4", "Gardermoen", "Norway"),
        tt_item("Sommerjobb på flyplassen 2027", "g5", "Oslo", "Norway", body="Du jobber med innsjekk og gate."),
        tt_item("Sommerjobb i hangaren 2027", "g6", "Oslo", "Norway", body="Du vasker fly."),
        tt_item("HR Specialist", "g7", "Oslo", "Norway", department="Admin"),
    )
    calls = serve(monkeypatch, {target("Aviator")["feed"]: feed})
    result = jr.check(target("Aviator"), set())
    assert [tuple(h) for h in result.hits] == [
        ("Passenger Service Agent (Gardermoen)", "https://careers.no.aviator.eu/jobs/g1", "Aviator|teamtailor|g1"),
        ("Sommerjobb på flyplassen 2027 (Oslo)", "https://careers.no.aviator.eu/jobs/g5", "Aviator|teamtailor|g5"),
    ]
    assert result.listings == 7 and calls == [target("Aviator")["feed"]]
    assert jr.check(target("Aviator"), {"Aviator|teamtailor|g1", "Aviator|teamtailor|g5"}).hits == []


def test_aviator_feed_that_is_not_rss_is_a_failure(monkeypatch):
    serve(monkeypatch, {target("Aviator")["feed"]: "<html><body>Not found</body></html>"})
    with pytest.raises(jr.SourceError, match="not RSS"):
        jr.check(target("Aviator"), set())


# --------------------------------------------------------- NAV (official feed)

NAV = "https://pam-stilling-feed.nav.no"


def nav_entry(uuid, title, employer="Firma AS", municipal="ULLENSAKER", status="ACTIVE", changed="2026-11-10T10:00:00+01:00"):
    return {"id": uuid, "_feed_entry": {"uuid": uuid, "status": status, "title": title, "businessName": employer,
                                        "municipal": municipal, "sistEndret": changed}}


def nav_page(page_id, entries, next_id=None):
    return Resp(data={"id": page_id, "next_id": next_id, "next_url": f"/api/v1/feed/{next_id}" if next_id else None, "items": entries})


def nav_detail(uuid, title, due="27.11.2026", expires="2099-01-01T00:00:00+01:00", status="ACTIVE", employer="Firma AS",
               city=None, text="Vi søker deg."):
    return Resp(data={"uuid": uuid, "status": status, "ad_content": {
        "title": title, "applicationDue": due, "expires": expires, "employer": {"name": employer},
        "description": f"<p>{text}</p>",
        "workLocations": [{"country": "NORGE", "address": None, "city": city, "postalCode": None, "county": "AKERSHUS", "municipal": "ULLENSAKER"}],
        "link": f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"}})


def nav_serve(monkeypatch, pages):
    """Like serve(), but records the headers too."""
    calls = []

    def fake_fetch(method, url, **kwargs):
        calls.append((url, kwargs.get("headers", {})))
        page = pages.get(url)
        if page is None:
            raise requests.ConnectionError(f"no fixture for {url}")
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(jr, "fetch", fake_fetch)
    monkeypatch.setenv("NAV_FEED_TOKEN", "my.own.token")
    return calls


def test_nav_first_run_filters_follows_pages_and_remembers(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    pages = {
        f"{NAV}/api/v1/feed": nav_page("p1", [
            nav_entry("a", "Passasjerservice på Oslo lufthavn", "Handling AS"),
            nav_entry("b", "Sykepleier 100 %", "Ullensaker kommune"),
            nav_entry("c", "Flyekspeditør innsjekk", "Handling AS", municipal="BERGEN"),                    # wrong place
            nav_entry("d", "Check-in agent Gardermoen", "Crew AS", municipal="OSLO"),                        # place named in the title
            nav_entry("e", "Passasjerservice sommer", "SAS Ground Handling Norway AS"),                      # followed directly already
            nav_entry("f", "Butikkselger med passasjerkontakt", "Kiosk AS"),                                 # excluded word
        ], next_id="p2"),
        f"{NAV}/api/v1/feed/p2": nav_page("p2", [
            nav_entry("g", "Flyekspeditør", "Fly AS"),
            nav_entry("a", "...", "", status="INACTIVE", changed="2026-11-12T10:00:00+01:00"),               # stopped later on
        ]),
        f"{NAV}/api/v1/feedentry/d": nav_detail("d", "Check-in agent Gardermoen", employer="Crew AS"),
        f"{NAV}/api/v1/feedentry/g": nav_detail("g", "Flyekspeditør", due="Snarest", employer="Fly AS"),
    }
    calls = nav_serve(monkeypatch, pages)
    memory = {}
    result = jr.check(target("NAV"), set(), memory=memory)
    assert [tuple(h) for h in result.hits] == [
        ("Check-in agent Gardermoen (Crew AS) · frist 27.11.2026", "https://arbeidsplassen.nav.no/stillinger/stilling/d", "NAV arbeidsplassen|nav|d"),
        ("Flyekspeditør (Fly AS) · frist Snarest", "https://arbeidsplassen.nav.no/stillinger/stilling/g", "NAV arbeidsplassen|nav|g"),
    ]
    assert [(o["title"], o["deadline"]) for o in result.open] == [("Check-in agent Gardermoen (Crew AS)", "2026-11-27"), ("Flyekspeditør (Fly AS)", None)]
    assert memory["cursor"] == "p2" and set(memory["open"]) == {"d", "g"}
    first_url, first_headers = calls[0]
    assert first_url == f"{NAV}/api/v1/feed" and first_headers["Authorization"] == "Bearer my.own.token"
    assert first_headers["If-Modified-Since"] == "Wed, 30 Sep 2026 12:00:00 GMT"     # 45 days back
    assert "If-Modified-Since" not in calls[1][1]


def test_nav_next_run_continues_from_the_bookmark(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    memory = {"cursor": "p2", "open": {"g": {"title": "Flyekspeditør", "employer": "Fly AS", "changed": "2026-11-10T10:00:00+01:00",
                                             "link": "https://arbeidsplassen.nav.no/stillinger/stilling/g", "due": "Snarest", "deadline": None,
                                             "expires": "2099-01-01T00:00:00+01:00", "detailed": True},
                                       "old": {"title": "Gate agent", "employer": "X", "changed": "c", "link": "l", "due": "", "deadline": None,
                                               "expires": "2026-11-01T00:00:00+01:00", "detailed": True}}}
    calls = nav_serve(monkeypatch, {
        f"{NAV}/api/v1/feed/p2": nav_page("p2", [nav_entry("g", "Flyekspeditør", "Fly AS"),                  # unchanged: not opened again
                                                   nav_entry("h", "Check-in agent OSL", "Gate AS", changed="2026-11-14T09:00:00+01:00")]),
        f"{NAV}/api/v1/feedentry/h": nav_detail("h", "Check-in agent OSL", due="2026-11-30", employer="Gate AS"),
    })
    result = jr.check(target("NAV"), {"NAV arbeidsplassen|nav|g"}, memory=memory)
    assert [h.key for h in result.hits] == ["NAV arbeidsplassen|nav|h"]
    assert [o["key"].rsplit("|", 1)[1] for o in result.open] == ["g", "h"]        # "old" expired and is gone
    assert [u for u, _ in calls] == [f"{NAV}/api/v1/feed/p2", f"{NAV}/api/v1/feedentry/h"]
    assert memory["cursor"] == "p2" and set(memory["open"]) == {"g", "h"}


def test_nav_reads_at_most_a_page_budget_then_continues_later(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    monkeypatch.setattr(jr, "NAV_MAX_PAGES", 2)
    calls = nav_serve(monkeypatch, {
        f"{NAV}/api/v1/feed": nav_page("p1", [], next_id="p2"),
        f"{NAV}/api/v1/feed/p2": nav_page("p2", [], next_id="p3"),
        f"{NAV}/api/v1/feed/p3": nav_page("p3", [nav_entry("z", "Flyekspeditør", "Fly AS")]),
        f"{NAV}/api/v1/feedentry/z": nav_detail("z", "Flyekspeditør"),
    })
    memory = {}
    assert jr.check(target("NAV"), set(), memory=memory).hits == []
    assert memory["cursor"] == "p3" and len(calls) == 2
    assert [h.key for h in jr.check(target("NAV"), set(), memory=memory).hits] == ["NAV arbeidsplassen|nav|z"]


def test_nav_failure_leaves_the_bookmark_untouched(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    nav_serve(monkeypatch, {
        f"{NAV}/api/v1/feed/p1": nav_page("p1", [nav_entry("z", "Gate agent", "Gate AS")], next_id="p2"),
        f"{NAV}/api/v1/feed/p2": requests.ConnectionError("down"),
    })
    memory = {"cursor": "p1", "open": {}}
    with pytest.raises(requests.ConnectionError):
        jr.check(target("NAV"), set(), memory=memory)
    assert memory == {"cursor": "p1", "open": {}}


def test_nav_ad_still_alerts_when_its_details_cannot_be_read(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    nav_serve(monkeypatch, {f"{NAV}/api/v1/feed": nav_page("p1", [nav_entry("z", "Flyekspeditør", "Fly AS")])})
    memory = {}
    result = jr.check(target("NAV"), set(), memory=memory)
    assert [tuple(h) for h in result.hits] == [("Flyekspeditør (Fly AS)", "https://arbeidsplassen.nav.no/stillinger/stilling/z", "NAV arbeidsplassen|nav|z")]
    assert memory["open"]["z"]["detailed"] is False        # tried again next run


def test_nav_api_change_is_a_failure(monkeypatch):
    nav_serve(monkeypatch, {f"{NAV}/api/v1/feed": Resp(data={"unexpected": True})})
    with pytest.raises(jr.SourceError, match="items"):
        jr.check(target("NAV"), set(), memory={})


def test_nav_dry_run_reads_a_recent_slice_with_the_public_token(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    calls = nav_serve(monkeypatch, {
        f"{NAV}/api/publicToken": Resp("Current public token for Nav Job Vacancy Feed:\neyJh.eyJz.l1WC"),
        f"{NAV}/api/v1/feed": nav_page("p1", []),
    })
    monkeypatch.delenv("NAV_FEED_TOKEN")
    assert jr.check(target("NAV"), set()).hits == []       # memory=None: a dry run
    assert calls[1][1]["Authorization"] == "Bearer eyJh.eyJz.l1WC"
    assert calls[1][1]["If-Modified-Since"] == "Sat, 07 Nov 2026 12:00:00 GMT"      # 7 days back


def test_run_keeps_each_sources_memory_between_runs(monkeypatch):
    GitHub(monkeypatch)
    no_heartbeat()
    seen_memory = []

    def source(t, seen_keys, log=None, memory=None):
        seen_memory.append(dict(memory))
        if t["name"] == "Alpha":
            memory["cursor"] = memory.get("cursor", 0) + 1
        return result_with()

    monkeypatch.setattr(jr, "check", source)
    jr.run_radar(two_targets())
    jr.run_radar(two_targets())
    assert seen_memory == [{}, {}, {"cursor": 1}, {}]
    assert state()["sources"] == {"Alpha": {"cursor": 2}}


def test_nav_everyday_words_only_count_at_the_airport(monkeypatch):
    at(monkeypatch, 2026, 11, 14)
    pages = {
        f"{NAV}/api/v1/feed": nav_page("p1", [
            nav_entry("k1", "Trives du med kundeservice? Bli vår nye kassemedarbeider!", "Adecco Karrieresenter"),   # really seen 14.11.2026
            nav_entry("k2", "Servicemedarbeider", "Handling AS"),
            nav_entry("k3", "Kundeservice på Oslo lufthavn", "Reise AS"),
            nav_entry("k4", "Servicemedarbeider", "Stille AS"),
        ]),
        f"{NAV}/api/v1/feedentry/k1": nav_detail("k1", "Trives du med kundeservice? Bli vår nye kassemedarbeider!", text="Dagligvarebutikk på Jessheim."),
        f"{NAV}/api/v1/feedentry/k2": nav_detail("k2", "Servicemedarbeider", employer="Handling AS", text="Du møter passasjerene i avgangshallen på Oslo lufthavn."),
        f"{NAV}/api/v1/feedentry/k3": nav_detail("k3", "Kundeservice på Oslo lufthavn", employer="Reise AS"),
        # k4: details unreadable this run
    }
    calls = nav_serve(monkeypatch, pages)
    memory = {}
    result = jr.check(target("NAV"), set(), memory=memory)
    assert [h.text for h in result.hits] == [
        "Servicemedarbeider (Handling AS) · frist 27.11.2026",          # the ad text places it at the airport
        "Kundeservice på Oslo lufthavn (Reise AS) · frist 27.11.2026",  # the title does
    ]
    assert memory["open"]["k1"]["rejected"] is True and memory["open"]["k4"]["pending"] is True

    # next run: the shop is not opened again, the unreadable one is tried again and now confirms
    pages[f"{NAV}/api/v1/feed/p1"] = pages[f"{NAV}/api/v1/feed"]
    pages[f"{NAV}/api/v1/feedentry/k4"] = nav_detail("k4", "Servicemedarbeider", employer="Stille AS", city="GARDERMOEN")
    del calls[:]
    seen = {h.key for h in result.hits}
    result = jr.check(target("NAV"), seen, memory=memory)
    assert [u.rsplit("/", 1)[1] for u, _ in calls] == ["p1", "k4"]
    assert [h.text for h in result.hits] == ["Servicemedarbeider (Stille AS) · frist 27.11.2026"]
    assert len(result.open) == 3
