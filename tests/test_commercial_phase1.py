from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lead_radar.commercial import assess_commercial, competition_score, freshness_score
from lead_radar.config import load_keyword_config
from lead_radar.database import Database
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType, CommercialIntent, Opportunity


ROOT = Path(__file__).resolve().parents[1]
MATCHER = Matcher(load_keyword_config(ROOT / "config" / "keywords.yaml"))
NOW = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)


def make(
    source: str,
    body: str,
    *,
    minutes_old: int = 5,
    title: str = "Opportunity",
    metadata: dict | None = None,
) -> Opportunity:
    return Opportunity(
        source=source,
        external_id=f"{source}:{title}:{minutes_old}",
        url="https://example.com/lead",
        title=title,
        body=body,
        author="person",
        created_at=NOW - timedelta(minutes=minutes_old),
        metadata=metadata or {},
    )


def assess(o: Opportunity):
    return assess_commercial(o, MATCHER.analyze(o), now=NOW)


def test_cash_now_paid_bounded_webhook_is_p0():
    a = assess(
        make(
            "hackernews",
            "Our customer webhook stopped working in production this morning. Customers are waiting. We need a contractor to fix the Stripe webhook and we have a budget for it.",
            metadata={"type": "comment", "reply_count": 0},
        )
    )
    assert a.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert a.commercial_intent == CommercialIntent.EXPLICIT
    assert a.cash_score >= 72
    assert a.alert_class == AlertClass.P0_CASH


def test_high_value_project_is_not_penalized_for_being_larger():
    a = assess(
        make(
            "hackernews",
            "We run a SaaS and need a developer to finish the production backend, connect several API integrations and clean up our data pipeline. We have a budget and want to start this week.",
            metadata={"type": "story", "reply_count": 1},
        )
    )
    assert a.commercial_intent in {CommercialIntent.IMPLICIT, CommercialIntent.EXPLICIT}
    assert a.value_score >= 74
    assert a.alert_class == AlertClass.P0_HIGH_VALUE
    assert a.opportunity_class.value == "HIGH_VALUE"


def test_agency_overflow_scores_relationship():
    a = assess(
        make(
            "hackernews",
            "We are an automation agency with multiple client projects and need extra capacity. Looking for a contractor for recurring Python API integration and webhook work. Paid test first, then ongoing client work.",
            metadata={"type": "comment", "reply_count": 0},
        )
    )
    assert a.author_type == AuthorType.AGENCY_BUYER
    assert a.relationship_score >= 76
    assert a.alert_class == AlertClass.P0_RELATIONSHIP


def test_job_seeker_is_hard_downranked():
    a = assess(
        make(
            "hackernews",
            "Location: Berlin Remote: Yes Technologies: Python, FastAPI, APIs Email: dev@example.com Open to contract work and available for hire.",
            metadata={"type": "comment", "reply_count": 0},
        )
    )
    assert a.author_type == AuthorType.JOB_SEEKER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.cash_score <= 12
    assert a.alert_class == AlertClass.REJECT


def test_generic_github_oss_issue_is_not_buyer():
    a = assess(
        make(
            "github",
            "The webhook handler fails after an OAuth refresh and the API returns 500. Steps to reproduce are below.",
            title="Webhook fails after OAuth refresh",
            metadata={"comments": 0},
        )
    )
    assert a.author_type == AuthorType.OSS_MAINTAINER
    assert a.cash_score <= 18
    assert not a.p0


def test_freshness_bands_match_first_mover_goal():
    assert freshness_score(make("hackernews", "API issue", minutes_old=5), now=NOW) == 100
    assert freshness_score(make("hackernews", "API issue", minutes_old=20), now=NOW) == 92
    assert freshness_score(make("hackernews", "API issue", minutes_old=50), now=NOW) == 82
    assert freshness_score(make("hackernews", "API issue", minutes_old=150), now=NOW) == 68
    assert freshness_score(make("hackernews", "API issue", minutes_old=4 * 24 * 60), now=NOW) == 0


def test_competition_uses_visible_reply_count():
    assert competition_score(make("hackernews", "API issue", metadata={"reply_count": 0})) == 100
    assert competition_score(make("hackernews", "API issue", metadata={"reply_count": 2})) == 90
    assert competition_score(make("hackernews", "API issue", metadata={"reply_count": 5})) == 76
    assert competition_score(make("github", "API issue", metadata={"comments": 30})) == 22


