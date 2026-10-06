from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from lead_radar.collectors.web_community import (
    AirtableWebCollector,
    HubSpotWebCollector,
    ZapierWebCollector,
)
from lead_radar.commercial import assess_commercial
from lead_radar.config import load_keyword_config, load_settings
from lead_radar.database import Database
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType


NOW = datetime.now(timezone.utc)


class FakeResponse:
    def __init__(self, text: str, *, url: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = type("Request", (), {"url": url})()
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("failed", request=self.request, response=self)


class FakeWebClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        recent = (NOW - timedelta(minutes=12)).isoformat().replace("+00:00", "Z")
        old = (NOW - timedelta(days=30)).isoformat().replace("+00:00", "Z")

        if "community.hubspot.com/c/" in url:
            return FakeResponse(
                '<html><body><a href="/t/hubspot-sync-broken/150001">HubSpot sync broken</a></body></html>',
                url=url,
            )
        if "community.hubspot.com/t/hubspot-sync-broken/150001" in url:
            payload = {
                "@context": "https://schema.org",
                "@type": "DiscussionForumPosting",
                "headline": "HubSpot webhook sync stopped working for our CRM",
                "articleBody": (
                    "Our company receives inbound leads in HubSpot, but the webhook stopped working and leads are not "
                    "syncing into our CRM. We need help fixing the API integration before the next campaign."
                ),
                "datePublished": recent,
                "author": {"@type": "Person", "name": "OpsOwner"},
                "commentCount": 1,
            }
            return FakeResponse(
                f'<html><script type="application/ld+json">{json.dumps(payload)}</script></html>',
                url=url,
            )

        if "community.airtable.com/jobs-board-16" in url and url.rstrip("/").endswith("jobs-board-16"):
            return FakeResponse(
                '<html><body><a href="/jobs-board-16/need-airtable-api-automation-49001">Need Airtable help</a>'
                '<a href="/jobs-board-16/old-job-47000">Old job</a></body></html>',
                url=url,
            )
        if "need-airtable-api-automation-49001" in url:
            payload = {
                "@context": "https://schema.org",
                "@type": "Question",
                "name": "Looking to hire consultant for Airtable automation and API cleanup",
                "text": (
                    "We run a small company and are looking for freelance support. Several Airtable workflows are "
                    "breaking and we need a consultant to clean up the automation and Make API integration."
                ),
                "datePublished": recent,
                "author": {"name": "Founder"},
                "commentCount": 0,
            }
            return FakeResponse(
                f'<html><script type="application/ld+json">{json.dumps(payload)}</script></html>',
                url=url,
            )
        if "old-job-47000" in url:
            payload = {
                "@context": "https://schema.org",
                "@type": "Question",
                "name": "Old Airtable job",
                "text": "Looking for a contractor for an Airtable API integration.",
                "datePublished": old,
                "author": {"name": "OldFounder"},
            }
            return FakeResponse(
                f'<html><script type="application/ld+json">{json.dumps(payload)}</script></html>',
                url=url,
            )

        if "community.zapier.com/code-webhooks-52" in url and url.rstrip("/").endswith("code-webhooks-52"):
            return FakeResponse(
                '<html><body><a href="/code-webhooks-52/critical-api-custom-fields-55001">Need API help</a></body></html>',
                url=url,
            )
        if "community.zapier.com/troubleshooting-99" in url and url.rstrip("/").endswith("troubleshooting-99"):
            return FakeResponse("<html><body></body></html>", url=url)
        if "community.zapier.com/how-do-i-3" in url and url.rstrip("/").endswith("how-do-i-3"):
            return FakeResponse("<html><body></body></html>", url=url)
        if "critical-api-custom-fields-55001" in url:
            return FakeResponse(
                f'''<html><head>
                <meta property="og:title" content="Need help using API to update custom fields">
                <meta name="author" content="BusinessOwner">
                <meta property="article:published_time" content="{recent}">
                </head><body><article>
                We use a webhook into Zapier to create jobs in our CRM. Standard fields work, but five custom fields
                require the vendor API and I cannot get the request working. This is critical for our operations and
                we need help getting the API mapping finished.
                </article></body></html>''',
                url=url,
            )

        raise AssertionError(f"Unexpected URL: {url}")


def _matcher() -> Matcher:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return Matcher(load_keyword_config(root / "config" / "keywords.yaml"))


@pytest.mark.asyncio
async def test_hubspot_public_html_collector_emits_business_pain(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.web_community.httpx.AsyncClient", FakeWebClient)
    collector = HubSpotWebCollector(Database(tmp_path / "hubspot.db"), max_topics_per_section=5)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == "hubspot"
    assert item.metadata["platform"] == "community_web"
    assert item.metadata["category_kind"] == "business_pain"
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.POTENTIAL_BUYER, AuthorType.TECHNICAL_BUYER}
    assert assessment.alert_class != AlertClass.REJECT


@pytest.mark.asyncio
async def test_airtable_jobs_board_is_marketplace_and_filters_old_topics(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.web_community.httpx.AsyncClient", FakeWebClient)
    collector = AirtableWebCollector(Database(tmp_path / "airtable.db"), max_topics_per_section=5)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == "airtable"
    assert item.metadata["category_kind"] == "marketplace"
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.BUYER, AuthorType.TECHNICAL_BUYER, AuthorType.AGENCY_BUYER}
    assert assessment.alert_class != AlertClass.REJECT


@pytest.mark.asyncio
async def test_zapier_public_html_collector_handles_meta_and_article_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.web_community.httpx.AsyncClient", FakeWebClient)
    collector = ZapierWebCollector(Database(tmp_path / "zapier.db"), max_topics_per_section=5)

    items = await collector.collect()

    assert len(items) == 1
    item = items[0]
    assert item.source == "zapier"
    assert "custom fields" in item.title.casefold()
    assert "five custom fields" in item.body.casefold()
    assert item.author == "BusinessOwner"
    assert item.metadata["category_kind"] == "business_pain"
    assessment = assess_commercial(item, _matcher().analyze(item), now=NOW)
    assert assessment.author_type in {AuthorType.POTENTIAL_BUYER, AuthorType.TECHNICAL_BUYER}


def test_phase5b_settings_are_enabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AIRTABLE_ENABLED", raising=False)
    monkeypatch.delenv("ZAPIER_ENABLED", raising=False)
    settings = load_settings(tmp_path)
    assert settings.airtable_enabled is True
    assert settings.zapier_enabled is True
    assert settings.airtable_lookback_hours == 336
    assert settings.zapier_lookback_hours == 168
    assert settings.web_fetch_concurrency == 4
