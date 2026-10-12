# Funnel diagnostics: interpreting local observations

The `lead_radar.funnel_audit` module examines **only a pre-existing** SQLite database using SQLite `mode=ro` and `PRAGMA query_only=ON`.

## Counters and evidence levels

- **Fetched**: collector-level results, potentially including duplicates and replies.
- **Stored**: records accepted by pre-insertion logic. These are not all fetched records.
- **Original topic / reply**: derived from persisted source metadata when present. Unknown record kinds stay unknown.
- **Buyer-like**: an on-disk heuristic assessment, **not a paid client**.
- **P0/P1 / explicit intent**: recorded internal classifications, not proof of an available contract.
- **LLM rejects / contacted**: prior local decisions. They can be stale or incorrect.
- **Possible review candidates**: a bounded list of stored low-priority original topics; each still requires direct validation.

Do not divide unmatched source populations to produce a misleading conversion rate: `collector_runs` and per-item source fields can aggregate data differently.

## What the audit cannot answer

The tool cannot measure true recall against posts rejected *before* database insertion, verify current competition, or establish the existence of a budget. It makes no network requests, no paid LLM calls, and no database writes.

## Usage

```bash
python -m lead_radar.funnel_audit --db ./data/radar.db --hours 24 --limit 10
python -m lead_radar.funnel_audit --db ./data/radar.db --hours 72 --format json
```

Review JSON output for private metadata **before sharing**, even though the diagnostic deliberately limits raw content.
