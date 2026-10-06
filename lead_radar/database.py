from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import CommercialAssessment, ContactStrategy, LeadBrief, LLMClassification, ScoredOpportunity


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    score INTEGER NOT NULL,
    score_breakdown_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    content_hash TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    notified_at TEXT,
    author_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    commercial_intent TEXT NOT NULL DEFAULT 'NONE',
    opportunity_class TEXT NOT NULL DEFAULT 'STORE_ONLY',
    alert_class TEXT NOT NULL DEFAULT 'P2',
    cash_score INTEGER NOT NULL DEFAULT 0,
    value_score INTEGER NOT NULL DEFAULT 0,
    relationship_score INTEGER NOT NULL DEFAULT 0,
    freshness_score INTEGER NOT NULL DEFAULT 0,
    competition_score INTEGER NOT NULL DEFAULT 65,
    commercial_features_json TEXT NOT NULL DEFAULT '{}',
    llm_decision TEXT NOT NULL DEFAULT 'NOT_RUN',
    llm_confidence INTEGER,
    llm_classified_at TEXT,
    UNIQUE(source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_opportunities_score ON opportunities(score DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_created ON opportunities(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);

CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    phrase TEXT NOT NULL,
    match_type TEXT NOT NULL DEFAULT 'phrase'
);

CREATE INDEX IF NOT EXISTS idx_matches_opportunity ON matches(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_matches_phrase ON matches(phrase);

CREATE TABLE IF NOT EXISTS source_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    reason TEXT,
    revenue REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_classifications (
    opportunity_id INTEGER PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    decision TEXT NOT NULL,
    is_opportunity INTEGER NOT NULL,
    author_type TEXT NOT NULL,
    commercial_intent TEXT NOT NULL,
    payment_intent INTEGER NOT NULL,
    business_impact INTEGER NOT NULL,
    urgency INTEGER NOT NULL,
    technical_fit INTEGER NOT NULL,
    decision_maker_probability INTEGER NOT NULL,
    bounded_task INTEGER NOT NULL,
    should_contact INTEGER NOT NULL,
    confidence INTEGER NOT NULL,
    problem_summary TEXT NOT NULL,
    rejection_reason TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS lead_briefs (
    opportunity_id INTEGER PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    problem_summary TEXT NOT NULL,
    why_fit TEXT NOT NULL,
    money_signal TEXT NOT NULL,
    suggested_scope TEXT NOT NULL,
    suggested_price_band TEXT NOT NULL,
    contact_strategy TEXT NOT NULL,
    draft TEXT NOT NULL,
    confidence INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    response_id TEXT,
    candidate_count INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    estimated_cost_usd REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at DESC);
"""


COMMERCIAL_COLUMNS: dict[str, str] = {
    "author_type": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "commercial_intent": "TEXT NOT NULL DEFAULT 'NONE'",
    "opportunity_class": "TEXT NOT NULL DEFAULT 'STORE_ONLY'",
    "alert_class": "TEXT NOT NULL DEFAULT 'P2'",
    "cash_score": "INTEGER NOT NULL DEFAULT 0",
    "value_score": "INTEGER NOT NULL DEFAULT 0",
    "relationship_score": "INTEGER NOT NULL DEFAULT 0",
    "freshness_score": "INTEGER NOT NULL DEFAULT 0",
    "competition_score": "INTEGER NOT NULL DEFAULT 65",
    "commercial_features_json": "TEXT NOT NULL DEFAULT '{}'",
    "llm_decision": "TEXT NOT NULL DEFAULT 'NOT_RUN'",
    "llm_confidence": "INTEGER",
    "llm_classified_at": "TEXT",
}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_opportunities(conn)
            self._ensure_indexes(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _migrate_opportunities(conn: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(opportunities)").fetchall()
        }
        for name, definition in COMMERCIAL_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE opportunities ADD COLUMN {name} {definition}")

    @staticmethod
    def _ensure_indexes(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_cash ON opportunities(cash_score DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_value ON opportunities(value_score DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_opportunities_relationship ON opportunities(relationship_score DESC)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_alert_class ON opportunities(alert_class)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_opportunities_llm_decision ON opportunities(llm_decision)")

    @staticmethod
    def _content_hash(source: str, title: str, body: str) -> str:
        raw = f"{source}\n{title.strip()}\n{body.strip()}".encode("utf-8", errors="ignore")
        return hashlib.sha256(raw).hexdigest()

    def exists(self, source: str, external_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM opportunities WHERE source=? AND external_id=? LIMIT 1",
                (source, external_id),
            ).fetchone()
        return row is not None

    def insert(self, scored: ScoredOpportunity) -> int | None:
        o = scored.opportunity
        assessment = scored.assessment
        content_hash = self._content_hash(o.source, o.title, o.body)
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO opportunities (
                    source, external_id, url, title, body, author, created_at,
                    discovered_at, score, score_breakdown_json, content_hash, metadata_json,
                    author_type, commercial_intent, opportunity_class, alert_class,
                    cash_score, value_score, relationship_score, freshness_score,
                    competition_score, commercial_features_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    o.source,
                    o.external_id,
                    o.url,
                    o.title,
                    o.body,
                    o.author,
                    o.created_at.astimezone(timezone.utc).isoformat(),
                    scored.discovered_at.astimezone(timezone.utc).isoformat(),
                    scored.score,
                    json.dumps(scored.breakdown, ensure_ascii=False),
                    content_hash,
                    json.dumps(o.metadata, ensure_ascii=False),
                    assessment.author_type.value if assessment else "UNKNOWN",
                    assessment.commercial_intent.value if assessment else "NONE",
                    assessment.opportunity_class.value if assessment else "STORE_ONLY",
                    assessment.alert_class.value if assessment else "P2",
                    assessment.cash_score if assessment else 0,
                    assessment.value_score if assessment else 0,
                    assessment.relationship_score if assessment else 0,
                    assessment.freshness_score if assessment else 0,
                    assessment.competition_score if assessment else 65,
                    json.dumps(
                        {
                            "features": assessment.features,
                            "reasons": assessment.reasons,
                        }
                        if assessment
                        else {},
                        ensure_ascii=False,
                    ),
                ),
            )
            if cur.rowcount == 0 or cur.lastrowid is None:
                return None
            opportunity_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO matches (opportunity_id, category, phrase, match_type) VALUES (?, ?, ?, ?)",
                [
                    (opportunity_id, m.category, m.phrase, m.match_type)
                    for m in scored.analysis.phrase_matches
                ],
            )
            return opportunity_id

    def mark_notified(self, opportunity_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE opportunities SET notified_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), opportunity_id),
            )

    def set_status(self, opportunity_id: int, status: str, reason: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("UPDATE opportunities SET status=? WHERE id=?", (status, opportunity_id))
            conn.execute(
                "INSERT INTO actions (opportunity_id, action, reason, created_at) VALUES (?, ?, ?, ?)",
                (opportunity_id, status, reason, now),
            )

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM source_state WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO source_state(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, now),
            )


    def reclassification_rows(self, sources: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        with self.connect() as conn:
            if sources:
                placeholders = ",".join("?" for _ in sources)
                query = f"""
                    SELECT id, source, external_id, url, title, body, author, created_at, metadata_json
                    FROM opportunities
                    WHERE source IN ({placeholders})
                    ORDER BY id
                """
                return list(conn.execute(query, sources).fetchall())
            return list(
                conn.execute(
                    """
                    SELECT id, source, external_id, url, title, body, author, created_at, metadata_json
                    FROM opportunities
                    ORDER BY id
                    """
                ).fetchall()
            )

    def update_commercial_assessment(self, opportunity_id: int, assessment: CommercialAssessment) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE opportunities
                SET author_type=?, commercial_intent=?, opportunity_class=?, alert_class=?,
                    cash_score=?, value_score=?, relationship_score=?, freshness_score=?,
                    competition_score=?, commercial_features_json=?
                WHERE id=?
                """,
                (
                    assessment.author_type.value,
                    assessment.commercial_intent.value,
                    assessment.opportunity_class.value,
                    assessment.alert_class.value,
                    assessment.cash_score,
                    assessment.value_score,
                    assessment.relationship_score,
                    assessment.freshness_score,
                    assessment.competition_score,
                    json.dumps(
                        {"features": assessment.features, "reasons": assessment.reasons},
                        ensure_ascii=False,
                    ),
                    opportunity_id,
                ),
            )
            conn.execute(
                "UPDATE opportunities SET llm_decision='NOT_RUN', llm_confidence=NULL, llm_classified_at=NULL WHERE id=?",
                (opportunity_id,),
            )
            conn.execute("DELETE FROM llm_classifications WHERE opportunity_id=?", (opportunity_id,))

    def llm_candidate_rows(
        self,
        limit: int = 20,
        sources: tuple[str, ...] | None = None,
        *,
        force: bool = False,
    ) -> list[sqlite3.Row]:
        limit = max(1, min(limit, 200))
        params: list[str | int] = []
        source_clause = ""
        if sources:
            placeholders = ",".join("?" for _ in sources)
            source_clause = f" AND source IN ({placeholders})"
            params.extend(sources)
        llm_clause = "" if force else " AND llm_decision='NOT_RUN'"
        params.append(limit)
        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT id, source, external_id, url, title, body, author, created_at,
                           metadata_json, author_type, commercial_intent, opportunity_class,
                           alert_class, cash_score, value_score, relationship_score,
                           freshness_score, competition_score, llm_decision, llm_confidence
                    FROM opportunities
                    WHERE status='new'
                      AND (alert_class LIKE 'P0_%' OR alert_class='P1')
                      {source_clause}
                      {llm_clause}
                    ORDER BY
                        CASE WHEN alert_class LIKE 'P0_%' THEN 0 ELSE 1 END,
                        MAX(cash_score, value_score, relationship_score) DESC,
                        freshness_score DESC,
                        created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            )

    def llm_decisions(self, opportunity_ids: list[int]) -> dict[int, str]:
        if not opportunity_ids:
            return {}
        placeholders = ",".join("?" for _ in opportunity_ids)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT id, llm_decision FROM opportunities WHERE id IN ({placeholders})",
                opportunity_ids,
            ).fetchall()
        return {int(row["id"]): str(row["llm_decision"]) for row in rows}

    def save_llm_classification(
        self,
        classification: LLMClassification,
        *,
        model: str,
        schema_version: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "opportunity_id": classification.opportunity_id,
            "is_opportunity": classification.is_opportunity,
            "author_type": classification.author_type.value,
            "commercial_intent": classification.commercial_intent.value,
            "payment_intent": classification.payment_intent,
            "business_impact": classification.business_impact,
            "urgency": classification.urgency,
            "technical_fit": classification.technical_fit,
            "decision_maker_probability": classification.decision_maker_probability,
            "bounded_task": classification.bounded_task,
            "should_contact": classification.should_contact,
            "confidence": classification.confidence,
            "problem_summary": classification.problem_summary,
            "rejection_reason": classification.rejection_reason,
            "decision": classification.decision.value,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_classifications (
                    opportunity_id, model, schema_version, decision, is_opportunity,
                    author_type, commercial_intent, payment_intent, business_impact,
                    urgency, technical_fit, decision_maker_probability, bounded_task,
                    should_contact, confidence, problem_summary, rejection_reason,
                    raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id) DO UPDATE SET
                    model=excluded.model,
                    schema_version=excluded.schema_version,
                    decision=excluded.decision,
                    is_opportunity=excluded.is_opportunity,
                    author_type=excluded.author_type,
                    commercial_intent=excluded.commercial_intent,
                    payment_intent=excluded.payment_intent,
                    business_impact=excluded.business_impact,
                    urgency=excluded.urgency,
                    technical_fit=excluded.technical_fit,
                    decision_maker_probability=excluded.decision_maker_probability,
                    bounded_task=excluded.bounded_task,
                    should_contact=excluded.should_contact,
                    confidence=excluded.confidence,
                    problem_summary=excluded.problem_summary,
                    rejection_reason=excluded.rejection_reason,
                    raw_json=excluded.raw_json,
                    created_at=excluded.created_at
                """,
                (
                    classification.opportunity_id,
                    model,
                    schema_version,
                    classification.decision.value,
                    int(classification.is_opportunity),
                    classification.author_type.value,
                    classification.commercial_intent.value,
                    classification.payment_intent,
                    classification.business_impact,
                    classification.urgency,
                    classification.technical_fit,
                    classification.decision_maker_probability,
                    int(classification.bounded_task),
                    int(classification.should_contact),
                    classification.confidence,
                    classification.problem_summary,
                    classification.rejection_reason,
                    json.dumps(payload, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE opportunities
                SET llm_decision=?, llm_confidence=?, llm_classified_at=?
                WHERE id=?
                """,
                (
                    classification.decision.value,
                    classification.confidence,
                    now,
                    classification.opportunity_id,
                ),
            )

    def save_lead_brief(self, brief: LeadBrief, *, model: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO lead_briefs (
                    opportunity_id, model, problem_summary, why_fit, money_signal,
                    suggested_scope, suggested_price_band, contact_strategy, draft,
                    confidence, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(opportunity_id) DO UPDATE SET
                    model=excluded.model,
                    problem_summary=excluded.problem_summary,
                    why_fit=excluded.why_fit,
                    money_signal=excluded.money_signal,
                    suggested_scope=excluded.suggested_scope,
                    suggested_price_band=excluded.suggested_price_band,
                    contact_strategy=excluded.contact_strategy,
                    draft=excluded.draft,
                    confidence=excluded.confidence,
                    created_at=excluded.created_at
                """,
                (
                    brief.opportunity_id, model, brief.problem_summary, brief.why_fit,
                    brief.money_signal, brief.suggested_scope, brief.suggested_price_band,
                    brief.contact_strategy.value, brief.draft, brief.confidence, now,
                ),
            )

    def get_lead_brief(self, opportunity_id: int) -> LeadBrief | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM lead_briefs WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
        if row is None:
            return None
        return LeadBrief(
            opportunity_id=int(row["opportunity_id"]),
            problem_summary=str(row["problem_summary"]),
            why_fit=str(row["why_fit"]),
            money_signal=str(row["money_signal"]),
            suggested_scope=str(row["suggested_scope"]),
            suggested_price_band=str(row["suggested_price_band"]),
            contact_strategy=ContactStrategy(str(row["contact_strategy"])),
            draft=str(row["draft"]),
            confidence=int(row["confidence"]),
        )

    def opportunity_row(self, opportunity_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, source, external_id, url, title, body, author, created_at, metadata_json,
                       author_type, commercial_intent, opportunity_class, alert_class,
                       cash_score, value_score, relationship_score, freshness_score,
                       competition_score, llm_decision, llm_confidence
                FROM opportunities WHERE id=?
                """,
                (opportunity_id,),
            ).fetchone()

    def record_llm_usage(
        self,
        *,
        purpose: str,
        model: str,
        response_id: str | None,
        candidate_count: int,
        input_tokens: int,
        cached_input_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
        status: str = "ok",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO llm_usage (
                    purpose, model, response_id, candidate_count, input_tokens,
                    cached_input_tokens, cache_write_tokens, output_tokens,
                    estimated_cost_usd, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    purpose,
                    model,
                    response_id,
                    candidate_count,
                    input_tokens,
                    cached_input_tokens,
                    cache_write_tokens,
                    output_tokens,
                    estimated_cost_usd,
                    status,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def llm_usage_summary(self) -> dict[str, int | float]:
        now = datetime.now(timezone.utc)
        day_prefix = now.date().isoformat()
        month_prefix = now.strftime("%Y-%m")
        hour_cutoff = now.replace(minute=0, second=0, microsecond=0).isoformat()
        with self.connect() as conn:
            today = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0) input_tokens,
                       COALESCE(SUM(output_tokens), 0) output_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0.0) cost,
                       COALESCE(SUM(candidate_count), 0) candidates,
                       COUNT(*) requests
                FROM llm_usage
                WHERE created_at LIKE ? AND status='ok'
                """,
                (f"{day_prefix}%",),
            ).fetchone()
            month = conn.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0.0) cost,
                       COALESCE(SUM(candidate_count), 0) candidates,
                       COUNT(*) requests
                FROM llm_usage
                WHERE created_at LIKE ? AND status='ok'
                """,
                (f"{month_prefix}%",),
            ).fetchone()
            hour = conn.execute(
                """
                SELECT COALESCE(SUM(candidate_count), 0) candidates
                FROM llm_usage
                WHERE created_at >= ? AND status='ok'
                """,
                (hour_cutoff,),
            ).fetchone()
            details = conn.execute(
                """
                SELECT COALESCE(SUM(candidate_count), 0) candidates
                FROM llm_usage
                WHERE created_at LIKE ? AND status='ok' AND purpose='lead_brief'
                """,
                (f"{day_prefix}%",),
            ).fetchone()
        return {
            "input_tokens_today": int(today["input_tokens"]),
            "output_tokens_today": int(today["output_tokens"]),
            "llm_requests_today": int(today["requests"]),
            "classified_candidates_today": int(today["candidates"]),
            "classified_candidates_hour": int(hour["candidates"]),
            "estimated_cost_today": float(today["cost"]),
            "estimated_cost_month": float(month["cost"]),
            "llm_requests_month": int(month["requests"]),
            "classified_candidates_month": int(month["candidates"]),
            "detailed_generations_today": int(details["candidates"]),
        }

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT id, source, score, status, created_at, title, url,
                           author_type, commercial_intent, opportunity_class, alert_class,
                           cash_score, value_score, relationship_score
                    FROM opportunities
                    ORDER BY discovered_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )


    def shortlist(self, limit: int = 20, sources: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        """Return the highest-priority actionable P0/P1 opportunities.

        Shortlist is intentionally stricter than ``recent``: it only shows NEW
        opportunities that already passed the commercial classifier. This keeps
        ignored/contacted/history rows out of the immediate action queue.
        """
        limit = max(1, min(limit, 200))
        params: list[str | int] = []
        source_clause = ""
        if sources:
            placeholders = ",".join("?" for _ in sources)
            source_clause = f" AND source IN ({placeholders})"
            params.extend(sources)
        params.append(limit)

        with self.connect() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT id, source, status, created_at, discovered_at, title, url,
                           author_type, commercial_intent, opportunity_class, alert_class,
                           cash_score, value_score, relationship_score, freshness_score,
                           competition_score, llm_decision, llm_confidence,
                           MAX(cash_score, value_score, relationship_score) AS priority_score
                    FROM opportunities
                    WHERE status='new'
                      AND (alert_class LIKE 'P0_%' OR alert_class='P1')
                      AND llm_decision <> 'REJECT'
                      {source_clause}
                    ORDER BY
                        CASE WHEN alert_class LIKE 'P0_%' THEN 0 ELSE 1 END,
                        priority_score DESC,
                        freshness_score DESC,
                        competition_score DESC,
                        created_at DESC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
            )

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
            p0 = conn.execute(
                "SELECT COUNT(*) c FROM opportunities WHERE alert_class LIKE 'P0_%' AND llm_decision <> 'REJECT'"
            ).fetchone()["c"]
            p1 = conn.execute(
                "SELECT COUNT(*) c FROM opportunities WHERE alert_class='P1' AND llm_decision <> 'REJECT'"
            ).fetchone()["c"]
            llm_rejected = conn.execute(
                "SELECT COUNT(*) c FROM opportunities WHERE llm_decision='REJECT'"
            ).fetchone()["c"]
            contacted = conn.execute(
                "SELECT COUNT(*) c FROM opportunities WHERE status='contacted'"
            ).fetchone()["c"]
        return {
            "total": int(total),
            "p0": int(p0),
            "p1": int(p1),
            "contacted": int(contacted),
            "llm_rejected": int(llm_rejected),
        }
