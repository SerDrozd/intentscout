from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from lead_radar.database import Database
from lead_radar.llm import OpenAILeadClassifier, decide_classification
from lead_radar.models import (
    AuthorType,
    CommercialIntent,
    LLMClassification,
    LLMDecision,
    Opportunity,
)


def _classification(**overrides: object) -> LLMClassification:
    values: dict[str, object] = {
        "opportunity_id": 1,
        "is_opportunity": True,
        "author_type": AuthorType.POTENTIAL_BUYER,
        "commercial_intent": CommercialIntent.IMPLICIT,
        "payment_intent": 45,
        "business_impact": 75,
        "urgency": 55,
        "technical_fit": 90,
        "decision_maker_probability": 70,
        "bounded_task": True,
        "should_contact": True,
        "confidence": 88,
        "problem_summary": "Meta leads are not reaching the intended workflow.",
        "rejection_reason": "",
        "decision": LLMDecision.REVIEW,
    }
    values.update(overrides)
    return LLMClassification(**values)  # type: ignore[arg-type]


def test_llm_decision_rejects_high_confidence_service_provider() -> None:
    item = _classification(
        is_opportunity=False,
        author_type=AuthorType.SERVICE_PROVIDER,
        commercial_intent=CommercialIntent.NONE,
        should_contact=False,
        confidence=94,
    )
    assert decide_classification(item) == LLMDecision.REJECT


def test_llm_decision_fails_open_on_low_confidence() -> None:
    item = _classification(
        is_opportunity=False,
        should_contact=False,
        confidence=52,
    )
    assert decide_classification(item) == LLMDecision.REVIEW


@pytest.mark.asyncio
async def test_openai_classifier_parses_structured_response_and_cost() -> None:
    result = {
        "classifications": [
            {
                "opportunity_id": 7,
                "is_opportunity": True,
                "author_type": "POTENTIAL_BUYER",
                "commercial_intent": "IMPLICIT",
                "payment_intent": 35,
                "business_impact": 82,
                "urgency": 60,
                "technical_fit": 91,
                "decision_maker_probability": 72,
                "bounded_task": True,
                "should_contact": True,
                "confidence": 89,
                "problem_summary": "Lead capture from WhatsApp is failing.",
                "rejection_reason": "",
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        payload = json.loads(request.content)
        assert payload["model"] == "gpt-6-luna"
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(result)}],
                    }
                ],
                "usage": {
                    "input_tokens": 1000,
                    "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 0},
                    "output_tokens": 100,
                },
            },
        )

    classifier = OpenAILeadClassifier(
        api_key="test-key",
        model="gpt-6-luna",
        max_post_chars=5000,
        max_output_tokens=1200,
        input_price_per_mtok=0.10,
        cached_input_price_per_mtok=0.01,
        cache_write_price_per_mtok=0.125,
        output_price_per_mtok=0.50,
        transport=httpx.MockTransport(handler),
    )
    opportunity = Opportunity(
        source="make",
        external_id="7",
        url="https://example.test/7",
        title="WhatsApp lead capture issue",
        body="Our leads are not reaching the workflow.",
        author="buyer",
        created_at=datetime.now(timezone.utc),
    )
    classifications, usage = await classifier.classify([(7, opportunity)])
    assert len(classifications) == 1
    assert classifications[0].decision == LLMDecision.PASS
    assert usage.response_id == "resp_test"
    assert usage.input_tokens == 1000
    assert usage.cached_input_tokens == 200
    assert usage.estimated_cost_usd == pytest.approx(0.000132)


def test_database_persists_llm_classification_usage_and_shortlist_filter(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO opportunities (
                source, external_id, url, title, body, author, created_at,
                discovered_at, score, score_breakdown_json, content_hash, metadata_json,
                author_type, commercial_intent, opportunity_class, alert_class,
                cash_score, value_score, relationship_score, freshness_score,
                competition_score, commercial_features_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "make",
                "one",
                "https://example.test/one",
                "Need API help",
                "Need help with an API integration",
                "buyer",
                now,
                now,
                10,
                "{}",
                "hash",
                "{}",
                "POTENTIAL_BUYER",
                "IMPLICIT",
                "HIGH_VALUE",
                "P1",
                60,
                74,
                54,
                80,
                90,
                "{}",
            ),
        )
        opportunity_id = int(cur.lastrowid or 0)

    rejected = _classification(
        opportunity_id=opportunity_id,
        is_opportunity=False,
        author_type=AuthorType.SERVICE_PROVIDER,
        commercial_intent=CommercialIntent.NONE,
        should_contact=False,
        confidence=95,
    )
    rejected.decision = decide_classification(rejected)
    db.save_llm_classification(rejected, model="gpt-6-luna", schema_version="test")
    db.record_llm_usage(
        purpose="test",
        model="gpt-6-luna",
        response_id="resp_1",
        candidate_count=1,
        input_tokens=500,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=80,
        estimated_cost_usd=0.00009,
    )

    assert db.shortlist(20) == []
    usage = db.llm_usage_summary()
    assert usage["classified_candidates_today"] == 1
    assert usage["llm_requests_today"] == 1
    assert usage["estimated_cost_today"] == pytest.approx(0.00009)


