from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lead_radar.database import Database
from lead_radar.models import (
    AlertClass,
    Analysis,
    AuthorType,
    CommercialAssessment,
    CommercialIntent,
    Opportunity,
    OpportunityClass,
    PhraseMatch,
    ScoredOpportunity,
)


def _lead(source: str, external_id: str, *, query_key: str, alert: AlertClass) -> ScoredOpportunity:
    opportunity = Opportunity(
        source=source,
        external_id=external_id,
        url=f"https://example.test/{external_id}",
        title="Need contractor for API automation",
        body="We need a contractor for a paid API automation project.",
        author="buyer",
        created_at=datetime.now(timezone.utc),
        metadata={"query_family": query_key, "platform": source},
    )
    analysis = Analysis([PhraseMatch("integrations", "api automation")], [], [], ["client"])
    assessment = CommercialAssessment(
        author_type=AuthorType.BUYER,
        commercial_intent=CommercialIntent.EXPLICIT,
        opportunity_class=OpportunityClass.HIGH_VALUE,
        alert_class=alert,
        cash_score=78,
        value_score=84,
        relationship_score=62,
        freshness_score=95,
        competition_score=88,
        features={},
    )
    return ScoredOpportunity(opportunity, analysis, 16, {"test": 16}, assessment=assessment)


def test_feedback_actions_and_source_roi(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    won_id = db.insert(_lead("bluesky", "won", query_key="need_contractor", alert=AlertClass.P0_HIGH_VALUE))
    ignored_id = db.insert(_lead("bluesky", "ignored", query_key="need_contractor", alert=AlertClass.P1))
    assert won_id is not None and ignored_id is not None

    db.record_collector_run(
        source="bluesky",
        fetched=25,
        matched=2,
        inserted=2,
        excluded=0,
        duration_ms=420,
    )
    db.set_status(won_id, "contacted", reason="telegram")
    db.set_status(won_id, "replied", reason="email reply")
    db.set_status(won_id, "proposal", reason="sent fixed-price scope")
    db.set_status(won_id, "won", reason="paid", revenue=250.0)
    db.set_status(ignored_id, "ignored", reason="not a fit")
    db.record_llm_usage(
        purpose="shortlist",
        model="gpt-6-luna",
        response_id="resp_roi",
        candidate_count=2,
        input_tokens=1000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=200,
        estimated_cost_usd=0.0002,
        opportunity_ids=[won_id, ignored_id],
    )

    rows = db.roi_report(days=30)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "bluesky"
    assert row["fetched"] == 25
    assert row["leads"] == 2
    assert row["p0"] == 1
    assert row["p1"] == 1
    assert row["contacted"] == 1
    assert row["replied"] == 1
    assert row["proposal"] == 1
    assert row["won"] == 1
    assert row["ignored"] == 1
    assert row["revenue"] == pytest.approx(250.0)
    assert row["llm_cost"] == pytest.approx(0.0002)


def test_query_roi_uses_query_family_and_does_not_require_collector_runs(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    first = db.insert(_lead("hackernews", "1", query_key="need_help_api", alert=AlertClass.P1))
    second = db.insert(_lead("hackernews", "2", query_key="need_help_api", alert=AlertClass.P1))
    third = db.insert(_lead("hackernews", "3", query_key="looking_for_dev", alert=AlertClass.P0_CASH))
    assert first and second and third
    db.set_status(third, "contacted")

    rows = db.roi_report(days=30, by_query=True, sources=("hackernews",))
    keyed = {str(row["key"]): row for row in rows}
    assert keyed["need_help_api"]["leads"] == 2
    assert keyed["looking_for_dev"]["p0"] == 1
    assert keyed["looking_for_dev"]["contacted"] == 1
    assert keyed["need_help_api"]["fetched"] is None


def test_set_status_validates_stage_and_revenue(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    opportunity_id = db.insert(_lead("make", "x", query_key="hire_help", alert=AlertClass.P1))
    assert opportunity_id is not None
    with pytest.raises(ValueError):
        db.set_status(opportunity_id, "maybe")
    with pytest.raises(ValueError):
        db.set_status(opportunity_id, "won", revenue=-1.0)


def test_reset_feedback_removes_accidental_funnel_and_revenue(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    opportunity_id = db.insert(_lead("make", "reset", query_key="questions", alert=AlertClass.P1))
    assert opportunity_id is not None
    db.set_status(opportunity_id, "contacted")
    db.set_status(opportunity_id, "replied")
    db.set_status(opportunity_id, "won", revenue=250.0)

    before = db.roi_report(days=30)
    assert before[0]["won"] == 1
    assert before[0]["revenue"] == pytest.approx(250.0)

    db.reset_feedback(opportunity_id)

    after = db.roi_report(days=30)
    assert after[0]["contacted"] == 0
    assert after[0]["replied"] == 0
    assert after[0]["won"] == 0
    assert after[0]["revenue"] == pytest.approx(0.0)
    row = db.opportunity_row(opportunity_id)
    assert row is not None
    assert row["status"] == "new"


def test_revenue_is_only_allowed_for_won_status(tmp_path) -> None:
    db = Database(tmp_path / "radar.db")
    opportunity_id = db.insert(_lead("make", "revenue-guard", query_key="questions", alert=AlertClass.P1))
    assert opportunity_id is not None
    with pytest.raises(ValueError, match="only be recorded"):
        db.set_status(opportunity_id, "contacted", revenue=10.0)
