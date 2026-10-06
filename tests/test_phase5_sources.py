from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lead_radar.collectors.discourse import (
    BubbleCommunityCollector,
    HUBSPOT_CATEGORIES,
    HubSpotCommunityCollector,
    ShopifyCommunityCollector,
)
from lead_radar.commercial import assess_commercial
from lead_radar.config import load_keyword_config, load_settings
from lead_radar.database import Database
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType, Opportunity


NOW = datetime.now(timezone.utc)


class FakeResponse:
    def __init__(self, payload, *, url: str):
        self._payload = payload
        self.status_code = 200
        self.headers = {}
        self.request = type("Request", (), {"url": url})()
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakePhase5Client:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, **kwargs):
        recent = (NOW - timedelta(minutes=9)).isoformat().replace("+00:00", "Z")

        if "apis-integrations/80" in url:
            return FakeResponse(
                {
                    "topic_list": {
                        "topics": [
                            {
                                "id": 9201,
                                "slug": "hubspot-webhook-sync-broken",
                                "title": "HubSpot webhook sync stopped working for our CRM",
                                "created_at": recent,
                                "last_posted_at": recent,
                                "highest_post_number": 1,
                                "reply_count": 0,
                            }
                        ]
                    }
                },
                url=url,
            )
        if "/t/9201.json" in url:
            return FakeResponse(
                {
                    "title": "HubSpot webhook sync stopped working for our CRM",
                    "slug": "hubspot-webhook-sync-broken",
                    "details": {"created_by": {"username": "ops_owner"}},
                    "post_stream": {
                        "posts": [
                            {
                                "id": 92011,
                                "post_number": 1,
                                "username": "ops_owner",
                                "created_at": recent,
                                "cooked": (
                                    "<p>Our company uses HubSpot for inbound leads. The webhook stopped working "
                                    "and new leads are not syncing into our CRM. We need help fixing the API integration.</p>"
                                ),
                            }
                        ]
                    },
                },
                url=url,
            )

        if "webhooks-and-events/18.json" in url:
            return FakeResponse(
                {
                    "topic_list": {
                        "topics": [
                            {
                                "id": 9301,
                                "slug": "orders-webhook-not-firing",
                                "title": "Orders/create webhook stopped firing in production",
                                "created_at": recent,
                                "last_posted_at": recent,
                                "highest_post_number": 1,
                                "reply_count": 0,
                            }
                        ]
                    }
                },
                url=url,
            )
        if any(part in url for part in ("fulfillment-inventory/7.json", "product-and-orders-apis/13.json", "authentication/6.json")):
            return FakeResponse({"topic_list": {"topics": []}}, url=url)
        if "/t/9301.json" in url:
            return FakeResponse(
                {
                    "title": "Orders/create webhook stopped firing in production",
                    "slug": "orders-webhook-not-firing",
                    "details": {"created_by": {"username": "app_team"}},
                    "post_stream": {
                        "posts": [
                            {
                                "id": 93011,
                                "post_number": 1,
                                "username": "app_team",
                                "created_at": recent,
                                "cooked": (
                                    "<p>Our production Shopify app stopped receiving orders/create webhooks. "
                                    "Customer orders are no longer reaching our backend and we need help debugging it.</p>"
                                ),
                            }
                        ]
                    },
                },
                url=url,
            )

        if "jobs-freelance/13.json" in url:
            return FakeResponse(
                {
                    "topic_list": {
                        "topics": [
                            {
                                "id": 9401,
                                "slug": "bubble-api-paid-project",
                                "title": "Developer needed for paid Bubble API integration",
                                "created_at": recent,
                                "last_posted_at": recent,
                                "highest_post_number": 1,
                                "reply_count": 0,
                            },
                            {
                                "id": 9402,
                                "slug": "bubble-dev-available",
                                "title": "Bubble developer available for projects",
                                "created_at": recent,
                                "last_posted_at": recent,
                                "highest_post_number": 1,
                                "reply_count": 0,
                            },
                        ]
                    }
                },
                url=url,
            )
        if "/t/9401.json" in url:
            return FakeResponse(
                {
                    "title": "Developer needed for paid Bubble API integration",
                    "slug": "bubble-api-paid-project",
                    "details": {"created_by": {"username": "founder"}},
                    "post_stream": {
                        "posts": [
                            {
                                "id": 94011,
                                "post_number": 1,
                                "username": "founder",
                                "created_at": recent,
                                "cooked": (
                                    "<p>We need a developer to finish a paid project. Our Bubble app needs a custom API "
                                    "integration and webhook backend before launch. Looking for a contractor.</p>"
                                ),
                            }
                        ]
                    },
                },
                url=url,
            )
        if "/t/9402.json" in url:
            return FakeResponse(
                {
                    "title": "Bubble developer available for projects",
                    "slug": "bubble-dev-available",
                    "details": {"created_by": {"username": "seller"}},
                    "post_stream": {
                        "posts": [
                            {
                                "id": 94021,
                                "post_number": 1,
                                "username": "seller",
                                "created_at": recent,
                                "cooked": "<p>I am available for freelance work and open to project-based work.</p>",
                            }
                        ]
                    },
                },
                url=url,
            )

        raise AssertionError(f"Unexpected URL: {url}")


