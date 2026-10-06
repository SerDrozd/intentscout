from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lead_radar.collectors.rss import RSSCollector, load_feed_definitions
from lead_radar.commercial import assess_commercial
from lead_radar.config import load_keyword_config, load_settings
from lead_radar.database import Database
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType


NOW = datetime.now(timezone.utc)


class FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}
        self.request = type("Request", (), {"url": "https://example.test/feed"})()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("failed", request=self.request, response=self)


class FakeRSSClient:
    requests: list[dict[str, str]] = []
    call_count = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, headers=None, **kwargs):
        type(self).call_count += 1
        type(self).requests.append(dict(headers or {}))
        if type(self).call_count == 2:
            return FakeResponse(b"", status_code=304)
        recent = NOW.strftime("%a, %d %b %Y %H:%M:%S +0000")
        stale = (NOW - timedelta(days=20)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <title>Jobs</title>
          <item>
            <guid>job-1</guid>
            <title>Freelance Python automation contractor needed</title>
            <link>https://jobs.example.test/1</link>
            <pubDate>{recent}</pubDate>
            <author>Founder</author>
            <description><![CDATA[
              We need a contractor to fix our API/webhook automation. This is a paid freelance project
              for our business and we want someone who can start this week.
            ]]></description>
          </item>
          <item>
            <guid>job-old</guid>
            <title>Old integration project</title>
            <link>https://jobs.example.test/old</link>
            <pubDate>{stale}</pubDate>
            <description>Need a freelancer for an API integration.</description>
          </item>
        </channel></rss>""".encode()
        return FakeResponse(
            body,
            headers={"ETag": '"feed-v1"', "Last-Modified": recent},
        )


class FakeAtomClient:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, headers=None, **kwargs):
        recent = NOW.isoformat().replace("+00:00", "Z")
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>Buyer pain</title>
          <entry>
            <id>tag:example.test,2026:abc</id>
            <title>Our CRM sync is failing</title>
            <link rel="alternate" href="https://example.test/posts/abc" />
            <published>{recent}</published>
            <author><name>Ops Owner</name></author>
            <content type="html">Our company webhook stopped syncing leads. We need help fixing the API integration.</content>
          </entry>
        </feed>""".encode()
        return FakeResponse(body)


def _matcher() -> Matcher:
    root = Path(__file__).resolve().parents[1]
    return Matcher(load_keyword_config(root / "config" / "keywords.yaml"))


def _write_config(path: Path, url: str, *, category_kind: str = "marketplace") -> None:
    path.write_text(
        f"""feeds:\n  - key: test_feed\n    source: testfeed\n    name: Test Feed\n    url: \"{url}\"\n    enabled: true\n    category_kind: {category_kind}\n    lookback_hours: 168\n    max_items: 20\n    min_poll_minutes: 0\n""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_rss2_collector_filters_stale_and_uses_conditional_get(tmp_path, monkeypatch):
    FakeRSSClient.call_count = 0
    FakeRSSClient.requests = []
    monkeypatch.setattr("lead_radar.collectors.rss.httpx.AsyncClient", FakeRSSClient)
    config_path = tmp_path / "feeds.yaml"
    _write_config(config_path, "https://example.test/feed")
    db = Database(tmp_path / "rss.db")
    collector = RSSCollector(db, config_path=config_path)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == "testfeed"
    assert item.external_id == "job-1"
    assert item.metadata["platform"] == "rss"
    assert item.metadata["category_kind"] == "marketplace"
    assert item.author == "Founder"
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER}
    assert assessment.alert_class != AlertClass.REJECT
    assert db.get_state("rss:test_feed:cursor") is not None

    second = await collector.collect()
    assert second == []
    assert FakeRSSClient.requests[1]["If-None-Match"] == '"feed-v1"'
    assert "If-Modified-Since" in FakeRSSClient.requests[1]


@pytest.mark.asyncio
async def test_atom_collector_extracts_author_link_and_business_pain(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.rss.httpx.AsyncClient", FakeAtomClient)
    config_path = tmp_path / "feeds.yaml"
    _write_config(config_path, "https://example.test/atom", category_kind="business_pain")
    collector = RSSCollector(Database(tmp_path / "atom.db"), config_path=config_path)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.external_id == "tag:example.test,2026:abc"
    assert item.url == "https://example.test/posts/abc"
    assert item.author == "Ops Owner"
    assert item.metadata["category_kind"] == "business_pain"
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.POTENTIAL_BUYER, AuthorType.TECHNICAL_BUYER}


def test_feed_config_and_phase6b_settings(tmp_path, monkeypatch):
    config_path = tmp_path / "feeds.yaml"
    config_path.write_text(
        """feeds:\n  - key: enabled\n    source: custom\n    name: Custom Feed\n    url: https://example.test/feed.xml\n    enabled: true\n  - key: disabled\n    source: custom\n    name: Disabled\n    url: https://example.test/disabled.xml\n    enabled: false\n""",
        encoding="utf-8",
    )
    feeds = load_feed_definitions(config_path)
    assert len(feeds) == 2
    assert feeds[0].enabled is True
    assert feeds[1].enabled is False

    monkeypatch.delenv("RSS_ENABLED", raising=False)
    monkeypatch.delenv("RSS_CONCURRENCY", raising=False)
    settings = load_settings(tmp_path)
    assert settings.rss_enabled is True
    assert settings.rss_concurrency == 4
    assert settings.rss_feeds_path == (tmp_path / "config" / "feeds.yaml").resolve()
    assert settings.rss_local_feeds_path == (tmp_path / "config" / "feeds.local.yaml").resolve()
    assert settings.google_alerts_queries_path == (tmp_path / "config" / "google_alerts_queries.yaml").resolve()


def test_bundled_jobicy_feeds_are_hourly_marketplace_sources() -> None:
    root = Path(__file__).resolve().parents[1]
    feeds = load_feed_definitions(root / "config" / "feeds.yaml")
    jobicy = [feed for feed in feeds if feed.source == "jobicy" and feed.enabled]
    assert len(jobicy) == 2
    assert all(feed.category_kind == "marketplace" for feed in jobicy)
    assert all(feed.min_poll_minutes >= 60 for feed in jobicy)
