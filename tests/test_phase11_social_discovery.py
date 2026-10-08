from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import lead_radar.__main__ as cli
from lead_radar.app import RadarApp
from lead_radar.collectors.bluesky import QUERY_FAMILIES
from lead_radar.config import Settings
from lead_radar.database import Database
from lead_radar.google_alerts import build_google_alert_plan, load_google_alert_queries
from lead_radar.social import SOCIAL_PLATFORMS, build_social_opportunity, canonical_social_url

ROOT = Path(__file__).resolve().parents[1]
BUYER_TEXT = (
    "We need a Python developer to repair a production webhook and API integration. "
    "This is a paid freelance task with a fixed budget. DM me to discuss the scope."
)


@pytest.mark.parametrize("platform,url,expected", [
    ("x", "https://twitter.com/Founder/status/123456?utm_source=share", "https://x.com/founder/status/123456"),
    ("facebook", "https://www.facebook.com/groups/abc/posts/123/?fbclid=x", "https://facebook.com/groups/abc/posts/123"),
    ("reddit", "https://old.reddit.com/r/forhire/comments/abcde/my_post/?ref=x", "https://reddit.com/r/forhire/comments/abcde/my_post"),
    ("threads", "https://www.threads.net/@founder/post/ABCD?utm_source=a", "https://threads.com/@founder/post/ABCD"),
    ("telegram", "https://t.me/publicjobs/123?single", "https://t.me/publicjobs/123"),
    ("discord", "https://discord.com/channels/123/456/789", "https://discord.com/channels/123/456/789"),
])
def test_manual_social_post_permalink_canonicalization(platform: str, url: str, expected: str) -> None:
    assert canonical_social_url(platform, url) == expected


@pytest.mark.parametrize("platform,url", [
    ("x", "https://x.com/founder"),
    ("x", "https://x.com.evil.test/founder/status/123"),
    ("x", "https://x.com/founder/status/not-a-post"),
    ("facebook", "https://facebook.com/groups/123"),
    ("reddit", "https://reddit.com/r/forhire/new"),
    ("threads", "https://threads.com/@account"),
    ("telegram", "https://t.me/+secret"),
    ("telegram", "https://t.me/c/123/567"),
    ("discord", "https://discord.com/invite/xyz"),
    ("x", "file:///etc/passwd"),
])
def test_social_import_rejects_invalid_post_links(platform: str, url: str) -> None:
    with pytest.raises(ValueError):
        canonical_social_url(platform, url)


def test_social_opportunity_has_dedup_and_manual_metadata() -> None:
    url = "https://x.com/founder/status/123?utm_source=copy"
    a = build_social_opportunity(platform="x", url=url, body=BUYER_TEXT, author="Founder")
    b = build_social_opportunity(platform="x", url="https://x.com/founder/status/123", body=BUYER_TEXT)
    assert a.source == "x"
    assert a.external_id == b.external_id
    assert a.metadata["platform"] == "manual_social"
    assert a.metadata["created_at_assumed"] is True
    assert a.metadata["query_key"] == "social_manual_x"
    assert a.metadata["event_type"] == "topic"


def test_social_opportunity_rejects_short_text_or_future() -> None:
    with pytest.raises(ValueError):
        build_social_opportunity(platform="x", url="https://x.com/founder/status/123", body="short")
    with pytest.raises(ValueError):
        build_social_opportunity(
            platform="x", url="https://x.com/founder/status/123", body=BUYER_TEXT,
            created_at="2099-10-08T12:00:00Z",
        )


