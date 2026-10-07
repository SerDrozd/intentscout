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
