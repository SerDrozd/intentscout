# Lead Radar v2.3 Phase 6B - Config-driven RSS/Atom discovery

Phase 6B adds a generic public RSS/Atom collector so new feed sources can be added in YAML without writing a new Python adapter. It keeps the same deterministic matcher, commercial scoring, SQLite dedupe, LLM precision gate and Telegram controls.

## Phase 6B adds

- RSS 2.0, Atom 1.0 and common RDF/RSS item parsing using the Python standard library;
- `config/feeds.yaml` for feed definitions;
- per-feed lookback, item caps, category kind and source labels;
- ETag / Last-Modified conditional requests;
- per-feed publication cursors with a small overlap window;
- per-feed minimum polling intervals so slow feeds are not hammered by the 3-minute radar cycle;
- bundled public Jobicy freelance/contract engineering feeds as the first marketplace RSS source;
- a disabled Google Alerts example that can be enabled locally by pasting a personal RSS URL;
- RSS marketplace items reuse the same buyer/seller/recruiter and LLM gates as existing communities.

No new API key is required for the bundled public feeds. Jobicy feeds are hard-limited in `feeds.yaml` to one request per feed per hour.

## New environment variables

```env
RSS_ENABLED=true
RSS_FEEDS_CONFIG=./config/feeds.yaml
RSS_CONCURRENCY=4
```

## Add another feed

Edit `config/feeds.yaml`:

```yaml
feeds:
  - key: my_feed
    source: my_source
    name: My buyer-intent feed
    url: "https://example.com/feed.xml"
    enabled: true
    category_kind: business_pain
    lookback_hours: 168
    max_items: 50
    min_poll_minutes: 15
```

Use `category_kind: marketplace` only for feeds where posts are actually hiring/contract/project listings.

## Verify Phase 6B

```powershell
ruff check .
mypy lead_radar
python -m pytest -q
python -m compileall -q lead_radar
git diff --check
python -m lead_radar once
```

A normal cycle now includes a line such as `rss fetched N item(s)`.

---

# Lead Radar v1.7 Phase 2 - Make + n8n Direct Buyer Sources

Phase 2 keeps the existing private/local Lead Radar and adds direct public Discourse adapters for the two most commercially useful automation communities in the current plan. No account or API key is required for these adapters.

## Phase 2 adds

- **Make Community** direct monitoring:
  - `Hire Help` marketplace;
  - `Questions` for production/business pain.
- **n8n Community** direct monitoring:
  - `Jobs`;
  - `Help me Build my Workflow`.
- public Discourse JSON ingestion with conservative retries and `Retry-After` handling;
- per-topic SQLite cursors so unchanged threads are not re-fetched every cycle;
- fresh reply events, including old threads that become active again;
- explicit `is_topic_author` metadata so buyer clarifications can be separated from service-provider replies;
- source-aware commercial scoring:
  - Make `Hire Help` topic starters are treated as buyer-oriented marketplace signals;
  - n8n `[For Hire]` / seller posts are rejected;
  - generic full-time employment posts are rejected from the money radar;
  - fresh OP replies such as `still looking`, `budget is`, `DM me`, or `contractor disappeared` can reactivate a thread;
- source-level failure isolation remains unchanged: one forum failure does not stop HN/GitHub or the other forum.

## New environment variables

```env
MAKE_ENABLED=true
MAKE_LOOKBACK_HOURS=48
MAKE_TOPICS_PER_CATEGORY=20
N8N_ENABLED=true
N8N_LOOKBACK_HOURS=48
N8N_TOPICS_PER_CATEGORY=20
DISCOURSE_CONCURRENCY=6
```

The category list is intentionally small. We are prioritizing buyer-heavy areas first instead of crawling every forum category.

## Verify Phase 2

```powershell
ruff check .
mypy lead_radar
python -m pytest -q
python -m compileall -q lead_radar
git diff --check
python -m lead_radar once
```

A normal cycle can now log collectors named `make` and `n8n` alongside `hackernews` and `github`.

---


A $0-first Python radar for finding fresh public technical pain / buying-intent signals before they become crowded freelance listings.

## What this MVP does

