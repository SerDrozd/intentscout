"""Regression coverage for the read-only, no-LLM commercial funnel diagnostics."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lead_radar.database import Database
from lead_radar.funnel_audit import examine_database, main, render_markdown

ANCHOR = datetime(2026, 10, 9, 7, 38, 30, tzinfo=timezone.utc)


def _date(hours_ago: int) -> str:
    return (ANCHOR - timedelta(hours=hours_ago)).isoformat()


def _insert(
    conn: sqlite3.Connection, *, key: str, source: str = "n8n", title: str = "Hiring Python developer",
    body: str = "Paid API webhook integration task. Need someone to fix this.",
    metadata: object = None, created_hours_ago: int = 1, discovered_hours_ago: int = 1,
    author_type: str = "BUYER", intent: str = "NONE", alert: str = "P2", decision: str = "NOT_RUN",
    status: str = "new",
) -> int:
    if metadata is None:
        metadata = {"event_type": "topic", "reply_count": 1}
    raw_meta = metadata if isinstance(metadata, str) else json.dumps(metadata)
    return int(conn.execute(
        """INSERT INTO opportunities (
               source, external_id, url, title, body, created_at, discovered_at,
               score, score_breakdown_json, content_hash, metadata_json,
               author_type, commercial_intent, alert_class, llm_decision, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, key, f"https://example.com/{key}", title, body,
         _date(created_hours_ago), _date(discovered_hours_ago), 10, "{}", key,
         raw_meta, author_type, intent, alert, decision, status),
    ).lastrowid)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "radar.db"
    Database(path)  # Schema initialization occurs ONLY in synthetic test fixtures.
    return path


def test_snapshot_separates_topics_replies_bumped_age_and_collectors(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _insert(conn, key="fresh")
        _insert(conn, key="old", created_hours_ago=80)
        _insert(conn, key="reply", metadata={"event_type": "reply"},
                alert="P1", decision="REJECT", author_type="SERVICE_PROVIDER")
        _insert(conn, key="assumed", metadata={"event_type": "topic", "created_at_assumed": True})
        _insert(conn, key="alert", source="google_alerts", metadata={"event_type": "topic"})
        _insert(conn, key="hn", source="hackernews", metadata={"type": "comment"})
        _insert(conn, key="invalid", source="make", metadata="not-json")
        _insert(conn, key="out-of-window", discovered_hours_ago=40)
        conn.executemany(
            """INSERT INTO collector_runs (source, fetched, matched, inserted, excluded, duration_ms,
               error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [("rss", 16, 3, 2, 0, 1, None, _date(1)),
             ("make", 0, 0, 0, 0, 100, None, _date(1)),
             ("make", 0, 0, 0, 0, 100, "Timeout", _date(1))],
        )
    report = examine_database(path, as_of=ANCHOR, hours=24, sample_limit=10)
    assert report.stored == 7
    assert report.malformed_metadata == 1
    sources = {s.source: s for s in report.sources}
    assert (sources["n8n"].stored, sources["n8n"].topics, sources["n8n"].replies) == (4, 3, 1)
    assert sources["n8n"].fresh_topics == 1  # Old bumped topic + assumed-time topic excluded.
    assert sources["n8n"].old_topics == 1
    assert sources["n8n"].buyer_like_fresh_topics == 1
    assert sources["n8n"].p0_p1 == 1
    assert sources["n8n"].llm_rejected == 1
    assert sources["hackernews"].replies == 1
    collectors = {c.source: c for c in report.collectors}
    assert "google_alerts" not in collectors  # Combined RSS collector, distinct persisted source.
    assert collectors["rss"].fetched == 16
    assert collectors["make"].calls == 2
    assert collectors["make"].errors == 1
    assert collectors["make"].empty_undetermined == 1
    assert [x.id for x in report.review_items] == [1, 5]
    assert "not confirmed buyers" in render_markdown(report)


def test_saved_ignore_past_contact_and_llm_reject_cannot_be_review_suggestions(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        ignored_id = _insert(conn, key="ignored")
        contacted_id = _insert(conn, key="contacted")
        _insert(conn, key="llm_reject", decision="REJECT")
        _insert(conn, key="seller", author_type="SERVICE_PROVIDER")
        _insert(conn, key="jobseeker", title="Available for hire: Python developer")
        _insert(conn, key="old", created_hours_ago=25)
        _insert(conn, key="priority", alert="P0_CASH")
        _insert(conn, key="weak-question", title="Webhook broken", body="How to fix API auth?")
        conn.execute(
            """INSERT INTO lead_briefs (
                opportunity_id, model, problem_summary, why_fit, money_signal,
                suggested_scope, suggested_price_band, contact_strategy, draft,
                confidence, created_at
            ) VALUES (?, 'm', '', '', '', '', '', 'IGNORE', '', 80, ?)""",
            (ignored_id, _date(1)),
        )
        conn.execute(
            "INSERT INTO actions(opportunity_id,action,created_at) VALUES(?,?,?)",
            (contacted_id, "contacted", _date(1)),
        )
    report = examine_database(path, hours=24, as_of=ANCHOR)
    assert report.review_items == []
    assert {s.source: s.contacted for s in report.sources}["n8n"] == 1


def test_read_only_no_side_effects_including_missing_db(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    report = examine_database(path, hours=24, as_of=ANCHOR)
    assert report.stored == 0
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == before_mtime
    with pytest.raises(ValueError, match="does not exist"):
        examine_database(tmp_path / "nonexistent.db")
    assert not (tmp_path / "nonexistent.db").exists()
    with pytest.raises(ValueError, match="between 1 and 720"):
        examine_database(path, hours=0)
    with pytest.raises(ValueError, match="between 0 and 30"):
        examine_database(path, sample_limit=31)


def test_cli_machine_readable_output_does_not_reveal_urls_or_body(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _insert(conn, key="private", title="Hiring Python developer for a webhook fix")
    assert main(["--db", str(path), "--as-of", ANCHOR.isoformat(), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["review_items"]) == 1
    assert data["review_items"][0]["title"].startswith("Hiring Python")
    assert "https://example.com" not in json.dumps(data)
    assert "Paid API webhook integration task" not in json.dumps(data)
    assert main(["--db", str(tmp_path / "missing.db")]) == 2
    assert "Audit error:" in capsys.readouterr().err


def test_invalid_database_schema_exits_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "not_radar.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE random_table (id INTEGER)")
    assert main(["--db", str(path)]) == 2
    assert "Missing required Radar tables:" in capsys.readouterr().err


def test_generic_job_ads_and_mandatory_local_restrictions_are_not_actionable_samples(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        _insert(conn, key="generic-job", author_type="UNKNOWN", intent="NONE",
                title="Hiring Software Developer for Python and AI integrations",
                body="Hiring a Software Developer, experienced in Python and webhooks.")
        _insert(conn, key="geo", author_type="UNKNOWN", intent="NONE",
                title="Hiring Backend Python contractor remotely",
                body="Paid API contract, UK right to work only, no sponsorship.")
        qualified_id = _insert(conn, key="buyer", author_type="POTENTIAL_BUYER", intent="IMPLICIT",
                               title="Need a Python freelancer for a paid webhook integration task")
    report = examine_database(path, as_of=ANCHOR)
    assert [item.id for item in report.review_items] == [qualified_id]