def test_reclassification_invalidates_stale_llm_decision(tmp_path) -> None:
    from lead_radar.models import AlertClass, CommercialAssessment, OpportunityClass

    db = Database(tmp_path / "radar.db")
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO opportunities (
                source, external_id, url, title, body, author, created_at,
                discovered_at, score, score_breakdown_json, content_hash, metadata_json,
                llm_decision, llm_confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "github",
                "two",
                "https://example.test/two",
                "Internal issue",
                "Spec",
                "maintainer",
                now,
                now,
                5,
                "{}",
                "hash2",
                "{}",
                "REJECT",
                95,
            ),
        )
        opportunity_id = int(cur.lastrowid or 0)

    assessment = CommercialAssessment(
        author_type=AuthorType.OSS_MAINTAINER,
        commercial_intent=CommercialIntent.NONE,
        opportunity_class=OpportunityClass.STORE_ONLY,
        alert_class=AlertClass.P2,
        cash_score=18,
        value_score=30,
        relationship_score=16,
        freshness_score=80,
        competition_score=65,
        features={},
        reasons=[],
    )
    db.update_commercial_assessment(opportunity_id, assessment)
    assert db.llm_decisions([opportunity_id])[opportunity_id] == "NOT_RUN"

@pytest.mark.asyncio
async def test_app_enforces_daily_llm_budget_before_api_call(tmp_path) -> None:
    from lead_radar.app import RadarApp
    from lead_radar.config import Settings
    from lead_radar.llm import LLMBudgetExceeded

    settings = Settings(
        db_path=tmp_path / "radar.db",
        keywords_path=__import__("pathlib").Path(__file__).parents[1] / "config" / "keywords.yaml",
        poll_interval_seconds=180,
        log_level="INFO",
        alert_threshold=13,
        review_threshold=8,
        alert_review_queue=False,
        cash_alert_threshold=72,
        value_alert_threshold=74,
        relationship_alert_threshold=76,
        max_alerts_per_cycle=5,
        telegram_min_send_interval_seconds=1.15,
        hn_enabled=False,
        hn_bootstrap_items=300,
        hn_max_items_per_cycle=500,
        hn_concurrency=20,
        github_enabled=False,
        github_token=None,
        github_lookback_hours=24,
        github_results_per_query=50,
        make_enabled=False,
        make_lookback_hours=48,
        make_topics_per_category=20,
        n8n_enabled=False,
        n8n_lookback_hours=48,
        n8n_topics_per_category=20,
        discourse_concurrency=6,
        telegram_bot_token=None,
        telegram_chat_id=None,
        llm_enabled=True,
        openai_api_key="test-key",
        daily_llm_budget_usd=0.00001,
        monthly_llm_budget_usd=1.0,
    )
    app = RadarApp(settings)
    opportunity = Opportunity(
        source="make",
        external_id="budget",
        url="https://example.test/budget",
        title="Need help with webhook",
        body="Our webhook is broken and customers are waiting.",
        author="buyer",
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(LLMBudgetExceeded):
        await app._classify_opportunities([(1, opportunity)], purpose="test")

def test_phase3_migrates_existing_opportunities_table_in_place(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE opportunities (
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
        CREATE TABLE source_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            phrase TEXT NOT NULL,
            match_type TEXT NOT NULL DEFAULT 'phrase'
        );
        CREATE TABLE actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opportunity_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            revenue REAL,
            created_at TEXT NOT NULL
        );
        """
    )
    conn.close()

    db = Database(path)
    with db.connect() as migrated:
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(opportunities)").fetchall()}
        tables = {row[0] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"llm_decision", "llm_confidence", "llm_classified_at"} <= columns
    assert {"llm_classifications", "llm_usage"} <= tables