def _matcher() -> Matcher:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return Matcher(load_keyword_config(root / "config" / "keywords.yaml"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collector_cls", "expected_source"),
    [
        (HubSpotCommunityCollector, "hubspot"),
        (ShopifyCommunityCollector, "shopify"),
    ],
)
async def test_business_pain_discourse_collectors_emit_public_topics(
    tmp_path, monkeypatch, collector_cls, expected_source
):
    monkeypatch.setattr("lead_radar.collectors.discourse.httpx.AsyncClient", FakePhase5Client)
    db = Database(tmp_path / f"{expected_source}.db")
    collector = collector_cls(db, max_topics_per_category=10)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == expected_source
    assert item.metadata["platform"] == "discourse"
    assert item.metadata["category_kind"] == "business_pain"
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.POTENTIAL_BUYER, AuthorType.TECHNICAL_BUYER}
    assert assessment.alert_class != AlertClass.REJECT


@pytest.mark.asyncio
async def test_bubble_mixed_marketplace_keeps_buyer_and_rejects_seller(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.discourse.httpx.AsyncClient", FakePhase5Client)
    db = Database(tmp_path / "bubble.db")
    collector = BubbleCommunityCollector(db, max_topics_per_category=10)

    items = await collector.collect()

    assert len(items) == 2
    by_author = {item.author: item for item in items}
    buyer = by_author["founder"]
    seller = by_author["seller"]

    buyer_assessment = assess_commercial(buyer, _matcher().analyze(buyer), now=NOW)
    seller_assessment = assess_commercial(seller, _matcher().analyze(seller), now=NOW)

    assert buyer_assessment.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert buyer_assessment.alert_class in {
        AlertClass.P0_CASH,
        AlertClass.P0_HIGH_VALUE,
        AlertClass.P0_RELATIONSHIP,
        AlertClass.P1,
    }
    assert seller_assessment.author_type == AuthorType.JOB_SEEKER
    assert seller_assessment.alert_class == AlertClass.REJECT


def test_non_author_reply_from_new_discourse_source_cannot_become_buyer():
    opportunity = Opportunity(
        source="shopify",
        external_id="topic:99:post:2",
        url="https://community.shopify.dev/t/x/99/2",
        title="Need help fixing production webhook",
        body="I can help with this, I build Shopify integrations for clients.",
        author="provider",
        created_at=NOW - timedelta(minutes=2),
        metadata={
            "platform": "discourse",
            "category_key": "webhooks_events",
            "category_kind": "business_pain",
            "event_type": "reply",
            "topic_author": "buyer",
            "is_topic_author": False,
            "reply_count": 1,
        },
    )
    assessment = assess_commercial(opportunity, _matcher().analyze(opportunity), now=NOW)
    assert assessment.author_type == AuthorType.UNKNOWN
    assert assessment.alert_class not in {
        AlertClass.P0_CASH,
        AlertClass.P0_HIGH_VALUE,
        AlertClass.P0_RELATIONSHIP,
    }


def test_hubspot_uses_extensionless_category_path_for_accept_json():
    assert HUBSPOT_CATEGORIES[0].path == "c/develop-build-integrate/apis-integrations/80"


def test_bubble_default_bootstrap_lookback_is_seven_days(tmp_path, monkeypatch):
    monkeypatch.delenv("BUBBLE_LOOKBACK_HOURS", raising=False)
    settings = load_settings(tmp_path)
    assert settings.bubble_lookback_hours == 168


@pytest.mark.asyncio
async def test_widened_lookback_recovers_activity_only_topic_cursor(tmp_path, monkeypatch):
    old_now = NOW - timedelta(days=5)
    recent_for_wide_window = old_now.isoformat().replace("+00:00", "Z")

    class LookbackClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, **kwargs):
            if "jobs-freelance/13.json" in url:
                return FakeResponse(
                    {
                        "topic_list": {
                            "topics": [
                                {
                                    "id": 9501,
                                    "slug": "older-paid-bubble-project",
                                    "title": "Paid Bubble API project",
                                    "created_at": recent_for_wide_window,
                                    "last_posted_at": recent_for_wide_window,
                                    "highest_post_number": 1,
                                    "reply_count": 0,
                                }
                            ]
                        }
                    },
                    url=url,
                )
            if "/t/9501.json" in url:
                return FakeResponse(
                    {
                        "title": "Paid Bubble API project",
                        "slug": "older-paid-bubble-project",
                        "details": {"created_by": {"username": "buyer"}},
                        "post_stream": {
                            "posts": [
                                {
                                    "id": 95011,
                                    "post_number": 1,
                                    "username": "buyer",
                                    "created_at": recent_for_wide_window,
                                    "cooked": "<p>Need a contractor for a paid API integration.</p>",
                                }
                            ]
                        },
                    },
                    url=url,
                )
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("lead_radar.collectors.discourse.httpx.AsyncClient", LookbackClient)
    db = Database(tmp_path / "bubble-lookback.db")

    narrow = BubbleCommunityCollector(db, lookback_hours=72, max_topics_per_category=10)
    assert await narrow.collect() == []

    wide = BubbleCommunityCollector(db, lookback_hours=168, max_topics_per_category=10)
    items = await wide.collect()
    assert len(items) == 1
    assert items[0].external_id == "topic:9501:post:95011"


def test_hubspot_uses_browser_compatible_headers(tmp_path):
    db = Database(tmp_path / "hubspot-headers.db")
    collector = HubSpotCommunityCollector(db)
    assert collector.request_headers["User-Agent"].startswith("Mozilla/5.0")
    assert collector.request_headers["Referer"] == "https://community.hubspot.com/"
