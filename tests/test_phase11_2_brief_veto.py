"""A final action brief veto must not turn into a Telegram P0 alert."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lead_radar.app import CycleStats, RadarApp
from lead_radar.commercial import assess_commercial
from lead_radar.config import Settings
from lead_radar.llm import LLMBudgetExceeded
from lead_radar.models import ContactStrategy, LeadBrief, Opportunity, ScoredOpportunity
from lead_radar.scoring import score

ROOT = Path(__file__).resolve().parents[1]


def _app(tmp_path: Path) -> RadarApp:
    return RadarApp(Settings(
        db_path=tmp_path / "radar.db", keywords_path=ROOT / "config/keywords.yaml",
        poll_interval_seconds=180, log_level="INFO", alert_threshold=13,
        review_threshold=8, alert_review_queue=False,
        cash_alert_threshold=72, value_alert_threshold=74, relationship_alert_threshold=76,
        max_alerts_per_cycle=5, telegram_min_send_interval_seconds=1.15,
        hn_enabled=False, hn_bootstrap_items=10, hn_max_items_per_cycle=10,
        hn_concurrency=2, github_enabled=False, github_token=None,
        github_lookback_hours=24, github_results_per_query=10,
        make_enabled=False, make_lookback_hours=168, make_topics_per_category=30,
        n8n_enabled=False, n8n_lookback_hours=168, n8n_topics_per_category=30,
        discourse_concurrency=2, telegram_bot_token=None, telegram_chat_id=None,
        llm_enabled=False, openai_api_key=None,
        hn_algolia_enabled=False, bluesky_enabled=False, rss_enabled=False,
        shopify_enabled=False, bubble_enabled=False, airtable_enabled=False,
        zapier_enabled=False, retool_enabled=False, xano_enabled=False,
        flutterflow_enabled=False,
    ))


def _candidate(app: RadarApp, title: str) -> tuple[int, ScoredOpportunity]:
    opportunity = Opportunity(
        source="make", external_id=title, url="https://community.make.com/t/fake/123/1",
        title=title,
        body="Hiring for a paid API webhook automation integration project; please apply.",
        author="buyer", created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        metadata={"category_kind": "marketplace", "event_type": "topic", "is_topic_author": True},
    )
    analysis = app.matcher.analyze(opportunity)
    total, breakdown = score(opportunity, analysis)
    scored = ScoredOpportunity(opportunity, analysis, total, breakdown, assess_commercial(opportunity, analysis))
    saved = app.db.insert(scored)
    assert saved is not None
    return saved, scored


def _brief(saved: int, strategy: ContactStrategy) -> LeadBrief:
    return LeadBrief(
        opportunity_id=saved, problem_summary="WordPress API integrations",
        why_fit="Technical fit, but replies must be in Czech and contractor must live in CZ.",
        money_signal="Budget not stated", suggested_scope="One workflow",
        suggested_price_band="Unconfirmed", contact_strategy=strategy,
        draft="" if strategy == ContactStrategy.IGNORE else "Can you share the webhook payload?",
        confidence=93,
    )


class FakeTelegram:
    enabled = True

    def __init__(self, app: RadarApp) -> None:
        self.app = app
        self.sent: list[int] = []

    async def send_alert(self, opportunity_id: int, scored: ScoredOpportunity, brief=None) -> bool:
        self.sent.append(opportunity_id)
        self.app.db.mark_notified(opportunity_id)
        return True


@pytest.mark.asyncio
async def test_generated_ignore_brief_blocks_alert_and_replay(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    saved, scored = _candidate(app, "Hiring Czech Specialist based in CZ: replies in Czech only")
    fake = FakeTelegram(app)
    app.telegram = fake  # type: ignore[assignment]

    async def ignore_brief(opportunity_id: int, _: ScoredOpportunity) -> LeadBrief:
        assert opportunity_id == saved
        return _brief(saved, ContactStrategy.IGNORE)

    monkeypatch.setattr(app, "_brief_for_scored", ignore_brief)
    stats = CycleStats()
    await app._send_pending_alerts([(89, saved, scored)], stats)

    assert fake.sent == []
    assert stats.alerted == 0
    assert stats.brief_vetoed == 1
    row = app.db.opportunity_row(saved)
    assert row is not None and row["status"] == "screened_out"
    with app.db.connect() as conn:
        notified = conn.execute("SELECT notified_at FROM opportunities WHERE id=?", (saved,)).fetchone()
    assert notified is not None and notified["notified_at"] is None
    assert app.db.pending_alert_rows() == []
    assert app.db.roi_report(days=1)[0]["ignored"] == 0
    restored: list[tuple[int, int, ScoredOpportunity]] = []
    app._restore_pending_alerts(restored)
    assert restored == []
    with app.db.connect() as conn:
        actions = conn.execute("SELECT action, reason FROM actions WHERE opportunity_id=?", (saved,)).fetchall()
    assert [(r["action"], r["reason"]) for r in actions] == [
        ("screened_out", "auto_brief_contact_strategy_ignore")
    ]


@pytest.mark.asyncio
async def test_cached_ignore_brief_blocks_alert_without_generation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    saved, scored = _candidate(app, "Hiring Czech Specialist based in CZ")
    app.db.save_lead_brief(_brief(saved, ContactStrategy.IGNORE), model="test")
    fake = FakeTelegram(app)
    app.telegram = fake  # type: ignore[assignment]

    stats = CycleStats()
    await app._send_pending_alerts([(89, saved, scored)], stats)
    assert stats.brief_vetoed == 1
    assert fake.sent == []
    assert app.db.opportunity_row(saved)["status"] == "screened_out"  # type: ignore[index]


@pytest.mark.asyncio
async def test_actionable_brief_still_sends_normal_alert(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    saved, scored = _candidate(app, "Hiring remote Python API automation developer")
    fake = FakeTelegram(app)
    app.telegram = fake  # type: ignore[assignment]

    async def actionable_brief(opportunity_id: int, _: ScoredOpportunity) -> LeadBrief:
        return _brief(opportunity_id, ContactStrategy.DM)

    monkeypatch.setattr(app, "_brief_for_scored", actionable_brief)
    stats = CycleStats()
    await app._send_pending_alerts([(89, saved, scored)], stats)
    assert fake.sent == [saved]
    assert stats.alerted == 1
    assert stats.brief_vetoed == 0


@pytest.mark.asyncio
async def test_brief_unavailable_does_not_auto_ignore_candidate(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    saved, scored = _candidate(app, "Hiring remote Python API automation developer")
    fake = FakeTelegram(app)
    app.telegram = fake  # type: ignore[assignment]

    async def budget_exhausted(opportunity_id: int, _: ScoredOpportunity) -> None:
        raise LLMBudgetExceeded("No budget")

    monkeypatch.setattr(app, "_brief_for_scored", budget_exhausted)
    stats = CycleStats()
    await app._send_pending_alerts([(89, saved, scored)], stats)
    assert fake.sent == [saved]
    assert stats.brief_vetoed == 0
