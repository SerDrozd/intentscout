from datetime import datetime, timezone

from lead_radar.database import Database
from lead_radar.models import Analysis, Opportunity, PhraseMatch, ScoredOpportunity


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
