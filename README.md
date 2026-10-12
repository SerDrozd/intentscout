# IntentScout

[![CI](https://github.com/SerDrozd/intentscout/actions/workflows/quality.yml/badge.svg)](https://github.com/SerDrozd/intentscout/actions/workflows/quality.yml)

Local-first discovery and review of technical hiring signals.

IntentScout is a Python CLI that searches public discussions for people who may need help with APIs, integrations, webhooks, or automation. It collects posts, checks whether they look like requests for paid work, and keeps the results in a local SQLite database for review.

The hard part is not finding posts that mention Python or an API. It is separating a possible client from someone asking a free technical question, advertising their own services, or hiring for work that is not a good fit.

IntentScout helps with that filtering. It does not contact anyone automatically, and a high score does not mean a job or budget has been confirmed.

## What it does

- Searches Hacker News, GitHub issues, Bluesky, selected SaaS communities, and configured RSS feeds.
- Matches technical terms alongside phrases that suggest someone is looking for help.
- Filters obvious service offers, general questions, and unsuitable hiring requirements.
- Ranks stored candidates by technical fit, commercial signals, and freshness.
- Offers optional LLM review and short action briefs for selected leads.
- Tracks decisions and outreach status locally so the same lead is not repeatedly treated as new.
- Supports optional Telegram alerts and read-only funnel diagnostics.

Social posts from platforms without a supported public collector can be added manually. IntentScout does not use private sessions or automate direct messages.

## Quick start

Requires Python 3.11+ and an internet connection for live collection.

**Windows PowerShell**

```powershell
git clone https://github.com/SerDrozd/intentscout.git
cd intentscout
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item .env.example .env
python -m lead_radar once
python -m lead_radar buyer-board --days 3 --limit 20
```

**macOS / Linux**

```bash
git clone https://github.com/SerDrozd/intentscout.git
cd intentscout
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
python -m lead_radar once
python -m lead_radar buyer-board --days 3 --limit 20
```

The first run creates `data/radar.db`. The example configuration enables several public collectors, so results depend on source availability and the current search window. An empty result does not necessarily mean there are no relevant requests.

LLM calls are disabled in `.env.example`, and Telegram credentials are blank. Neither is required for the basic workflow.

## Reviewing leads

The collection pipeline has three main stages:

1. Collect recent posts from configured sources and match technical terms.
2. Evaluate buyer intent, commercial language, and suitability. A seller's post or an ordinary support question should not become a high-priority lead just because it contains the right keywords.
3. Store results for review, with optional LLM classification and an action brief before any manual outreach.

The CLI distinguishes higher-priority candidates from lower-confidence records, but the classifications are estimates. Budget, competition, location requirements, and whether the request is still open must be checked at the original source.

Useful commands:

| Command | What it does |
| --- | --- |
| `python -m lead_radar once` | Run a single collection cycle |
| `python -m lead_radar run` | Keep collecting at configured intervals |
| `python -m lead_radar recent --limit 20` | Show recently stored matches |
| `python -m lead_radar buyer-board --days 3 --limit 20` | Review possible buyers |
| `python -m lead_radar shortlist --limit 20` | Show the current P0/P1 shortlist |
| `python -m lead_radar stats` | Show local database totals |
| `python -m lead_radar roi --days 30 --by source` | Inspect source-level outcomes |
| `python -m lead_radar alerts-plan` | Review suggested Google Alerts queries |

Run `python -m lead_radar --help` for the full CLI.

## Configuration

Settings live in the local `.env` file. The main switches include:

```dotenv
RADAR_DB=./data/radar.db
HN_ALGOLIA_ENABLED=true
GITHUB_ENABLED=true
MAKE_ENABLED=true
N8N_ENABLED=true
RSS_ENABLED=true
LLM_ENABLED=false
```

Source and keyword definitions are under `config/`. Personal Google Alerts URLs belong in `config/feeds.local.yaml`, which is excluded from Git. An example file is provided at `config/feeds.local.example.yaml`.

LLM review and Telegram notifications are optional. If enabling LLM calls, set your API credentials and model in `.env` and check the configured usage and price limits first. The application's cost estimates are not a substitute for provider billing data.

## Funnel diagnostics

IntentScout includes a read-only report for understanding what the collectors are actually finding:

```bash
python -m lead_radar.funnel_audit --db ./data/radar.db --hours 24 --limit 10
```

For JSON output:

```bash
python -m lead_radar.funnel_audit --db ./data/radar.db --hours 24 --format json
```

The report separates stored original topics from replies where source metadata allows it, summarizes activity by source, and surfaces a small set of records worth checking manually. It does not modify SQLite or call external APIs.

It can only examine records that reached the database. Posts rejected before storage remain outside the report. See [funnel diagnostic methodology](docs/funnel-diagnostic.md).

## Code layout

```text
lead_radar/
├── collectors/       Public-source adapters
├── app.py            Collection orchestration
├── matcher.py        Technical and intent matching
├── commercial.py     Buyer/seller and commercial filters
├── scoring.py        Relevance scoring
├── database.py       SQLite storage
├── llm.py            Optional LLM review
├── telegram.py       Optional notifications
└── funnel_audit.py   Read-only diagnostics

config/                Source and query definitions
tests/                 Unit and regression tests
docs/                  Notes on diagnostics and privacy
scripts/               Public-release checks
```

## Development

Install development dependencies and run the checks:

```bash
python -m pip install -e '.[dev]'
ruff check .
mypy lead_radar
python -m pytest -q
python -m compileall -q lead_radar
python scripts/check_public_release.py
python scripts/check_public_history.py
```

The tests use synthetic records and mocked network calls. GitHub Actions runs these checks on supported Python versions.

The repository retains the development commit history, with personal data removed from the public revisions. The two alternative historical branches are kept separately and are not meant to be merged into `main`. See [history and privacy](docs/history-and-privacy.md) for details.

## Data and security

The database, logs, API tokens, Telegram settings, and personal feed URLs stay on the machine running IntentScout. They are excluded from the public repository. The release checks look for known sensitive files and patterns in both the working tree and Git history, but they cannot detect every possible secret.

Use public endpoints in line with each service's terms. The tool does not send outreach messages or attempt to access private accounts.

## Current limitations

- A possible hiring signal is not a verified paid opportunity.
- Source coverage is incomplete, and public endpoints can fail or change without notice.
- Reply counts and other competition signals can become outdated.
- Strict filters may miss legitimate requests; the funnel report cannot measure everything rejected before storage.
- LLM judgments and suggested prices still need human review.

## License

No license has been assigned to this release.