def test_existing_database_is_migrated_in_place(tmp_path):
    path = tmp_path / "radar.db"
    conn = sqlite3.connect(path)
    conn.execute(
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
            UNIQUE(source, external_id)
        )
        """
    )
    conn.commit()
    conn.close()

    Database(path)
    conn = sqlite3.connect(path)
    names = {row[1] for row in conn.execute("PRAGMA table_info(opportunities)")}
    conn.close()
    assert {"cash_score", "value_score", "relationship_score", "author_type", "alert_class"} <= names


def test_phase1_app_flow_persists_commercial_scores(tmp_path):
    import asyncio

    from lead_radar.app import CycleStats, RadarApp
    from lead_radar.config import Settings

    settings = Settings(
        db_path=tmp_path / "radar.db",
        keywords_path=ROOT / "config" / "keywords.yaml",
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
    )
    app = RadarApp(settings)
    stats = CycleStats()
    pending = []
    opportunity = make(
        "hackernews",
        "Our customers are waiting. We need a contractor to fix our broken Stripe webhook and we have a budget.",
        metadata={"type": "comment", "reply_count": 0},
    )
    asyncio.run(app.process(opportunity, stats, pending))

    assert stats.inserted == 1
    assert stats.alert_candidates == 1
    row = app.db.recent(1)[0]
    assert row["cash_score"] >= 72
    assert row["author_type"] in {"BUYER", "TECHNICAL_BUYER"}
    assert row["alert_class"] == "P0_CASH"


def test_reclassify_stored_community_reply_removes_false_p0(tmp_path):
    from lead_radar.app import RadarApp
    from lead_radar.config import Settings

    settings = Settings(
        db_path=tmp_path / "radar.db",
        keywords_path=ROOT / "config" / "keywords.yaml",
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
    )
    app = RadarApp(settings)
    opportunity = Opportunity(
        source="make",
        external_id="topic:77:post:8",
        url="https://community.make.com/t/x/77/8",
        title="Reply: Looking for White-Label Make.com Automation Developers",
        body="Hi, I run an automation studio and can help with your Make and API work.",
        author="seller",
        created_at=NOW - timedelta(minutes=3),
        metadata={
            "category_key": "hire_help",
            "category_kind": "marketplace",
            "event_type": "reply",
            "topic_author": "buyer",
            "is_topic_author": False,
            "reply_count": 8,
        },
    )
    # Insert a deliberately stale/incorrect assessment to simulate the Phase 2 bootstrap data.
    analysis = app.matcher.analyze(opportunity)
    from lead_radar.models import CommercialAssessment, OpportunityClass, ScoredOpportunity
    stale = CommercialAssessment(
        author_type=AuthorType.AGENCY_BUYER,
        commercial_intent=CommercialIntent.EXPLICIT,
        opportunity_class=OpportunityClass.RELATIONSHIP,
        alert_class=AlertClass.P0_RELATIONSHIP,
        cash_score=80,
        value_score=87,
        relationship_score=90,
        freshness_score=100,
        competition_score=58,
        features={},
        reasons=[],
    )
    inserted = app.db.insert(ScoredOpportunity(opportunity, analysis, 14, {}, assessment=stale))
    assert inserted is not None
    assert app.db.stats()["p0"] == 1

    result = app.reclassify_stored(("make",))
    assert result["processed"] == 1
    assert result["reject"] == 1
    assert app.db.stats()["p0"] == 0
    row = app.db.recent(1)[0]
    assert row["author_type"] == "SERVICE_PROVIDER"
    assert row["alert_class"] == "REJECT"


def test_github_backlog_spec_with_overflow_word_cannot_become_agency_buyer():
    opportunity = make(
        "github",
        (
            "Specification for provider/model cache preferences, context-overflow signals and tool-result image paths. "
            "Preserve explicit-error and zero-output overflow scenarios and update the production tests."
        ),
        title="Qualify cache retention, overflow and tool images",
        metadata={"comments": 0},
    )
    a = assess(opportunity)
    assert a.author_type == AuthorType.OSS_MAINTAINER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.features["github_direct_buyer_signal"] is False
    assert a.alert_class == AlertClass.P2
    assert not a.p0


def test_github_direct_paid_request_can_still_surface():
    opportunity = make(
        "github",
        (
            "Our Stripe webhook is broken in production and customers are waiting. "
            "We are looking for a contractor to fix it and have a budget for this work."
        ),
        title="Looking for contractor to fix production Stripe webhook",
        metadata={"comments": 0},
    )
    a = assess(opportunity)
    assert a.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert a.commercial_intent in {CommercialIntent.IMPLICIT, CommercialIntent.EXPLICIT}
    assert a.features["github_direct_buyer_signal"] is True
    assert a.alert_class in {AlertClass.P0_CASH, AlertClass.P0_HIGH_VALUE, AlertClass.P0_RELATIONSHIP, AlertClass.P1}


def test_n8n_self_promoter_looking_for_opportunities_is_job_seeker():
    opportunity = make(
        "n8n",
        (
            "Looking for n8n Automation Opportunities / Collaboration. "
            "I am an AI Automation Developer. I want to gain commercial experience and "
            "I'm open to project-based work and long-term collaboration."
        ),
        title="N8n Automation Developer | Project-Based & Remote Collaboration",
        metadata={"category_kind": "marketplace", "event_type": "topic", "reply_count": 1},
    )
    a = assess(opportunity)
    assert a.author_type == AuthorType.JOB_SEEKER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.alert_class == AlertClass.REJECT


def test_make_vendor_product_promo_is_service_provider_not_buyer():
    opportunity = make(
        "make",
        (
            "I'm part of the InstantDM team. I'm sharing this because I'd like your feedback. "
            "What it is: our product schedules social posts and has native Make nodes, REST API and webhooks. "
            "Pricing note: there is a 4 day free trial. Docs: API reference."
        ),
        title="Schedule posts to 8 social platforms from Make.com",
        metadata={"category_kind": "marketplace", "event_type": "topic", "reply_count": 0},
    )
    a = assess(opportunity)
    assert a.author_type == AuthorType.SERVICE_PROVIDER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.alert_class == AlertClass.REJECT


def test_marketplace_author_application_logistics_reply_is_not_new_buyer_event():
    opportunity = make(
        "make",
        (
            "Thank you for your application. We are reviewing all applications and evaluating candidates "
            "for the next stage. We will get back to you shortly with the interview date and time."
        ),
        title="Reply: Hiring Senior AI & Automation Operations Lead (Make.com Specialist) | Remote",
        metadata={
            "category_kind": "marketplace",
            "event_type": "reply",
            "topic_author": "buyer",
            "is_topic_author": True,
            "reply_count": 8,
        },
    )
    a = assess(opportunity)
    assert a.author_type == AuthorType.RECRUITER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.alert_class == AlertClass.REJECT


def test_structured_long_hiring_role_is_recruiter_even_when_contract_based():
    opportunity = make(
        "n8n",
        (
            "About the Role: We are looking for a hands-on AI Operations Lead. "
            "Requirements: advanced n8n and Make. Type: long-term renewable contract. "
            "How to Apply: submit your background, weekly hourly availability and portfolio. "
            "Candidates will be invited to an interview."
        ),
        title="Hiring Senior AI & Automation Operations Lead (n8n & Make Specialist) | Remote",
        metadata={"category_kind": "marketplace", "event_type": "topic", "reply_count": 12},
    )
    a = assess(opportunity)
    assert a.author_type == AuthorType.RECRUITER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.alert_class == AlertClass.REJECT


def test_long_github_audit_cannot_trigger_on_buried_looking_for_phrase():
    body = (
        "Review action item. Phase P4. Plan and evidence. Why: reports are hidden. "
        "What to do: enable flags, write tests, update production code, verify acceptance criteria. "
        + ("internal engineering evidence and implementation detail. " * 80)
        + "Users who go looking for someone to help with store reviews should find documentation."
    )
    opportunity = make(
        "github",
        body,
        title="[A42] Ship the hidden features deliberately",
        metadata={"comments": 0},
    )
    a = assess(opportunity)
    assert a.author_type == AuthorType.OSS_MAINTAINER
    assert a.features["github_direct_buyer_signal"] is False
    assert a.alert_class == AlertClass.P2