- Monitors **new Hacker News items including comments** via the official HN Firebase API.
- Searches recent public **GitHub Issues** using a small set of broad source queries.
- Runs every candidate through **250 exact phrases plus a compositional intent grammar** in `config/keywords.yaml`.
- Applies hard excludes, soft penalties, proximity-based paraphrase rules, source-aware commercial-intent scoring and deterministic ranking.
- Stores matches + exact triggering phrases in **SQLite**.
- Sends only the **top N high-score alerts per cycle** to Telegram (`MAX_ALERTS_PER_CYCLE=5` by default).
- Telegram buttons can mark an opportunity **Ignored** or **Contacted**, and the action is saved to SQLite.
- Works without Telegram: alerts are logged to the console and all matches remain in SQLite.

## v1.2 calibration changes

v1.1 was intentionally broad and a real replay showed it could classify ordinary GitHub engineering tickets as "HIGH INTENT". v1.2 tightens that behavior:

- rule matches require the technical anchor and pain/intent trigger to occur within 180 characters;
- generic words such as `production` or `client` no longer automatically count as strong buyer context;
- GitHub backlog/spec/task language receives a penalty;
- GitHub issues without an explicit buyer/help signal receive a commercial-intent penalty;
- alerts are sorted and capped per cycle;
- Telegram sends are spaced and `429 retry_after` is handled;
- Telegram HTTP errors are sanitized so the bot token is not printed in exception URLs.

The 250 exact phrases are unchanged.

## v1.4 compositional matching

v1.4 expands recall without adding 1,000 brittle full-sentence strings. It keeps the original 250 exact phrases and adds:

- **236 technical aliases/synonyms** across API, integrations, webhooks, OAuth/auth, sync/data, automation, scraping, backend/production and MVP/shipping;
- **311 intent/context aliases** for help, failures, build requests, questions, commercial intent, ownership, business context and manual-process pain;
- **order-independent proximity matching**: the technical term and intent/pain term may appear before or after each other;
- three-part context rules such as `our/we + webhook + failing`, `our/we + integration + trying to connect`, or `we + manual process + takes hours`;
- word-boundary matching so short tokens do not match inside unrelated words.

The pairwise grammar alone represents roughly **48,000 surface-form equivalents**, before three-part combinations are counted. It remains deterministic and requires no LLM, embeddings or paid API.

At startup the radar prints matcher coverage, for example:

```text
Matcher coverage: exact=250 technical_aliases=236 intent_aliases=311 pair_variant_equivalents=48380
```

## Requirements

- Python 3.11+ recommended
- Internet access
- Optional GitHub token (recommended for more comfortable API limits)
- Optional Telegram bot token/chat ID

## Install

```bash
cd technical-api-problem-radar-mvp
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
```

## First run (no tokens required)

GitHub public search and HN can run without credentials. Telegram is optional.

```bash
python -m lead_radar once
python -m lead_radar recent --limit 20
python -m lead_radar stats
```

The SQLite DB is created at `data/radar.db` by default.

## Continuous mode

```bash
python -m lead_radar run
```

Default poll interval is 180 seconds. Change `POLL_INTERVAL_SECONDS` in `.env`.

Telegram is deliberately rate-limited to at least ~1 message/second per chat and the client honors Bot API `retry_after` on 429 flood-control responses.

## GitHub token (recommended)

Put a token into:

```env
GITHUB_TOKEN=
```

The MVP only reads public issue-search results; it does not modify GitHub.

## Telegram setup

1. Create a bot with **@BotFather** and put its token in `.env`:

```env
TELEGRAM_BOT_TOKEN=
```

2. Send `/start` (or any message) to your new bot.
3. Run:

```bash
python -m lead_radar telegram-id
```

4. Copy the shown ID into:

```env
TELEGRAM_CHAT_ID=
```

5. Run `python -m lead_radar once` again.

High-score opportunities will now arrive with **Open / Ignore / Contacted** buttons. Callback actions are consumed on subsequent radar cycles.

## How matching works

The original 250 exact phrases are grouped into 10 categories:

- `explicit_help_hire`
- `api_failures`
- `integrations`
- `webhooks`
- `auth_oauth`
- `sync_data`
- `automation_workflows`
- `scraping_extraction`
- `backend_production`
- `ship_fix_mvp`

The source collectors are intentionally broad. Exact phrases and grammar aliases are **not** network searches: they are local classifier layers. v1.4 can recognize reordered/synonymous expressions such as `our webhook has been failing; could use a hand`, even when no original 250 phrase appears verbatim.

## Default scoring

Examples of positive signals:

- very fresh item: up to +6
- explicit willingness to pay: +7
- `need someone`: +6
- concrete technical problem: +3
- strong business/buyer context: +3
- urgency: +3
- strong technical fit: +4
- direct contact path: +1

