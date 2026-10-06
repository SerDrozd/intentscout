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
        external_id=title[:50],
        url="https://example.com/v15",
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


def test_hn_open_to_contract_work_is_seller_not_buyer():
    o = make(
        "hackernews",
        "HN comment",
        "Location: San Jose, CA Remote: Open Technologies: Python, REST APIs, OAuth2. Résumé/CV: https://example.com Email: me@example.com Open to contract work.",
        {"type": "comment"},
    )
    _, _, breakdown, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "hackernews_seller_self_promo"
    assert breakdown.get("seller_self_promo") == -10
    assert "explicit_pay_or_hire" not in breakdown


def test_hn_seeking_freelance_work_is_seller_not_buyer():
    o = make(
        "hackernews",
        "HN comment",
        "SEEKING FREELANCE WORK | Senior iOS Developer. I build apps and backend systems and am available for freelance work.",
        {"type": "comment"},
    )
    _, _, breakdown, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "hackernews_seller_self_promo"
    assert breakdown.get("seller_self_promo") == -10


def test_generic_llm_copy_paste_tedium_is_not_manual_buyer_pain():
    o = make(
        "hackernews",
        "HN comment",
        "I use LLMs to generate isolated copy-paste code. I still write my own code because fixing generated code is more tedious than writing it myself.",
        {"type": "comment"},
    )
    _, _, breakdown, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "hn_discussion_or_weak_intent"
    assert breakdown.get("manual_process_pain") is None


def test_github_contract_work_mention_inside_roadmap_does_not_alert():
    o = make(
        "github",
        "Operator automation wizard: schedules and custom scripts",
        "## Goal Build an automation wizard. The document mentions contract work in historical notes, but this is an internal roadmap and no contractor is being sought.",
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "github_no_commercial_intent"


def test_github_bounty_receipt_is_historical_not_open_opportunity():
    o = make(
        "github",
        "discord thread receipt",
        "Completed claimant receipt. Source PRs are merged. The earlier bounty and submission have already been accepted.",
    )
    _, _, breakdown, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "github_completed_or_historical_work"
    assert breakdown.get("github_completed_or_historical") == -8


def test_github_can_pay_generic_product_language_is_not_buyer_signal():
    o = make(
        "github",
        "[EPIC] Bookings and Billing",
        "## Business Need Billing can take a lifetime pass or subscription. This Epic adds a bookings engine and Stripe webhook support.",
    )
    _, _, _, eligible, reason = analyze(o)
    assert not eligible
    assert reason == "github_no_commercial_intent"


def test_real_hn_buyer_with_reordered_language_still_alerts():
    o = make(
        "hackernews",
        "Ask HN: Stripe integration",
        "Our customers are waiting. Since yesterday the webhook in our production app keeps timing out. We are looking for a contractor who can get the Stripe integration working again, and we have a budget for it.",
        {"type": "story"},
    )
    _, total, _, eligible, reason = analyze(o)
    assert total >= 13
    assert eligible
    assert reason in {"hn_commercial_intent", "hn_explicit_owned_help", "hn_owned_business_problem"}


def test_real_github_paid_buyer_request_still_alerts():
    o = make(
        "github",
        "Need contractor for broken webhook",
        "Our customer webhook stopped working. We have a budget for a contractor and are willing to pay someone to fix the API integration this week.",
    )
    _, total, _, eligible, reason = analyze(o)
    assert total >= 13
    assert eligible
    assert reason == "github_commercial_intent"


def test_real_manual_business_pain_still_alerts():
    o = make(
        "hackernews",
        "HN comment",
        "Our team manually copies order data into a spreadsheet every week and it takes hours. Is there a way to automate this workflow?",
        {"type": "comment"},
    )
    _, total, _, eligible, reason = analyze(o)
    assert total >= 13
    assert eligible
    assert reason == "hn_manual_process_pain"