def test_social_buyer_appears_on_board_and_seller_does_not(tmp_path: Path) -> None:
    config = Settings(
        db_path=tmp_path / "radar.db", keywords_path=ROOT / "config/keywords.yaml",
        poll_interval_seconds=180, log_level="INFO", alert_threshold=13,
        review_threshold=8, alert_review_queue=False, cash_alert_threshold=72,
        value_alert_threshold=74, relationship_alert_threshold=76,
        max_alerts_per_cycle=5, telegram_min_send_interval_seconds=1.15,
        hn_bootstrap_items=300, hn_max_items_per_cycle=500, hn_concurrency=20,
        github_token=None, github_lookback_hours=24, github_results_per_query=50,
        make_lookback_hours=168, make_topics_per_category=30,
        n8n_lookback_hours=168, n8n_topics_per_category=30,
        discourse_concurrency=6, telegram_bot_token=None, telegram_chat_id=None,
        hn_enabled=False, bluesky_enabled=False, github_enabled=False, rss_enabled=False,
        make_enabled=False, n8n_enabled=False, shopify_enabled=False, bubble_enabled=False,
        airtable_enabled=False, zapier_enabled=False, retool_enabled=False, xano_enabled=False,
        flutterflow_enabled=False, llm_enabled=False,
    )
    app = RadarApp(config)
    buyer = build_social_opportunity(platform="x", url="https://x.com/founder/status/123",
                                     body=BUYER_TEXT, created_at=datetime.now(timezone.utc).isoformat())
    result = app.ingest_manual(buyer)
    assert result.status == "stored"
    assert result.opportunity_id is not None
    board = app.buyer_board(days=1, limit=20)
    assert any(int(row["id"]) == result.opportunity_id for _, row in board)
    seller = build_social_opportunity(
        platform="reddit", url="https://reddit.com/r/forhire/comments/abc/my_services",
        body=("Available for hire! I offer paid automation and Python API integration. "
              "My rate starts at $40/hour, and I am seeking projects."),
    )
    app.ingest_manual(seller)
    assert all("my_services" not in str(row["url"]) for _, row in app.buyer_board(days=1))


def test_social_query_pack_and_bluesky_expansion() -> None:
    queries = load_google_alert_queries(ROOT / "config/google_alerts_queries.yaml")
    social = tuple(item for item in queries if item.key.startswith("social_"))
    assert len(social) >= 7
    assert len({item.feed_key for item in queries}) == len(queries)
    plan = build_google_alert_plan(social, ())
    assert all(item.status == "UNCONFIGURED" for item in plan)
    assert any("n8n" in item.query for item in plan)
    assert len(QUERY_FAMILIES) >= 20
    assert len({item.key for item in QUERY_FAMILIES}) == len(QUERY_FAMILIES)


@pytest.mark.asyncio
async def test_social_add_cli_import_duplicate(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    class FakeRadar:
        def __init__(self, _: object) -> None:
            pass

        def ingest_manual(self, _: object) -> SimpleNamespace:
            return SimpleNamespace(status="duplicate")

    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "RadarApp", FakeRadar)
    args = cli.build_parser().parse_args([
        "social-add", "--platform", "x", "--url", "https://x.com/founder/status/123", "--text", BUYER_TEXT,
    ])
    assert await cli.async_main(args) == 0
    assert "already in the radar database" in capsys.readouterr().out


def test_platforms_are_explicit_and_no_linkedin() -> None:
    assert SOCIAL_PLATFORMS == ("x", "facebook", "reddit", "threads", "telegram", "discord")
    assert "linkedin" not in SOCIAL_PLATFORMS
    assert Database is not None


def test_indexed_social_hiring_is_not_treated_like_news() -> None:
    from lead_radar.commercial import has_direct_buyer_evidence
    from lead_radar.config import load_keyword_config
    from lead_radar.matcher import Matcher
    from lead_radar.models import Opportunity

    classifier = Matcher(load_keyword_config(ROOT / "config/keywords.yaml"))
    sample = Opportunity(
        source="google_alerts", external_id="indexed-social-1",
        url="https://x.com/founder/status/123456", title="Hiring a Python API developer for a paid webhook fix",
        body="We need a developer to repair a broken production API integration for our business.",
        author=None, created_at=datetime.now(timezone.utc),
        metadata={"platform": "rss", "event_type": "topic", "category_kind": "marketplace",
                  "query_key": "social_x_api_hiring"},
    )
    assert has_direct_buyer_evidence(sample, classifier.analyze(sample))
    sample.url = "https://www.nst.com.my/news/news-example"
    assert not has_direct_buyer_evidence(sample, classifier.analyze(sample))
