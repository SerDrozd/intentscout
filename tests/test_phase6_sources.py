from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lead_radar.collectors.bluesky import BlueskyCollector
from lead_radar.collectors.hn_algolia import HackerNewsAlgoliaCollector
from lead_radar.commercial import assess_commercial
from lead_radar.config import load_keyword_config, load_settings
from lead_radar.database import Database
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType


NOW = datetime.now(timezone.utc)


class FakeResponse:
    def __init__(self, payload: dict, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.request = type("Request", (), {"url": "https://example.test"})()

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("failed", request=self.request, response=self)

    def json(self):
        return self._payload


class FakeHNClient:
    def __init__(self, *args, **kwargs):
        self.params: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, params=None, **kwargs):
        assert "hn.algolia.com/api/v1/search_by_date" in url
        self.params.append(dict(params or {}))
        ts = int(NOW.timestamp())
        return FakeResponse(
            {
                "hits": [
                    {
                        "objectID": "555001",
                        "created_at_i": ts,
                        "author": "founder123",
                        "story_title": "Ask HN: Need a developer to fix our Stripe webhook",
                        "comment_text": (
                            "Our customers are waiting and the Stripe webhook keeps failing. "
                            "We need a developer to fix it and can pay for a quick solution."
                        ),
                        "story_id": 555000,
                        "parent_id": 555000,
                    }
                ]
            }
        )


class FakeBlueskyClient:
    def __init__(self, *args, **kwargs):
        self.params: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, params=None, **kwargs):
        assert url.endswith("/xrpc/app.bsky.feed.searchPosts")
        self.params.append(dict(params or {}))
        return FakeResponse(
            {
                "posts": [
                    {
                        "uri": "at://did:plc:abc/app.bsky.feed.post/3abcxyz",
                        "author": {
                            "did": "did:plc:abc",
                            "handle": "opsfounder.bsky.social",
                            "displayName": "Ops Founder",
                        },
                        "record": {
                            "text": (
                                "Need a developer to help with our HubSpot webhook. Leads stopped syncing into our "
                                "CRM this morning and we are willing to pay to get it fixed today."
                            ),
                            "createdAt": NOW.isoformat().replace("+00:00", "Z"),
                        },
                        "replyCount": 0,
                        "likeCount": 1,
                        "repostCount": 0,
                        "quoteCount": 0,
                    }
                ]
            }
        )


def _matcher() -> Matcher:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return Matcher(load_keyword_config(root / "config" / "keywords.yaml"))


@pytest.mark.asyncio
async def test_hn_algolia_collector_emits_targeted_comment(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.hn_algolia.httpx.AsyncClient", FakeHNClient)
    db = Database(tmp_path / "hn.db")
    collector = HackerNewsAlgoliaCollector(db, queries_per_cycle=1, results_per_query=10)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == "hackernews"
    assert item.external_id == "555001"
    assert item.metadata["platform"] == "hn_algolia"
    assert item.title.startswith("Reply:")
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert assessment.alert_class != AlertClass.REJECT
    assert db.get_state("hn_algolia:hire_developer:cursor") is not None


@pytest.mark.asyncio
async def test_bluesky_public_search_emits_fresh_buyer_post(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.bluesky.httpx.AsyncClient", FakeBlueskyClient)
    db = Database(tmp_path / "bsky.db")
    collector = BlueskyCollector(db, queries_per_cycle=1, results_per_query=10)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == "bluesky"
    assert item.author == "opsfounder.bsky.social"
    assert item.url == "https://bsky.app/profile/opsfounder.bsky.social/post/3abcxyz"
    assert item.metadata["reply_count"] == 0
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert assessment.alert_class != AlertClass.REJECT


def test_phase6_settings_and_hubspot_default(tmp_path, monkeypatch):
    for name in (
        "HN_ALGOLIA_ENABLED",
        "BLUESKY_ENABLED",
        "HUBSPOT_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(tmp_path)
    assert settings.hn_algolia_enabled is True
    assert settings.hn_algolia_queries_per_cycle == 6
    assert settings.bluesky_enabled is True
    assert settings.bluesky_queries_per_cycle == 6
    assert settings.hubspot_enabled is False