Hard-excluded homework / interview-noise is discarded. Soft educational or already-solved signals get penalties.

Default alert threshold: **13**.

## SQLite tables

- `opportunities` — matched leads and scores
- `matches` — every exact phrase/category that triggered
- `source_state` — collector cursors + Telegram update offset
- `actions` — feedback such as `ignored` / `contacted`

Useful query:

```sql
SELECT m.phrase,
       COUNT(*) AS matches,
       SUM(CASE WHEN o.status='contacted' THEN 1 ELSE 0 END) AS contacted
FROM matches m
JOIN opportunities o ON o.id = m.opportunity_id
GROUP BY m.phrase
ORDER BY contacted DESC, matches DESC;
```

That is the foundation for learning which signals actually lead to conversations and money.

## Important MVP limitations

1. **GitHub is noisy.** v1.2 therefore separates technical fit from commercial intent: internal specs/backlog items get penalties and generic GitHub technical issues are stored but normally do not alert.
2. **HN initial bootstrap** scans only the latest configured item IDs (`HN_BOOTSTRAP_ITEMS=300`). Increase it if needed.
3. **No Reddit collector yet.** Reddit access is changing, so it should be an isolated adapter rather than a core dependency.
4. **No LLM classifier.** This is intentional. We first collect real false positives/positives and only add an LLM if deterministic rules leave a meaningful gap.
5. **No dashboard.** SQLite + Telegram are enough to validate whether the radar creates paid conversations.

## Recommended first validation

Run it continuously for several days and mark interesting items `Contacted` / bad ones `Ignored`. Then inspect which categories/phrases produce useful leads. Only after that add more sources or AI.

## v1.3 precision gate

v1.3 separates **technical relevance** from **commercial/contact intent**. A high score alone no longer sends Telegram alerts. GitHub requires an explicit money/contract signal for Telegram; Hacker News requires commercial intent or an owned first-person problem with a real request for help/business context. Job-post-like HN comments are suppressed. Rule matching also uses word boundaries, preventing false matches such as `import` inside `important` and `developer to` inside `developer tools`.

Cycle logs now include `intent_filtered=...`: items that scored above the threshold but were blocked by the commercial-intent gate. They remain stored in SQLite.


## v1.4 alert behavior

The high-precision v1.3 source gate remains. v1.4 broadens *recognition* rather than blindly lowering the Telegram threshold:

- Hacker News can alert on explicit help, owned build requests, owned business problems and manual-process pain.
- GitHub still requires a real money/contract/bounty signal for Telegram because ordinary GitHub issues are overwhelmingly project backlog.
- All technically relevant matches remain in SQLite even when the alert gate suppresses them.

This means adding synonyms increases recall without returning to the v1.1 flood of ordinary GitHub tickets.

## v1.5: buyer direction calibration

v1.5 keeps the v1.4 compositional matcher and its broad technical recall, but separates commercial direction from generic commercial vocabulary.

Key changes:

- seller/self-promotion posts such as `Open to contract work` and `SEEKING FREELANCE WORK` are rejected;
- generic `contract work`, `freelance work`, `paid work`, `day rate`, `hourly rate` and bare `bounty` are weak mentions, not buyer intent;
- GitHub alerts require directional buyer language such as `looking for a contractor`, `need a freelancer`, `willing to pay`, or a real budget/payment signal;
- completed/claimed bounty receipts are suppressed;
- manual-process alerts require stronger evidence such as repeated work, time cost, business ownership, or an automation request;
- technical matching coverage from v1.4 is unchanged.

This makes the architecture deliberately two-stage:

```text
technical relevance / recall
        ↓
buyer-vs-seller intent direction
        ↓
source-specific alert gate
        ↓
Telegram
```
## Phase 3: cheap LLM precision gate

Phase 3 adds an optional OpenAI structured classifier after deterministic commercial scoring.
It is deliberately a precision layer, not the primary scorer:

- deterministic hard rejects still win;
- the model may suppress a high-confidence false positive before Telegram;
- low-confidence model output fails open to deterministic scoring;
- the model does not promote weak P2 rows directly to P0;
- ingestion, SQLite storage and deterministic scoring continue if the API is unavailable or a budget is exhausted.

Current default model: `gpt-6-luna` via the Responses API with Structured Outputs.
The model and token prices are configuration values so they can be changed without code changes.

Local `.env` settings:

