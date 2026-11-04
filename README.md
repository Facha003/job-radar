# Job Radar

A small monitoring system that watches five employers' career sites every 30 minutes and emails me only when a job I actually want shows up: passenger facing airport roles (check in, gate, ticket and service desks, PRM assistance) at Oslo Airport Gardermoen, plus customer guide roles at Vy.

**Live dashboard:** https://facha003.github.io/job-radar/

![Dashboard](docs/screenshot.png)

## Why

Airport employers hire in short batches, often with deadlines of a few weeks, and the posting titles are vague ("Har du lyst på en aktiv arbeidshverdag?"). Generic job alerts either miss these or flood you with cleaning, ramp and management roles at other airports. This radar is built to be quiet unless something is genuinely relevant.

## How it works

```
GitHub Actions (cron, every 30 min)
  └─ job_radar.py
       ├─ scrapes each career site (requests + BeautifulSoup, Webcruiter JSON API for Vy)
       ├─ filters each listing row: location → exclusions → role keywords
       │    └─ vague titles: opens the ad and checks the role in the body
       ├─ new matches → GitHub issue → email notification
       ├─ weekly "radar alive" mail with live listing counts per company
       └─ commits seen.json + docs/status.json
GitHub Pages
  └─ docs/index.html reads status.json (departure board style)
```

| Target | Source | What counts as a match |
|---|---|---|
| SAS Ground Handling | SuccessFactors search, Gardermoen | Servicemedarbeider, check in, gate, desks |
| Norwegian (Red Handling) | SuccessFactors search, Gardermoen | Customer service, CSA, check in, gate, ticket desk |
| Widerøe Ground Handling | jobbiwideroe.no → Jobbnorge | Flyekspeditør, passenger roles at OSL |
| ISS | ReachMee portal | PRM assistance at Gardermoen, never cleaning |
| Vy | Webcruiter API | Kundeveileder and related roles |

## Design choices worth noting

**Filter on the whole listing row, not just the title.** ISS titles are marketing lines, but the row also prints the real role category ("Renholdsmedarbeider Stjørdal"). Matching on the row is what keeps cleaning jobs out.

**Prefer a noisy alert to a silent miss.** If an ad can't be read, the radar still alerts, tagged "check the role yourself". Missing the one right posting costs more than one extra email.

**Detect a quietly broken scraper.** Each target has a layout sanity check (no job links at all → failure, shown on the dashboard, warning after 5 in a row). The weekly mail also flags any company whose live listing count drops to 0 or halves since last week.

**Tested against real history.** The filter was replayed against real past postings (SAS summer temps Feb 2026, ISS PRM Feb 2026, Norwegian customer service OSL Mar 2024, plus ramp, cargo, engineering and office roles that must stay quiet). That replay found and fixed one real miss.

**No secrets needed.** Alerts go out as GitHub issues, so GitHub's own notification email does the delivery. SMTP and ntfy push are supported if configured as repository secrets.

## Running it

Everything runs in GitHub Actions. From the Actions tab, "job radar" can be started manually with:

| Input | Effect |
|---|---|
| `dry_run` | Scrape every site, print every listing and what would alert. Sends nothing, saves nothing |
| `dry_run_target` | Limit the dry run to one company |
| `heartbeat` | Send the weekly "radar alive" mail now |
| `test_alert` | Send a fake alert through the real email path |

Locally: `pip install -r requirements.txt` then `python job_radar.py --dry-run`.

Targets, keywords and exclusions live in `config.json`. Setup notes for GitHub Pages (Norwegian) are in `NETTSIDE_OPPSETT.md`.
