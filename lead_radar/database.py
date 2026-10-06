from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import ScoredOpportunity


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

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            total = conn.execute("SELECT COUNT(*) c FROM opportunities").fetchone()["c"]
            p0 = conn.execute(
                "SELECT COUNT(*) c FROM opportunities WHERE alert_class LIKE 'P0_%'"
            ).fetchone()["c"]
            p1 = conn.execute("SELECT COUNT(*) c FROM opportunities WHERE alert_class='P1'").fetchone()["c"]
            contacted = conn.execute(
                "SELECT COUNT(*) c FROM opportunities WHERE status='contacted'"
            ).fetchone()["c"]
        return {
            "total": int(total),
            "p0": int(p0),
            "p1": int(p1),
            "contacted": int(contacted),
        }
