from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lead_radar.app import CycleStats, RadarApp
from lead_radar.commercial import assess_commercial, has_direct_buyer_evidence
from lead_radar.config import Settings, load_keyword_config
from lead_radar.matcher import Matcher
from lead_radar.models import (
    AlertClass,
    Analysis,
    AuthorType,
    CommercialAssessment,
    CommercialIntent,
    Opportunity,
    OpportunityClass,
    ScoredOpportunity,
)


ROOT = Path(__file__).resolve().parents[1]
MATCHER = Matcher(load_keyword_config(ROOT / "config" / "keywords.yaml"))
NOW = datetime.now(timezone.utc)


def opportunity(
    source: str,
    body: str,
    *,
    title: str = "Lead",
    metadata: dict | None = None,
) -> Opportunity:
    return Opportunity(
        source=source,
        external_id=f"{source}:{title}",
        url="https://example.test/lead",
        title=title,
        body=body,
        author="person",
        created_at=NOW - timedelta(minutes=5),
        metadata=metadata or {},
    )


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = dict(
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
        llm_enabled=False,
        openai_api_key=None,
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_n8n_same_day_rescue_service_offer_is_hard_rejected() -> None:
    o = opportunity(
        "n8n",
        (
            "I am available today for one small existing n8n workflow problem that needs a fast fix. "
            "Price: USD 50 fixed for one bounded bug. You pay only after I reproduce the problem. "
            "What I need: a sanitized workflow export and one failing payload. "
            "Deliverable today: root-cause note, fixed workflow, and before/after evidence."
        ),
        title="Same-day n8n workflow rescue - $50 fixed for one bounded bug",
        metadata={"platform": "discourse", "event_type": "topic", "category_kind": "marketplace", "reply_count": 0},
    )
    a = assess_commercial(o, MATCHER.analyze(o), now=NOW)
    assert a.author_type == AuthorType.SERVICE_PROVIDER
    assert a.commercial_intent == CommercialIntent.NONE
    assert a.alert_class == AlertClass.REJECT
    assert a.cash_score <= 12


def test_bluesky_willing_to_pay_geopolitics_is_not_buyer() -> None:
    o = opportunity(
        "bluesky",
        "With refineries under pressure, Moscow is desperate for fuel and willing to pay a premium for supply.",
        title="Fuel markets are tightening",
        metadata={"platform": "bluesky", "query_family": "willing_pay", "reply_count": 0},
    )
    analysis = Analysis(
        phrase_matches=[],
        hard_excludes=[],
        soft_penalties=[],
        positive_context=[],
    )
    a = assess_commercial(o, analysis, now=NOW)
    assert a.author_type == AuthorType.UNKNOWN
    assert not a.p0
    assert a.alert_class in {AlertClass.P2, AlertClass.REJECT}


def test_raw_hn_default_requires_direct_buyer_evidence_for_p1_or_p0() -> None:
    o = opportunity(
        "hackernews",
        "We have been discussing webhook retries and API reliability. Fixed price systems can be fragile in production.",
        title="HN comment",
        metadata={"type": "comment", "reply_count": 0},
    )
    analysis = MATCHER.analyze(o)
    a = assess_commercial(o, analysis, now=NOW)
    assert not has_direct_buyer_evidence(o, analysis)
    assert a.alert_class in {AlertClass.P2, AlertClass.REJECT}
    assert max(a.cash_score, a.value_score, a.relationship_score) < 58


def test_direct_hn_buyer_can_still_be_p0() -> None:
    o = opportunity(
        "hackernews",
        "Our Stripe webhook is failing in production. We need a contractor to fix the API integration today and we have a budget.",
        title="HN comment",
        metadata={"type": "comment", "reply_count": 0},
    )
    analysis = MATCHER.analyze(o)
    a = assess_commercial(o, analysis, now=NOW)
    assert has_direct_buyer_evidence(o, analysis)
    assert a.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert a.p0


def test_background_llm_quota_preserves_live_alert_reserve(tmp_path: Path) -> None:
    app = RadarApp(
        settings(
            tmp_path,
            max_llm_candidates_per_hour=10,
            max_llm_candidates_per_day=20,
            llm_alert_reserve_per_hour=4,
            llm_alert_reserve_per_day=8,
        )
    )
    for i in range(7):
        app.db.record_llm_usage(
            purpose="auto_shortlist",
            model="test",
            response_id=f"r{i}",
            candidate_count=1,
            input_tokens=1,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=1,
            estimated_cost_usd=0.0,
        )
    background_remaining, _, _ = app._budget_capacity(purpose="auto_shortlist")
    live_remaining, _, _ = app._budget_capacity(purpose="live_alert_gate")
    assert background_remaining == 0
    assert live_remaining == 3


def manual_scored(o: Opportunity, *, p0: bool) -> ScoredOpportunity:
    assessment = CommercialAssessment(
        author_type=AuthorType.POTENTIAL_BUYER,
        commercial_intent=CommercialIntent.IMPLICIT,
        opportunity_class=OpportunityClass.CASH_NOW,
        alert_class=AlertClass.P0_CASH if p0 else AlertClass.P1,
        cash_score=75 if p0 else 66,
        value_score=62,
        relationship_score=50,
        freshness_score=100,
        competition_score=100,
        features={},
        reasons=[],
    )
    return ScoredOpportunity(
        opportunity=o,
        analysis=MATCHER.analyze(o),
        score=15,
        breakdown={},
        assessment=assessment,
    )


@pytest.mark.asyncio
async def test_fail_open_holds_implicit_p0_without_direct_buyer_evidence(tmp_path: Path) -> None:
    app = RadarApp(settings(tmp_path, llm_enabled=False))
    o = opportunity(
        "n8n",
        "Our workflow has an API issue and webhook errors in production.",
        metadata={"platform": "discourse", "event_type": "topic", "category_kind": "question", "reply_count": 0},
    )
    scored = manual_scored(o, p0=True)
    stats = CycleStats()
    kept = await app._llm_filter_pending([(75, 1, scored)], stats)
    assert kept == []
    assert stats.llm_held == 1


@pytest.mark.asyncio
async def test_fail_open_keeps_direct_p0_when_llm_unavailable(tmp_path: Path) -> None:
    app = RadarApp(settings(tmp_path, llm_enabled=False))
    o = opportunity(
        "n8n",
        "We need a contractor to fix our broken webhook and API integration today. We have a budget.",
        metadata={"platform": "discourse", "event_type": "topic", "category_kind": "marketplace", "reply_count": 0},
    )
    scored = manual_scored(o, p0=True)
    assert has_direct_buyer_evidence(o, scored.analysis)
    stats = CycleStats()
    kept = await app._llm_filter_pending([(75, 1, scored)], stats)
    assert len(kept) == 1
    assert stats.llm_held == 0


def test_fresh_direct_p1_enters_review_lane(tmp_path: Path) -> None:
    app = RadarApp(settings(tmp_path, review_alerts_enabled=True, review_alert_min_priority=64, review_alert_min_freshness=68))
    o = opportunity(
        "n8n",
        "Looking for an n8n automation specialist for a paid trial. Need help stress-testing API and webhook failure cases.",
        title="Looking for n8n automation specialist - paid trial",
        metadata={"platform": "discourse", "event_type": "topic", "category_kind": "marketplace", "reply_count": 0},
    )
    scored = manual_scored(o, p0=False)
    assert app._review_alert_eligible(scored)
