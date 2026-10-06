from datetime import datetime, timedelta, timezone
from pathlib import Path

from lead_radar.config import load_keyword_config
from lead_radar.matcher import Matcher
from lead_radar.models import Opportunity
from lead_radar.scoring import alert_eligibility, score

ROOT = Path(__file__).resolve().parents[1]
CONFIG = load_keyword_config(ROOT / "config" / "keywords.yaml")
MATCHER = Matcher(CONFIG)


def make(source: str, title: str, body: str, metadata=None) -> Opportunity:
    return Opportunity(
        source=source,
        external_id="v13",
        url="https://example.com/v13",
        title=title,
        body=body,
        author="tester",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        metadata=metadata or {},
    )


def analyze(o: Opportunity):
    a = MATCHER.analyze(o)
    total, breakdown = score(o, a)
    eligible, reason = alert_eligibility(o, a, breakdown)
    return a, total, breakdown, eligible, reason


def test_rule_anchor_import_does_not_match_important():
    o = make(
        "hackernews",
        "HN comment",
        "The most important thing is that a bug is not necessarily less serious than a vulnerability. This issue should be patched.",
        {"type": "comment"},
    )
    a, *_ = analyze(o)
    assert not any(m.phrase.casefold().startswith("import +") for m in a.phrase_matches)


def test_rule_trigger_developer_to_does_not_match_developer_tools():
    o = make(
        "github",
        "Expand public Repo Unpack evidence",
        "We are prioritizing developer tools and frameworks. No deployment is authorized.",
    )
    a, *_ = analyze(o)
    assert not any("developer to" in m.phrase.casefold() for m in a.phrase_matches)


def test_generic_hn_bug_discussion_is_not_alert_eligible():
    o = make(
        "hackernews",
        "HN comment",
        "I think you have to distinguish bugs and vulnerabilities. A serious bug should be patched just as urgently as a vulnerability.",
        {"type": "comment"},
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "hn_discussion_or_weak_intent"


def test_hn_job_post_is_not_alert_eligible():
    o = make(
        "hackernews",
        "HN comment",
        "Estee Lauder Companies | Staff Machine Learning Engineer | NY | Hybrid | Full-Time. We are hiring for this role. Apply here.",
        {"type": "comment"},
    )
    _, _, breakdown, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "hn_job_post"
    assert breakdown.get("hn_job_post") == -8


def test_generic_customer_discussion_is_not_business_context():
    o = make(
        "hackernews",
        "HN comment",
        "European banks are trash and I don't think the payment system will benefit the customer. It may benefit merchants.",
        {"type": "comment"},
    )
    _, _, breakdown, eligible, _ = analyze(o)
    assert "business_context" not in breakdown
    assert not eligible


def test_hn_owned_business_problem_with_help_can_alert():
    o = make(
        "hackernews",
        "Ask HN: Stripe webhook stopped working",
        "Our company has paying customers waiting. We can't get our Stripe webhook working and need help fixing it today.",
        {"type": "story"},
    )
    a, total, _, eligible, reason = analyze(o)
    assert a.phrase_matches
    assert total >= 13
    assert eligible
    assert reason in {"hn_explicit_owned_help", "hn_owned_business_problem"}


def test_github_without_money_never_alerts_even_if_score_high():
    o = make(
        "github",
        "Need someone to fix webhook",
        "Our webhook is broken and we need someone to help fix it. Steps to reproduce follow.",
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "github_no_commercial_intent"


def test_github_paid_help_can_alert():
    o = make(
        "github",
        "Need contractor to fix webhook",
        "Our customer webhook is broken. We have budget for a contractor and are willing to pay for help.",
    )
    _, total, _, eligible, reason = analyze(o)
    assert total >= 13
    assert eligible
    assert reason == "github_commercial_intent"


def test_mime_word_contractor_is_not_commercial_intent():
    o = make(
        "github",
        "scanplan request-set format",
        "The MIME type is application/vnd.contractor.http-requests+json and production code needs a producer.",
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "github_no_commercial_intent"
