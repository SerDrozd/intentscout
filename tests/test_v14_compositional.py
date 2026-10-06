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
        external_id=title[:30],
        url="https://example.com/v14",
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


def cats(a):
    return {m.category for m in a.phrase_matches}


def test_coverage_is_far_beyond_1000_literal_variants():
    stats = MATCHER.coverage_stats()
    assert stats["exact_phrases"] == 250
    assert stats["technical_aliases"] >= 200
    assert stats["intent_aliases"] >= 250
    assert stats["pair_variant_equivalents"] >= 10000


def test_synonym_and_reordered_help_phrase_alerts():
    o = make(
        "hackernews",
        "Ask HN: Stripe events",
        "Our company has customers waiting. Since this morning our Stripe webhook has been failing, and we could use a hand getting it working again.",
        {"type": "story"},
    )
    a, total, _, eligible, _ = analyze(o)
    assert "webhooks" in cats(a)
    assert "explicit_help_hire" in cats(a)
    assert total >= 13
    assert eligible


def test_owned_problem_word_order_can_change():
    o = make(
        "hackernews",
        "HN comment",
        "Broken since yesterday is the OAuth flow in our production app; our customers cannot sign in.",
        {"type": "comment"},
    )
    a, total, _, eligible, reason = analyze(o)
    assert "auth_oauth" in cats(a)
    assert cats(a) & {"buyer_owned_problem", "buyer_business_problem"}
    assert total >= 13
    assert eligible
    assert reason == "hn_owned_business_problem"


def test_manual_process_pain_without_word_automation_is_caught():
    o = make(
        "hackernews",
        "HN comment",
        "We do this manually every Friday: copying data by hand into a spreadsheet takes hours for our team.",
        {"type": "comment"},
    )
    a, total, _, eligible, reason = analyze(o)
    assert "automation_workflows" in cats(a)
    assert "buyer_manual_pain" in cats(a)
    assert total >= 13
    assert eligible
    assert reason == "hn_manual_process_pain"


def test_owned_build_request_without_exact_sentence_is_caught():
    o = make(
        "hackernews",
        "Ask HN: CRM integration",
        "We use HubSpot and our own backend. We are trying to connect the two systems before launch.",
        {"type": "story"},
    )
    a, total, _, eligible, reason = analyze(o)
    assert "integrations" in cats(a)
    assert "buyer_build_request" in cats(a)
    assert total >= 13
    assert eligible
    assert reason == "hn_owned_build_request"


def test_previous_false_positive_job_stays_blocked():
    o = make(
        "hackernews",
        "HN comment",
        "Estee Lauder Companies | Staff Machine Learning Engineer | Hybrid | Full-Time. We are hiring. Apply here. You will integrate AI systems and solve ambiguous problems.",
        {"type": "comment"},
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "hn_job_post"


def test_previous_generic_discussion_stays_blocked():
    o = make(
        "hackernews",
        "HN comment",
        "European banks are bad and I don't believe the payment system will benefit the customer. Merchants may benefit instead.",
        {"type": "comment"},
    )
    _, _, _, eligible, _ = analyze(o)
    assert not eligible


def test_github_backlog_still_needs_money_signal():
    o = make(
        "github",
        "Expand public Repo Unpack evidence to 185 repositories",
        "## What Add 100 open source developer tools. Preserve records, run scripts and validate citations. No deployment is authorized.",
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "github_no_commercial_intent"


def test_github_commercial_synonym_can_alert():
    o = make(
        "github",
        "Stripe webhook help wanted",
        "Our customer webhook keeps timing out. Budget available for paid contract work to get this fixed this week.",
    )
    a, total, _, eligible, reason = analyze(o)
    assert "commercial_intent" in cats(a)
    assert total >= 13
    assert eligible
    assert reason == "github_commercial_intent"
