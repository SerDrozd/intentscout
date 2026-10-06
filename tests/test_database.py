from datetime import datetime, timezone

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


def test_insert_and_dedupe(tmp_path):
    db = Database(tmp_path / "radar.db")
    opportunity = Opportunity(
        source="github",
        external_id="123",
        url="https://example.com",
        title="Webhook not working",
        body="production webhook stopped working",
        author="x",
        created_at=datetime.now(timezone.utc),
    )
    analysis = Analysis([PhraseMatch("webhooks", "webhook stopped working")], [], [], ["production"])
    scored = ScoredOpportunity(opportunity, analysis, 15, {"test": 15})
    first = db.insert(scored)
    second = db.insert(scored)
    assert first is not None
    assert second is None
    assert db.exists("github", "123")


def _scored_with_assessment(external_id: str, alert_class: AlertClass, cash: int, value: int, rel: int) -> ScoredOpportunity:
    opportunity = Opportunity(
        source="n8n",
        external_id=external_id,
        url=f"https://example.com/{external_id}",
        title=f"Opportunity {external_id}",
        body="Looking for a contractor for a paid API integration.",
        author="buyer",
        created_at=datetime.now(timezone.utc),
    )
    analysis = Analysis([PhraseMatch("integrations", "api integration")], [], [], ["client"])
    assessment = CommercialAssessment(
        author_type=AuthorType.BUYER,
        commercial_intent=CommercialIntent.EXPLICIT,
        opportunity_class=OpportunityClass.CASH_NOW,
        alert_class=alert_class,
        cash_score=cash,
        value_score=value,
        relationship_score=rel,
        freshness_score=100,
        competition_score=90,
        features={},
    )
    return ScoredOpportunity(opportunity, analysis, 15, {"test": 15}, assessment=assessment)


def test_shortlist_returns_only_actionable_p0_p1_and_orders_priority(tmp_path):
    db = Database(tmp_path / "radar.db")
    p1_id = db.insert(_scored_with_assessment("p1", AlertClass.P1, 60, 65, 50))
    p0_id = db.insert(_scored_with_assessment("p0", AlertClass.P0_RELATIONSHIP, 70, 75, 92))
    p2_id = db.insert(_scored_with_assessment("p2", AlertClass.P2, 95, 95, 95))
    assert p1_id is not None and p0_id is not None and p2_id is not None

    rows = db.shortlist(limit=20)
    assert [row["id"] for row in rows] == [p0_id, p1_id]

    db.set_status(p0_id, "contacted")
    rows = db.shortlist(limit=20)
    assert [row["id"] for row in rows] == [p1_id]
