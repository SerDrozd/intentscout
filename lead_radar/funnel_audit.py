"""Read-only commercial funnel diagnostics for an existing Radar SQLite database.

No application initialization, schema migrations, network calls, model calls, or writes.
The report describes *stored observations*, not verified paid opportunities or recall.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REQUIRED_TABLES = frozenset({"opportunities", "collector_runs", "actions", "lead_briefs"})
BUYER_TYPES = frozenset({"BUYER", "POTENTIAL_BUYER", "TECHNICAL_BUYER", "AGENCY_BUYER", "RECRUITER"})
SELLER_TYPES = frozenset({"SERVICE_PROVIDER", "JOB_SEEKER", "LEARNER"})
TECHNICAL_WORDS = re.compile(
    r"\b(?:python|api|webhooks?|integrations?|automati(?:on|ng)|n8n|make\.com|"
    r"workflows?|scripts?|backend|stripe|shopify|airtable|zapier|technical|developer)\b",
    re.IGNORECASE,
)
BUYER_WORDS = re.compile(
    r"\b(?:hiring|hire (?:a|an|someone)|looking for (?:a|an|someone|freelancer|developer)|"
    r"need (?:a|an|someone|freelancer|developer)|paid (?:task|project|work)|"
    r"seeking (?:a|an|developer|freelancer))\b",
    re.IGNORECASE,
)
PAYMENT_OR_SCOPE_WORDS = re.compile(
    r"\b(?:paid|budget|freelance|contract(?:or)?|fixed.price|one.off|short.term|"
    r"small task|quick fix|hourly|payment)\b", re.IGNORECASE,
)
MANDATORY_RESTRICTIONS = re.compile(
    r"\b(?:no sponsorship|right to work (?:only|required)|local applicants only|"
    r"onsite required|on.site required|must (?:be based|live|reside) in|"
    r"residents only|citizens only)\b", re.IGNORECASE,
)
SELLER_WORDS = re.compile(
    r"\b(?:available for hire|open to (?:work|collabs?|projects)|offering my services|"
    r"looking for (?:work|clients|gigs|projects)|hire me)\b",
    re.IGNORECASE,
)


@dataclass
class SourceTotals:
    source: str
    stored: int = 0
    topics: int = 0
    replies: int = 0
    other: int = 0
    fresh_topics: int = 0
    old_topics: int = 0
    buyer_like_fresh_topics: int = 0
    explicit_intent: int = 0
    p0_p1: int = 0
    llm_rejected: int = 0
    llm_review: int = 0
    contacted: int = 0
    ignored: int = 0


@dataclass
class CollectorTotals:
    source: str
    calls: int = 0
    fetched: int = 0
    matched: int = 0
    inserted: int = 0
    excluded: int = 0
    errors: int = 0
    empty_undetermined: int = 0


@dataclass
class ReviewItem:
    id: int
    source: str
    created_at: str
    title: str
    reason: str
    alert_class: str
    commercial_intent: str


@dataclass
class FunnelReport:
    as_of: str
    from_time: str
    hours: int
    stored: int = 0
    malformed_metadata: int = 0
    sources: list[SourceTotals] = field(default_factory=list)
    collectors: list[CollectorTotals] = field(default_factory=list)
    review_items: list[ReviewItem] = field(default_factory=list)
    caveats: list[str] = field(default_factory=lambda: [
        "Fetched collector events include repeats. They cannot be divided by stored rows to estimate buyer conversion.",
        "Collector 'rss' includes Google Alerts and other RSS feeds; it does not map 1:1 to persisted source names.",
        "Collector error=NULL does not prove source health: partial category/topic failures may be swallowed.",
        "Unmatched posts discarded before SQLite insertion cannot be examined here; false-negative recall is unknown.",
        "Buyer-like, commercial intent, P0/P1 and suggested review items are heuristics, not confirmed buyers or payment.",
        "Stored reply_count is a historical snapshot; verify live competition, open status and conditions before outreach.",
        "Feed dates can differ from original public post dates; fresh RSS items require direct source verification.",
        "Outreach counts are for leads discovered in the window, not actions performed during the window.",
    ])

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata(raw: object) -> tuple[dict[str, Any], bool]:
    try:
        payload: object = json.loads(str(raw))
    except (ValueError, TypeError):
        return {}, True
    if not isinstance(payload, dict):
        return {}, True
    return payload, False


def _kind(source: str, meta: dict[str, Any]) -> str:
    kind = meta.get("event_type")
    if kind in {"topic", "reply"}:
        return str(kind)
    if source == "hackernews" and meta.get("type") == "comment":
        return "reply"
    return "other"


def _review_reason(row: sqlite3.Row, meta: dict[str, Any], created: datetime | None,
                   start: datetime, end: datetime, kind: str, actions: set[str],
                   strategy: str | None) -> str | None:
    if kind != "topic" or created is None or not start <= created <= end:
        return None
    if meta.get("created_at_assumed") or str(row["status"]) != "new":
        return None
    if actions & {"contacted", "replied", "proposal", "won", "lost", "ignored"}:
        return None
    if strategy == "IGNORE" or str(row["llm_decision"]) == "REJECT":
        return None
    if str(row["alert_class"]).startswith("P0") or str(row["alert_class"]) == "P1":
        return None
    author_type = str(row["author_type"])
    if author_type in SELLER_TYPES:
        return None
    text = f'{row["title"]}\n{str(row["body"])[:1200]}'
    if SELLER_WORDS.search(text) or MANDATORY_RESTRICTIONS.search(text):
        return None
    buyer_words = bool(BUYER_WORDS.search(text))
    technical_words = bool(TECHNICAL_WORDS.search(text))
    if not buyer_words or not technical_words:
        return None
    # Unknown author + NONE intent + no payment/scope wording is usually
    # a generic career advertisement, not a small actionable contract.
    if (author_type not in BUYER_TYPES and str(row["commercial_intent"]) == "NONE"
            and not PAYMENT_OR_SCOPE_WORDS.search(text)):
        return None
    if author_type in BUYER_TYPES:
        return "buyer-classified, but not currently P0/P1; manual review only"
    if str(row["commercial_intent"]) in {"IMPLICIT", "EXPLICIT"}:
        return "commercial-intent heuristic; manual review only"
    return "buyer/technical wording despite low commercial score; manual review only"


def examine_database(path: Path, *, hours: int = 24, as_of: datetime | None = None,
                     sample_limit: int = 10) -> FunnelReport:
    """Inspect *existing* data in SQLite read-only mode. Never use the application's Database initializer."""
    if not 1 <= hours <= 720:
        raise ValueError("--hours must be between 1 and 720")
    if not 0 <= sample_limit <= 30:
        raise ValueError("--limit must be between 0 and 30")
    if not path.is_file():
        raise ValueError(f"Database does not exist: {path}")
    requested_time = as_of or datetime.now(timezone.utc)
    if requested_time.tzinfo is None:
        requested_time = requested_time.replace(tzinfo=timezone.utc)
    end = requested_time.astimezone(timezone.utc)
    start = end - timedelta(hours=hours)
    report = FunnelReport(as_of=end.isoformat(), from_time=start.isoformat(), hours=hours)

    # SQLite's URI read-only flag plus query_only prevent schema changes, migrations,
    # and all INSERT/UPDATE/DELETE attempts, even when a database contains old schema.
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        tables = {str(r["name"]) for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"Missing required Radar tables: {', '.join(sorted(missing))}")

        # Discovered-at is used throughout: old topics bumped today are not new topics.
        rows = connection.execute(
            """SELECT id, source, title, body, created_at, discovered_at, metadata_json,
                      author_type, commercial_intent, alert_class, llm_decision, status
               FROM opportunities WHERE discovered_at >= ? AND discovered_at <= ?""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        action_map: dict[int, set[str]] = {}
        for action in connection.execute(
            """SELECT a.opportunity_id, a.action FROM actions a
               JOIN opportunities o ON o.id = a.opportunity_id
               WHERE o.discovered_at >= ? AND o.discovered_at <= ?""",
            (start.isoformat(), end.isoformat()),
        ):
            action_map.setdefault(int(action["opportunity_id"]), set()).add(str(action["action"]))
        brief_map = {
            int(brief["opportunity_id"]): str(brief["contact_strategy"])
            for brief in connection.execute(
                """SELECT b.opportunity_id, b.contact_strategy FROM lead_briefs b
                   JOIN opportunities o ON o.id = b.opportunity_id
                   WHERE o.discovered_at >= ? AND o.discovered_at <= ?""",
                (start.isoformat(), end.isoformat()),
            )
        }
        source_map: dict[str, SourceTotals] = {}
        suggested: list[ReviewItem] = []
        for row in rows:
            source = str(row["source"])
            bucket = source_map.setdefault(source, SourceTotals(source=source))
            bucket.stored += 1
            meta, invalid = _metadata(row["metadata_json"])
            report.malformed_metadata += int(invalid)
            kind = _kind(source, meta)
            if kind == "topic":
                bucket.topics += 1
            elif kind == "reply":
                bucket.replies += 1
            else:
                bucket.other += 1
            created = _parse_time(row["created_at"])
            if kind == "topic" and created is not None and not meta.get("created_at_assumed"):
                if start <= created <= end:
                    bucket.fresh_topics += 1
                    if str(row["author_type"]) in BUYER_TYPES:
                        bucket.buyer_like_fresh_topics += 1
                elif created < start:
                    bucket.old_topics += 1
            if str(row["commercial_intent"]) == "EXPLICIT":
                bucket.explicit_intent += 1
            alert_class = str(row["alert_class"])
            if alert_class.startswith("P0") or alert_class == "P1":
                bucket.p0_p1 += 1
            llm_decision = str(row["llm_decision"])
            if llm_decision == "REJECT":
                bucket.llm_rejected += 1
            elif llm_decision == "REVIEW":
                bucket.llm_review += 1
            actions = action_map.get(int(row["id"]), set())
            if "contacted" in actions:
                bucket.contacted += 1
            if "ignored" in actions:
                bucket.ignored += 1
            reason = _review_reason(row, meta, created, start, end, kind,
                                    actions, brief_map.get(int(row["id"])))
            if reason is not None:
                safe_title = re.sub(r"[\x00-\x1f\x7f|]+", " ", str(row["title"]))[:130]
                suggested.append(ReviewItem(
                    id=int(row["id"]), source=source, created_at=str(row["created_at"]),
                    title=safe_title, reason=reason, alert_class=alert_class,
                    commercial_intent=str(row["commercial_intent"]),
                ))
        report.stored = len(rows)
        report.sources = sorted(source_map.values(), key=lambda b: (-b.stored, b.source))
        # Higher confidence hints first, then earliest original post (lower competition hypothesis).
        suggested.sort(key=lambda item: (
            item.reason.startswith("buyer/technical wording"),
            item.reason.startswith("commercial-intent"),
            item.created_at,
        ))
        report.review_items = suggested[:sample_limit]

        report.collectors = [CollectorTotals(
            source=str(item["source"]), calls=int(item["calls"]), fetched=int(item["fetched"]),
            matched=int(item["matched"]), inserted=int(item["inserted"]),
            excluded=int(item["excluded"]), errors=int(item["errors"]),
            empty_undetermined=int(item["empty_undetermined"]),
        ) for item in connection.execute(
            """SELECT source, COUNT(*) AS calls, SUM(fetched) AS fetched,
                      SUM(matched) AS matched, SUM(inserted) AS inserted,
                      SUM(excluded) AS excluded,
                      SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) AS errors,
                      SUM(CASE WHEN fetched = 0 AND error IS NULL THEN 1 ELSE 0 END)
                          AS empty_undetermined
               FROM collector_runs WHERE created_at >= ? AND created_at <= ?
               GROUP BY source ORDER BY fetched DESC, source ASC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()]
    return report


