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
