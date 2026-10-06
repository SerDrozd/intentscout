
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lead_radar.config import load_keyword_config
from lead_radar.matcher import Matcher
from lead_radar.models import Opportunity
from lead_radar.scoring import score

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_keyword_config(ROOT / "config" / "keywords.yaml")
MATCHER = Matcher(CONFIG)

def make(source: str, title: str, body: str) -> Opportunity:
    return Opportunity(
        source=source,
        external_id="x",
        url="https://example.com/x",
        title=title,
        body=body,
        author="tester",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
    )

def test_rule_requires_proximity():
    body = "Webhook support is documented here. " + ("x " * 200) + "The unrelated build failed."
    a = MATCHER.analyze(make("github", "Docs", body))
    assert not any(m.category == "webhooks" and m.match_type == "rule" for m in a.phrase_matches)

def test_generic_github_bug_is_not_high_intent():
    o = make(
        "github",
        'Switchbot Cloud "Failed to Connect" on setup',
        "The integration fails to connect even though the API is reachable. Steps to reproduce...",
    )
    a = MATCHER.analyze(o)
    total, breakdown = score(o, a)
    assert total < 13
    assert "github_low_commercial_intent" in breakdown

def test_github_internal_spec_is_penalized():
    o = make(
        "github",
        "Spec: Add OAuth callback support",
        "## Goal\nImplement OAuth callback support.\n## Acceptance Criteria\n- tests pass",
    )
    a = MATCHER.analyze(o)
    total, breakdown = score(o, a)
    assert total < 13
    assert breakdown.get("github_backlog_spec") == -6

def test_explicit_paid_github_help_can_still_alert():
    o = make(
        "github",
        "Need someone to fix our webhook today",
        "Our customer webhook stopped working. We are willing to pay for help and need someone to fix the API integration.",
    )
    a = MATCHER.analyze(o)
    total, breakdown = score(o, a)
    assert total >= 13
    assert "github_low_commercial_intent" not in breakdown

def test_hn_business_problem_can_alert_without_github_penalty():
    o = make(
        "hackernews",
        "Ask HN: webhook problem",
        "Our company has paying customers waiting because our webhook stopped working in production.",
    )
    a = MATCHER.analyze(o)
    total, breakdown = score(o, a)
    assert total >= 13
    assert "github_low_commercial_intent" not in breakdown
