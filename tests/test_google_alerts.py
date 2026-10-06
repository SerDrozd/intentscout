from __future__ import annotations

from pathlib import Path

import pytest

from lead_radar.collectors.rss import RSSCollector, load_feed_definitions
from lead_radar.database import Database
from lead_radar.google_alerts import (
    build_google_alert_plan,
    combined_feed_definitions,
    is_google_alert_feed_url,
    load_google_alert_queries,
    upsert_google_alert_feed,
)


ROOT = Path(__file__).resolve().parents[1]


def test_query_pack_has_unique_prioritized_entries() -> None:
    queries = load_google_alert_queries(ROOT / "config" / "google_alerts_queries.yaml")
    assert len(queries) >= 10
    assert len({item.key for item in queries}) == len(queries)
    assert len({item.feed_key for item in queries}) == len(queries)
    assert {item.priority for item in queries} <= {"P0", "P1", "P2"}
    assert any(item.priority == "P0" for item in queries)
    assert all(item.rationale for item in queries)


def test_google_alert_url_validation_rejects_placeholders_and_non_google() -> None:
    assert is_google_alert_feed_url("https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED") is True
    assert is_google_alert_feed_url("https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED") is False
    assert is_google_alert_feed_url("https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED") is False
    assert is_google_alert_feed_url("https://example.com/alerts/feeds/123/456") is False


def test_alerts_add_writes_only_local_feed_and_plan_becomes_active(tmp_path: Path) -> None:
    public_path = tmp_path / "feeds.yaml"
    local_path = tmp_path / "feeds.local.yaml"
    public_path.write_text("feeds: []\n", encoding="utf-8")
    query = load_google_alert_queries(ROOT / "config" / "google_alerts_queries.yaml")[0]

    upsert_google_alert_feed(local_path, query, "https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED")

    local_feeds = load_feed_definitions(local_path)
    assert len(local_feeds) == 1
    assert local_feeds[0].source == "google_alerts"
    assert local_feeds[0].query_key == query.key
    assert local_feeds[0].search_query == query.query
    plan = build_google_alert_plan((query,), combined_feed_definitions(public_path, local_path))
    assert plan[0].status == "ACTIVE"


def test_local_feed_overrides_same_public_key(tmp_path: Path) -> None:
    public_path = tmp_path / "feeds.yaml"
    local_path = tmp_path / "feeds.local.yaml"
    public_path.write_text(
        """feeds:\n  - key: same\n    source: public\n    name: public\n    url: https://example.test/public.xml\n    enabled: true\n""",
        encoding="utf-8",
    )
    local_path.write_text(
        """feeds:\n  - key: same\n    source: local\n    name: local\n    url: https://example.test/local.xml\n    enabled: true\n""",
        encoding="utf-8",
    )
    feeds = combined_feed_definitions(public_path, local_path)
    assert len(feeds) == 1
    assert feeds[0].source == "local"


def test_database_query_attribution_prefers_query_key() -> None:
    metadata = '{"feed_key":"google_alerts_feed","query_key":"buyer_api"}'
    assert Database._attribution_key("google_alerts", metadata) == "buyer_api"


@pytest.mark.asyncio
async def test_rss_collector_merges_local_overlay_without_public_secret(tmp_path: Path, monkeypatch) -> None:
    class Response:
        status_code = 304
        headers: dict[str, str] = {}
        content = b""
        request = type("Request", (), {"url": "https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED"})()

        def raise_for_status(self) -> None:
            return None

    class Client:
        requested: list[str] = []

        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, *, headers=None, **kwargs):
            type(self).requested.append(str(url))
            return Response()

    monkeypatch.setattr("lead_radar.collectors.rss.httpx.AsyncClient", Client)
    public_path = tmp_path / "feeds.yaml"
    local_path = tmp_path / "feeds.local.yaml"
    public_path.write_text("feeds: []\n", encoding="utf-8")
    query = load_google_alert_queries(ROOT / "config" / "google_alerts_queries.yaml")[0]
    upsert_google_alert_feed(local_path, query, "https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED")

    collector = RSSCollector(
        Database(tmp_path / "radar.db"),
        config_path=public_path,
        extra_config_paths=(local_path,),
    )
    assert await collector.collect() == []
    assert Client.requested == ["https://www.google.com/alerts/feeds/EXAMPLE_USER/EXAMPLE_FEED"]
