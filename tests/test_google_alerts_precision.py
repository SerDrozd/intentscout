from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lead_radar.commercial import assess_commercial
from lead_radar.config import load_keyword_config
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, Opportunity


ROOT = Path(__file__).resolve().parents[1]
MATCHER = Matcher(load_keyword_config(ROOT / "config" / "keywords.yaml"))
NOW = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)


def _assess(title: str, body: str, url: str):
    opportunity = Opportunity(
        source="google_alerts",
        external_id=title,
        url=url,
        title=title,
        body=body,
        author=None,
        created_at=NOW,
        metadata={"platform": "rss", "query_key": "test"},
    )
    return assess_commercial(opportunity, MATCHER.analyze(opportunity), now=NOW)


def test_google_alert_news_match_is_capped_to_p2_without_direct_buyer_language() -> None:
    assessment = _assess(
        "ChatGPT launches new API features",
        "The company announced an API integration update. Analysts say customers need help adapting workflows.",
        "https://example-news.test/article/chatgpt-api",
    )
    assert assessment.alert_class == AlertClass.P2
    assert assessment.features["google_alert_direct_buyer_signal"] is False


def test_google_alert_direct_help_request_can_remain_actionable() -> None:
    assessment = _assess(
        "Need help with CRM project (urgent) - Freelancer",
        "I need help fixing existing backend API issues, JSON request/response problems, and frontend-to-backend communication.",
        "https://www.google.com/url?rct=j&url=https%3A%2F%2Fwww.freelancer.com%2Fprojects%2Fapi-development%2Fneed-help-with-crm-project",
    )
    assert assessment.alert_class in {AlertClass.P0_CASH, AlertClass.P0_HIGH_VALUE, AlertClass.P1}
    assert assessment.features["google_alert_direct_buyer_signal"] is True


def test_google_alert_quoted_willing_to_pay_is_not_a_buyer_without_technical_request() -> None:
    assessment = _assess(
        "Singapore's HDB market splits: Record S$1m deals, slower overall sales | Malay Mail",
        'The report says buyers are "willing to pay" more for homes. They need help with rising costs.',
        "https://www.malaymail.com/news/singapore/2026/10/07/hdb-market-splits/238098",
    )
    assert assessment.alert_class == AlertClass.P2
    assert assessment.author_type.value == "UNKNOWN"
    assert assessment.commercial_intent.value == "NONE"
    assert assessment.features["google_alert_direct_buyer_signal"] is False


def test_google_alert_editorial_quote_with_api_still_not_a_buyer() -> None:
    assessment = _assess(
        "A Syracuse legend takes the reins - Winnipeg Free Press",
        'The coach said "we are looking for" more staff; an API can also help the team.',
        "https://www.winnipegfreepress.com/uncategorized/2026/10/07/a-syracuse-legend",
    )
    assert assessment.alert_class == AlertClass.P2
    assert assessment.author_type.value == "UNKNOWN"
    assert assessment.commercial_intent.value == "NONE"


def test_google_alert_non_editorial_willing_to_pay_news_without_hiring_is_not_a_buyer() -> None:
    assessment = _assess(
        "Oil firms willing to pay more for fuel",
        "Industry analysts say companies are willing to pay for imports.",
        "https://random-publication.test/energy/oil-price-report",
    )
    assert assessment.alert_class == AlertClass.P2
    assert assessment.features["google_alert_direct_buyer_signal"] is False


def test_google_alert_direct_api_help_from_forum_is_still_reviewable() -> None:
    assessment = _assess(
        "Need help with an API webhook",
        "We need help fixing our Stripe webhook that is failing to sync payments. Can someone fix it?",
        "https://community.example.test/t/stripe-api-webhook/123",
    )
    assert assessment.features["google_alert_direct_buyer_signal"] is True
    assert assessment.author_type.value in {"TECHNICAL_BUYER", "POTENTIAL_BUYER", "BUYER"}


def test_google_alert_first_person_paid_python_task_is_still_buyer() -> None:
    assessment = _assess(
        "Python API project help",
        "We are looking for a Python developer to fix an API integration. This is paid work.",
        "https://forum.example.test/t/paid-python-api-work/456",
    )
    assert assessment.features["google_alert_direct_buyer_signal"] is True
    assert assessment.author_type.value in {"TECHNICAL_BUYER", "POTENTIAL_BUYER", "BUYER"}