def render_markdown(report: FunnelReport) -> str:
    out = [
        "# Radar: read-only Commercial Funnel Diagnostic",
        f"Window: {report.from_time} to {report.as_of} ({report.hours}h)",
        f"Stored observations: {report.stored}. Malformed metadata: {report.malformed_metadata}.",
        "",
        "## Persisted source cohorts (not confirmed buyers)",
        "source | stored | topic | reply | other | fresh topics | buyer-like fresh | explicit intent | P0/P1 | LLM rejected | contacted",
        "--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---:",
    ]
    for s in report.sources:
        out.append(f"{s.source} | {s.stored} | {s.topics} | {s.replies} | {s.other} | "
                   f"{s.fresh_topics} | {s.buyer_like_fresh_topics} | {s.explicit_intent} | "
                   f"{s.p0_p1} | {s.llm_rejected} | {s.contacted}")
    if not report.sources:
        out.append("(No stored observations in this window)")
    out.extend([
        "", "## Collector health indicators (not unique posts)",
        "collector | runs | fetched | matched | inserted | excluded | raised errors | zero/no-error runs",
        "--- | ---: | ---: | ---: | ---: | ---: | ---: | ---:",
    ])
    for c in report.collectors:
        out.append(f"{c.source} | {c.calls} | {c.fetched} | {c.matched} | {c.inserted} | "
                   f"{c.excluded} | {c.errors} | {c.empty_undetermined}")
    if not report.collectors:
        out.append("(No collector-run records in this window)")
    out.extend(["", "## Stored low-priority items for HUMAN verification (not paid leads)"])
    if not report.review_items:
        out.append("No qualifying review candidates under this conservative rule. This does not establish zero false negatives.")
    for item in report.review_items:
        out.append(f"- #{item.id} [{item.source}] {item.title} (created {item.created_at}; "
                   f"{item.alert_class}, intent={item.commercial_intent}) — {item.reason}")
    out.extend(["", "## Important limitations"])
    out.extend(f"- {note}" for note in report.caveats)
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe, non-mutating commercial funnel audit (SQLite mode=ro)")
    parser.add_argument("--db", type=Path, default=Path("data/radar.db"), help="Existing Radar SQLite database")
    parser.add_argument("--hours", type=int, default=24, help="Hours to look back (1 to 720)")
    parser.add_argument("--as-of", help="Optional ISO 8601 timestamp; default: current UTC time")
    parser.add_argument("--limit", type=int, default=10, help="Max review candidates to display (0 to 30)")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    args = parser.parse_args(argv)
    as_of = _parse_time(args.as_of) if args.as_of is not None else None
    if args.as_of is not None and as_of is None:
        parser.error("--as-of must be an ISO 8601 timestamp, e.g. 2026-10-09T07:38:30+00:00")
    try:
        report = examine_database(args.db, hours=args.hours, as_of=as_of, sample_limit=args.limit)
    except (ValueError, sqlite3.DatabaseError, OSError) as exc:
        print(f"Audit error: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
