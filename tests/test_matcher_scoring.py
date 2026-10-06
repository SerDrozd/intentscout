from datetime import datetime, timedelta, timezone
from pathlib import Path

from lead_radar.config import load_keyword_config
from lead_radar.matcher import Matcher
from lead_radar.models import Opportunity
from lead_radar.scoring import score


ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_keyword_config(ROOT / "config" / "keywords.yaml")


def opp(text: str, minutes_old: int = 5) -> Opportunity:
    return Opportunity(
        source="hackernews",
        external_id="1",
        url="https://example.com/1",
        title="Need technical help",
        body=text,
        author="founder",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_old),
    )


def test_strong_business_problem_scores_high():
    matcher = Matcher(CONFIG)
    item = opp("Our customer webhook stopped working in production. Need someone to fix API urgently; willing to pay for help.")
    analysis = matcher.analyze(item)
    total, breakdown = score(item, analysis)
    assert not analysis.excluded
    assert any(m.category == "webhooks" for m in analysis.phrase_matches)
    assert total >= 13
    assert breakdown["strong_technical_fit"] == 4


def test_homework_is_excluded():
    matcher = Matcher(CONFIG)
    item = opp("Homework help: API not working in my school assignment")
    analysis = matcher.analyze(item)
    total, _ = score(item, analysis)
    assert analysis.excluded
    assert total == -100


def test_rule_layer_catches_paraphrased_webhook_problem():
    from datetime import datetime, timezone
    from pathlib import Path
    import yaml
    from lead_radar.matcher import Matcher
    from lead_radar.models import Opportunity
    from lead_radar.scoring import score

    cfg = yaml.safe_load(Path("config/keywords.yaml").read_text(encoding="utf-8"))
    matcher = Matcher(cfg)
    opp = Opportunity(
        source="github",
        external_id="rule-1",
        url="https://example.com/1",
        title="Stripe webhook has been failing since this morning",
        body="This is affecting our production customer integration.",
        author="acme",
        created_at=datetime.now(timezone.utc),
    )
    analysis = matcher.analyze(opp)
    assert any(m.category == "webhooks" for m in analysis.phrase_matches)
    assert any(m.match_type == "rule" for m in analysis.phrase_matches)
    total, breakdown = score(opp, analysis)
    # v1.2 keeps technically relevant GitHub bugs in SQLite but does not
    # automatically treat them as commercial HIGH INTENT.
    assert total < 13
    assert "github_low_commercial_intent" in breakdown


def test_rule_layer_does_not_match_generic_oauth_feature_request():
    from datetime import datetime, timezone
    from pathlib import Path
    import yaml
    from lead_radar.matcher import Matcher
    from lead_radar.models import Opportunity

    cfg = yaml.safe_load(Path("config/keywords.yaml").read_text(encoding="utf-8"))
    matcher = Matcher(cfg)
    opp = Opportunity(
        source="github",
        external_id="rule-2",
        url="https://example.com/2",
        title="Add OAuth support",
        body="It would be nice to support OAuth in a future release.",
        author="user",
        created_at=datetime.now(timezone.utc),
    )
    analysis = matcher.analyze(opp)
    assert not analysis.phrase_matches