```env
LLM_ENABLED=true
OPENAI_API_KEY=
LLM_MODEL=gpt-6-luna
DAILY_LLM_BUDGET_USD=0.03
MONTHLY_LLM_BUDGET_USD=0.75
MAX_LLM_CANDIDATES_PER_HOUR=10
MAX_LLM_CANDIDATES_PER_DAY=50
LLM_BATCH_SIZE=5
MAX_POST_CHARS=5000
LLM_MAX_OUTPUT_TOKENS=1200
LLM_INPUT_PRICE_PER_MTOK=0.10
LLM_CACHED_INPUT_PRICE_PER_MTOK=0.01
LLM_CACHE_WRITE_PRICE_PER_MTOK=0.125
LLM_OUTPUT_PRICE_PER_MTOK=0.50
```

Never paste the API key into source code or commit it. `.env` is git-ignored.

Useful commands:

```powershell
python -m lead_radar shortlist --limit 20
python -m lead_radar llm-classify --limit 20
python -m lead_radar llm-stats
```

`llm-classify` only sends currently actionable deterministic P0/P1 rows that have not already been classified, unless `--force` is supplied. It stores a structured result and exact API token usage in SQLite.

A live P0 alert is automatically checked by the LLM gate when `LLM_ENABLED=true` and `OPENAI_API_KEY` is configured. P1 rows are also classified opportunistically within the same hard candidate and dollar caps, but they are not automatically sent to Telegram unless the normal deterministic alert policy allows it.

SQLite additions:

- `llm_classifications`: latest structured classification per opportunity;
- `llm_usage`: request, token and estimated-cost accounting;
- `opportunities.llm_decision`: `NOT_RUN`, `PASS`, `REVIEW`, or `REJECT`.

`reclassify` invalidates stale LLM decisions because deterministic evidence may have changed.

## Phase 4: action briefs and Telegram money cards

For a shortlisted lead, generate or reuse a compact action brief:

```powershell
python -m lead_radar prepare --id 2127
```

The brief contains:
- exact problem;
- why the task fits the developer's real Python/backend/API/automation skill set;
- money/payment signal;
- one bounded first milestone;
- suggested USD price band;
- contact strategy (`PUBLIC_REPLY`, `DM`, `EMAIL`, `PUBLIC_REPLY_THEN_DM`, `APPLY_FORM`, `IGNORE`);
- a short personalized English draft.

Drafts are validated deterministically. Em/en dashes are removed, and generic phrases such as `I came across your post`, `I am excited`, `I believe I would be a great fit`, `cutting-edge`, `seamless`, and `robust solutions` are rejected.

For a live P0 lead that survives the deterministic and LLM precision gates, Lead Radar tries to create the same brief before sending Telegram. If the OpenAI budget is exhausted or the API fails, ingestion continues and Telegram falls back to a shorter deterministic card rather than dropping the lead.

`python -m lead_radar llm-stats` now also includes `detailed_generations_today`.


## Phase 5: additional public buyer/pain communities

Enabled by default with no new credentials:

- `hubspot`: HubSpot Community -> Develop, Build & Integrate -> APIs & Integrations.
- `shopify`: Shopify Developer Community -> Webhooks & Events, Fulfillment & Inventory, Products & Orders APIs, Authentication & Access.
- `bubble`: Bubble Forum -> Jobs / Freelance.

All three reuse the existing public Discourse JSON collector and per-topic reply cursors. Non-author replies are stored for competition/context but cannot become interruptive buyer events. Bubble Jobs/Freelance is treated as a mixed marketplace: seller/self-promo and structured employment posts remain deterministic rejects. HubSpot/Shopify are business-pain sources and require buyer/business context before P0/P1.

Airtable and Zapier are intentionally not scraped in this phase. Their current community platforms do not expose the same stable public Discourse JSON contract; they should be added through the later generic web/RSS discovery adapter rather than a brittle one-off scraper.


## Phase 5A repair notes

- HubSpot `APIs & Integrations` uses the still-working canonical Discourse JSON path `c/hubspot-developer-support/apis-integrations/80.json`. The newer browser-facing category slug does not expose the same JSON endpoint reliably.
- Bubble `Jobs / Freelance` is low-volume, so its bootstrap lookback defaults to 168 hours (7 days). Normal freshness scoring still downranks older posts; replies after bootstrap remain event-driven by the per-topic cursor.

## Phase 5B: safe public-page community adapter

Some commercially useful communities do not expose a stable public JSON/RSS API. Phase 5B adds a conservative HTML adapter that uses ordinary public GET requests only: no login, cookies, CAPTCHA bypass, browser automation, or private endpoints.

Enabled sources:

