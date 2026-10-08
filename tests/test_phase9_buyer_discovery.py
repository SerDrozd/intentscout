from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lead_radar.app import RadarApp
from lead_radar.collectors import FlutterFlowWebCollector, RetoolCommunityCollector, XanoWebCollector
from lead_radar.commercial import assess_commercial
from lead_radar.config import Settings, load_keyword_config
from lead_radar.database import Database
from lead_radar.manual import build_linkedin_opportunity
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType
from lead_radar.optimizer import OptimizationRecommendation


ROOT = Path(__file__).resolve().parents[1]
MATCHER = Matcher(load_keyword_config(ROOT / "config" / "keywords.yaml"))


def settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = dict(
        db_path=tmp_path / "radar.db",
        keywords_path=ROOT / "config" / "keywords.yaml",
        poll_interval_seconds=180,
        log_level="INFO",
        alert_threshold=13,
        review_threshold=8,
        alert_review_queue=False,
        cash_alert_threshold=72,
        value_alert_threshold=74,
        relationship_alert_threshold=76,
        max_alerts_per_cycle=5,
        telegram_min_send_interval_seconds=1.15,
        hn_enabled=False,
        hn_bootstrap_items=300,
        hn_max_items_per_cycle=500,
        hn_concurrency=20,
        github_enabled=False,
        github_token=None,
        github_lookback_hours=24,
        github_results_per_query=50,
        make_enabled=False,
        make_lookback_hours=48,
        make_topics_per_category=20,
        n8n_enabled=False,
        n8n_lookback_hours=48,
        n8n_topics_per_category=20,
        discourse_concurrency=6,
        telegram_bot_token=None,
        telegram_chat_id=None,
        llm_enabled=False,
        openai_api_key=None,
        rss_enabled=False,
        hn_algolia_enabled=False,
        bluesky_enabled=False,
        hubspot_enabled=False,
        shopify_enabled=False,
        bubble_enabled=False,
        airtable_enabled=False,
        zapier_enabled=False,
        retool_enabled=False,
        xano_enabled=False,
        flutterflow_enabled=False,
    )
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _roi_row(source: str, *, leads: int = 100, p0: int = 0, p1: int = 0) -> dict[str, object]:
    return {
        "source": source,
        "key": "all",
        "leads": leads,
        "p0": p0,
        "p1": p1,
        "contacted": 0,
        "replied": 0,
        "proposal": 0,
        "won": 0,
        "lost": 0,
        "ignored": 0,
        "revenue": 0.0,
        "llm_cost": 0.0,
    }


def test_protected_buyer_source_ignores_deprioritized_poll_interval(tmp_path: Path) -> None:
    app = RadarApp(
        settings(
            tmp_path,
            adaptive_scheduling_enabled=True,
            protected_buyer_sources=("n8n", "retool"),
            protected_buyer_max_poll_cycles=1,
        )
    )
    app.adaptive.refresh([_roi_row("n8n", leads=1000)], [], cycle_index=0)
    assert app.adaptive.source_plan("n8n").recommendation is OptimizationRecommendation.DEPRIORITIZE
    assert app.adaptive.source_plan("n8n").interval_cycles == 3

    app._cycle_index = 1
    assert app._effective_poll_interval("n8n") == 1
    assert app._should_run_source("n8n") is True
    assert app._effective_poll_interval("github") == 1  # no history -> neutral


def test_phase9_collectors_are_wired_into_app(tmp_path: Path) -> None:
    app = RadarApp(
        settings(
            tmp_path,
            retool_enabled=True,
            xano_enabled=True,
            flutterflow_enabled=True,
        )
    )
    collectors = list(app.collectors())
    assert any(isinstance(item, RetoolCommunityCollector) for item in collectors)
    assert any(isinstance(item, XanoWebCollector) for item in collectors)
    assert any(isinstance(item, FlutterFlowWebCollector) for item in collectors)


def test_linkedin_manual_buyer_can_enter_commercial_lane(tmp_path: Path) -> None:
    opportunity = build_linkedin_opportunity(
        url="https://www.linkedin.com/posts/example-activity-123?utm_source=share",
        author="Founder",
        query_key="looking_python_dm",
        body=(
            "Looking for a Python developer to help us fix a production API integration and broken webhook. "
            "This is a paid short project. DM me if you can start this week."
        ),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    assert opportunity.url == "https://www.linkedin.com/posts/example-activity-123"
    assert opportunity.metadata["platform"] == "manual_social"
    assert opportunity.metadata["query_key"] == "looking_python_dm"

    analysis = MATCHER.analyze(opportunity)
    assessment = assess_commercial(opportunity, analysis)
    assert assessment.author_type in {
        AuthorType.BUYER,
        AuthorType.TECHNICAL_BUYER,
        AuthorType.POTENTIAL_BUYER,
    }
    assert assessment.alert_class in {
        AlertClass.P0_CASH,
        AlertClass.P0_HIGH_VALUE,
        AlertClass.P0_RELATIONSHIP,
        AlertClass.P1,
    }

    app = RadarApp(settings(tmp_path))
    result = app.ingest_manual(opportunity)
    assert result.status == "stored"
    assert result.opportunity_id is not None
    assert Database(tmp_path / "radar.db").exists("linkedin", opportunity.external_id)



def test_linkedin_short_direct_buyer_is_kept_as_p1_or_better() -> None:
    opportunity = build_linkedin_opportunity(
        url="https://www.linkedin.com/posts/example-short-buyer-789",
        body=(
            "Looking for a Python developer to fix our API webhook. "
            "Paid short project. DM me if you can start today."
        ),
    )
    assessment = assess_commercial(opportunity, MATCHER.analyze(opportunity))
    assert assessment.features["direct_buyer_evidence"] is True
    assert assessment.features["manual_direct_buyer"] is True
    assert assessment.alert_class in {
        AlertClass.P0_CASH,
        AlertClass.P0_HIGH_VALUE,
        AlertClass.P0_RELATIONSHIP,
        AlertClass.P1,
    }


def test_linkedin_manual_seller_is_rejected() -> None:
    opportunity = build_linkedin_opportunity(
        url="https://linkedin.com/posts/example-seller-456",
        body=(
            "I am available today for API automation work. I can build or fix webhooks. "
            "My rate is $25/hour. Price: fixed options available. Deliverables include code and documentation."
        ),
    )
    assessment = assess_commercial(opportunity, MATCHER.analyze(opportunity))
    assert assessment.author_type == AuthorType.SERVICE_PROVIDER
    assert assessment.alert_class == AlertClass.REJECT


def test_new_marketplace_section_patterns_match_expected_urls(tmp_path: Path) -> None:
    xano = XanoWebCollector(Database(tmp_path / "xano.db"))
    xano_section = xano.sections[0]
    xano_html = (
        '<a href="/find-a-xano-expert/post/looking-for-xano-developer-abc123">buyer</a>'
        '<a href="/other/post/not-this">other</a>'
    )
    xano_links = xano._topic_links(xano_html, xano_section, xano_section.index_url)
    assert xano_links == [
        "https://community.xano.com/find-a-xano-expert/post/looking-for-xano-developer-abc123"
    ]

    flutter = FlutterFlowWebCollector(Database(tmp_path / "flutter.db"))
    flutter_section = flutter.sections[0]
    flutter_html = '<a href="/c/job-postings/post/revenuecat-debug-abc123">job</a>'
    flutter_links = flutter._topic_links(flutter_html, flutter_section, flutter_section.index_url)
    assert flutter_links == [
        "https://community.flutterflow.io/c/job-postings/post/revenuecat-debug-abc123"
    ]
