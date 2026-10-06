from __future__ import annotations

from lead_radar.adaptive import AdaptiveScheduler
from lead_radar.collectors.bluesky import BlueskyCollector
from lead_radar.collectors.github import BROAD_QUERIES, GitHubCollector
from lead_radar.collectors.hn_algolia import HackerNewsAlgoliaCollector
from lead_radar.config import load_settings
from lead_radar.database import Database
from lead_radar.optimizer import OptimizationRecommendation


def _row(
    source: str,
    *,
    key: str = "all",
    leads: int,
    p0: int = 0,
    p1: int = 0,
    contacted: int = 0,
    replied: int = 0,
    proposal: int = 0,
    won: int = 0,
    revenue: float = 0.0,
) -> dict[str, object]:
    return {
        "source": source,
        "key": key,
        "leads": leads,
        "p0": p0,
        "p1": p1,
        "contacted": contacted,
        "replied": replied,
        "proposal": proposal,
        "won": won,
        "lost": 0,
        "ignored": 0,
        "revenue": revenue,
        "llm_cost": 0.0,
    }


def test_disable_candidate_is_sampled_not_disabled() -> None:
    scheduler = AdaptiveScheduler()
    scheduler.refresh([_row("github", leads=2743)], [], cycle_index=0)

    plan = scheduler.source_plan("github")
    assert plan.recommendation is OptimizationRecommendation.DISABLE_CANDIDATE
    assert plan.interval_cycles == 8
    assert scheduler.should_run("github", 0) is True
    assert scheduler.should_run("github", 1) is False
    assert scheduler.should_run("github", 8) is True
    assert scheduler.query_limit("github", 8, 8) == 2


def test_insufficient_data_keeps_full_exploration_rate() -> None:
    scheduler = AdaptiveScheduler()
    scheduler.refresh([_row("airtable", leads=1)], [], cycle_index=0)

    plan = scheduler.source_plan("airtable")
    assert plan.recommendation is OptimizationRecommendation.INSUFFICIENT_DATA
    assert plan.interval_cycles == 1
    assert scheduler.should_run("airtable", 5) is True


def test_keep_hackernews_preserves_normal_query_budget() -> None:
    scheduler = AdaptiveScheduler()
    scheduler.refresh([_row("hackernews", leads=185, p0=1, p1=12)], [], cycle_index=0)

    plan = scheduler.source_plan("hackernews")
    assert plan.recommendation is OptimizationRecommendation.KEEP
    assert scheduler.query_limit("hackernews", 6, 12) == 6


def test_weighted_query_rotation_preserves_exploration() -> None:
    scheduler = AdaptiveScheduler()
    source_rows = [_row("github", leads=500)]
    query_rows = [
        _row("github", key="automation", leads=701),
        _row("github", key="oauth", leads=490),
        _row("github", key="webhook", leads=486),
        _row("github", key='"API integration"', leads=322),
        _row("github", key="fastapi", leads=189),
    ]
    scheduler.refresh(source_rows, query_rows, cycle_index=0)
    available = tuple(BROAD_QUERIES)

    observed: set[str] = set()
    for cycle in range(24):
        observed.update(
            scheduler.select_query_keys(
                "github", available, base_limit=len(available), cycle_index=cycle
            )
        )
    assert observed == set(available)


def test_disabled_adaptive_mode_keeps_fixed_behavior() -> None:
    scheduler = AdaptiveScheduler(enabled=False)
    scheduler.refresh([_row("github", leads=3000)], [], cycle_index=0)
    assert scheduler.should_run("github", 3) is True
    assert scheduler.query_limit("github", 8, 8) == 8
    assert scheduler.select_query_keys("github", BROAD_QUERIES, base_limit=8, cycle_index=3) == tuple(BROAD_QUERIES)


def test_collectors_accept_explicit_adaptive_query_subsets(tmp_path) -> None:
    db = Database(tmp_path / "adaptive.db")
    hn = HackerNewsAlgoliaCollector(db, query_keys=("pay_help", "automation_help"))
    bluesky = BlueskyCollector(db, query_keys=("need_automation", "paid_project"))
    github = GitHubCollector(None, queries=("webhook", "automation"))

    assert {item.key for item in hn._selected_queries()} == {"pay_help", "automation_help"}
    assert {item.key for item in bluesky._selected_queries()} == {"need_automation", "paid_project"}
    assert github.queries == ("webhook", "automation")


def test_phase8_settings_default_to_safe_adaptive_mode(tmp_path, monkeypatch) -> None:
    for name in (
        "ADAPTIVE_SCHEDULING_ENABLED",
        "ADAPTIVE_WINDOW_DAYS",
        "ADAPTIVE_REFRESH_CYCLES",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(tmp_path)
    assert settings.adaptive_scheduling_enabled is True
    assert settings.adaptive_window_days == 30
    assert settings.adaptive_refresh_cycles == 10