- **HubSpot APIs & Integrations** through its public category/topic HTML because HubSpot's JSON endpoint returned HTTP 403 from the local collector;
- **Airtable Jobs Board** as a low-frequency buyer marketplace (14-day bootstrap window);
- **Zapier Code & Webhooks / Troubleshooting / How Do I** as business-pain sources (7-day bootstrap window).

The adapter discovers same-domain topic links, fetches only a bounded number of pages, extracts structured JSON-LD/meta/article content where available, requires a real publication timestamp, and stores only topic starters. Fresh reply monitoring remains on the dedicated Discourse adapters for Make/n8n/Shopify/Bubble; Phase 5B intentionally does not pretend generic HTML reply parsing is reliable.

```env
AIRTABLE_ENABLED=true
AIRTABLE_LOOKBACK_HOURS=336
AIRTABLE_TOPICS_PER_SECTION=20
ZAPIER_ENABLED=true
ZAPIER_LOOKBACK_HOURS=168
ZAPIER_TOPICS_PER_SECTION=15
WEB_FETCH_CONCURRENCY=4
```

## Phase 6A: HN targeted search + Bluesky public search

Phase 6A adds two no-credential discovery sensors while keeping the existing deterministic/LLM gates:

- `hn_algolia`: targeted Hacker News `search_by_date` queries over stories and comments, with per-query timestamp cursors and a five-minute overlap for late indexing. Opportunities are stored under the existing `hackernews` source so direct HN fetches still deduplicate against them.
- `bluesky`: public `app.bsky.feed.searchPosts` queries against the Bluesky AppView. Query families rotate across cycles and only fresh local-lookback results are emitted. No Bluesky account or token is required.

HubSpot public-page polling is disabled by default after repeatable HTTP 403 responses from the local client. It remains available behind `HUBSPOT_ENABLED=true` for future experiments, but it no longer wastes time or logs warnings in the normal loop.

## Phase 7A: feedback and ROI tracking

Lead Radar now stores collector run metrics and can track the sales outcome of each opportunity.
This turns the system from a one-way detector into a measurable acquisition loop.

Record actions from the CLI:

```powershell
python -m lead_radar lead-action --id YOUR_ID --action contacted --note "public reply"
python -m lead_radar lead-action --id YOUR_ID --action replied
python -m lead_radar lead-action --id YOUR_ID --action proposal
python -m lead_radar lead-action --id YOUR_ID --action won --revenue 250
```

Supported stages are `new`, `contacted`, `replied`, `proposal`, `won`, `lost`, and `ignored`.
Telegram alerts also expose `Replied` and `Won` buttons in addition to `Ignore` and `Contacted`.

Source-level ROI for the last 30 days:

```powershell
python -m lead_radar roi --days 30
```

Query/feed/category attribution:

```powershell
python -m lead_radar roi --days 30 --by query
python -m lead_radar roi --days 30 --by query --source bluesky
```

The report shows lead volume, P0/P1 counts, contacted/replied/proposal/won/lost/ignored stages,
recorded revenue, collector fetched volume (source view), and attributable LLM cost for requests
made after this phase was installed. Query attribution uses `query_family`, `broad_query`,
`feed_key`, or `category_key` from opportunity metadata.


### Undo accidental feedback

If a feedback command was run by mistake, remove the manually recorded funnel history and reset the lead to `new`:

```powershell
python -m lead_radar lead-action --id YOUR_ID --action reset --yes
```

`reset` only removes manual funnel actions/revenue for that opportunity. It preserves the opportunity, matches, LLM classification, lead brief, and notification history.

## Phase 7B: adaptive source/query advisor (dry-run only)

Lead Radar can now turn the Phase 7A funnel/ROI data into conservative source and query recommendations without changing any configuration, polling schedule, query rotation, or LLM budget.

Source-level advice:

```powershell
python -m lead_radar optimize --days 30
```

Query/feed/category advice:

```powershell
python -m lead_radar optimize --days 30 --by query
python -m lead_radar optimize --days 30 --by query --source github
```

Possible recommendations are:

- `BOOST` — strong downstream evidence; currently requires multiple wins or substantial reply/proposal evidence;
- `KEEP` — useful signal or a single win, but not enough evidence to increase allocation automatically;
- `WATCH` — mixed/weak discovery signal without enough negative evidence to reduce priority;
- `DEPRIORITIZE` — enough evidence that the segment consumes attention without producing useful signal;
- `DISABLE_CANDIDATE` — very large zero-signal sample; advisory only, never an automatic disable;
- `INSUFFICIENT_DATA` — fewer than 20 observed leads, regardless of an exciting individual outcome.

Safeguards are intentionally conservative:

- fewer than 20 observed leads always remains `INSUFFICIENT_DATA`;
- one recorded win can move a segment to `KEEP`, but cannot make it `BOOST`;
- replies/proposals/wins outweigh raw lead volume;
- large zero-P0/P1 samples can be deprioritized, but source-level disable advice requires a much larger sample than query-level advice;
- P0/P1 discovery yield alone can justify `KEEP`, not `BOOST`;
- the command is a pure advisor. It never rewrites `.env`, YAML, query lists, polling intervals, source enable flags, or LLM budgets.

Every run ends with an explicit dry-run message so recommendations cannot be confused with applied changes. Adaptive scheduling and automatic budget/query weighting should only be enabled later, after enough real `contacted -> replied -> proposal -> won/lost` outcomes have accumulated.

## Phase 8A: safe adaptive scheduling + Google Alerts query pack

Phase 7B remains the source of truth for recommendations, but continuous `run` mode can now use those recommendations as **reversible in-memory scheduling hints**. The scheduler never edits `.env`, YAML, source enable flags, or LLM budgets, and it never permanently disables a source/query.

Defaults:

```env
ADAPTIVE_SCHEDULING_ENABLED=true
ADAPTIVE_WINDOW_DAYS=30
ADAPTIVE_REFRESH_CYCLES=10
```

Preview the current runtime plan without collecting anything:

```powershell
python -m lead_radar schedule-plan
```

The conservative cadence mapping is:

- `BOOST`, `KEEP`, `INSUFFICIENT_DATA`: poll every cycle;
- `WATCH`: every 2 cycles;
- `DEPRIORITIZE`: every 3 cycles;
- `DISABLE_CANDIDATE`: every 8 cycles, never permanently disabled.

For query-driven collectors (`hn_algolia`, Bluesky, GitHub), the same advice also reduces or expands the number of queries run per scheduled cycle. Unknown or low-sample queries keep a normal exploration weight, so new query families are not starved before they gather evidence. Even a `DISABLE_CANDIDATE` query retains a non-zero exploration slot.

This behavior matters only inside a long-running process. A fresh `python -m lead_radar once` starts at cycle zero, so every enabled source still gets a chance to run; query-heavy sources may use the safer adaptive query budget immediately.

To return to the old fixed schedule at any time:

```env
ADAPTIVE_SCHEDULING_ENABLED=false
```

### Google Alerts query pack

`config/google_alerts_queries.yaml` contains buyer-intent alert suggestions, but Lead Radar does not create alerts or invent account-specific RSS URLs. Create an alert yourself, choose RSS delivery, then add the real feed URL to `config/feeds.yaml` using the existing RSS collector. No Python code is required.

## Phase 8B: buyer-signal Google Alerts overlay

Google Alerts can now be added without placing account-specific RSS URLs in tracked files. The tracked query pack lives in `config/google_alerts_queries.yaml`; real feed URLs are written to `config/feeds.local.yaml`, which is gitignored.

Preview the buyer-intent pack and current setup status:

```powershell
python -m lead_radar alerts-plan
```

After creating one alert in Google Alerts and selecting RSS delivery, add its real feed URL locally:

```powershell
python -m lead_radar alerts-add --key YOUR_QUERY_KEY --url "YOUR_REAL_GOOGLE_ALERTS_RSS_URL"
python -m lead_radar alerts-plan
```

The local RSS overlay is merged after `config/feeds.yaml`, so local entries can safely override a tracked template without editing or exposing the tracked file. Google Alerts opportunities carry `query_key` and `search_query` metadata, so `roi --by query` attributes downstream contact/reply/win data to the actual buyer-intent query instead of only to the feed filename.

`alerts-add` validates that the URL is HTTPS and points to a Google Alerts `/alerts/feeds/` path. It never creates an alert in the Google account and never commits the feed URL.

## Google Alerts precision guard (v0.12.0)

Google Alerts entries are now treated as discovery wrappers rather than authoritative page authorship. The RSS collector unwraps Google redirect URLs locally, preserves query attribution, and records the target host. The LLM classifier receives the target URL plus alert query context, so marketplace branding such as `- Freelancer` is not mistaken for a service-provider author. Google Alerts results without direct buyer language are capped at P2, which suppresses news/editorial matches while retaining direct help/hiring/project requests.
