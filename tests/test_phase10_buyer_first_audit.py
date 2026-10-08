from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from lead_radar.app import CycleStats, RadarApp
from lead_radar.collectors.discourse import DiscourseCategory, DiscourseCommunityCollector
from lead_radar.config import Settings
from lead_radar.models import Opportunity


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime.now(timezone.utc)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / 'radar.db', keywords_path=ROOT / 'config/keywords.yaml',
        poll_interval_seconds=180, log_level='INFO', alert_threshold=13,
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
    )


def _lead(source: str, key: str, title: str, body: str, kind: str = 'marketplace') -> Opportunity:
    return Opportunity(
        source=source, external_id=key, url=f'https://example.org/t/{key}',
        title=title, body=body, author='buyer',
        created_at=NOW - timedelta(minutes=3),
        metadata={'platform': 'discourse', 'category_kind': kind,
                  'event_type': 'topic', 'is_topic_author': True, 'reply_count': 0},
    )


@pytest.mark.asyncio
async def test_automatic_process_keeps_direct_buyer_without_phrase_match(tmp_path: Path) -> None:
    app = RadarApp(_settings(tmp_path))
    opportunity = _lead('make', 'no-phrase', 'Need contractor',
                        'I am hiring someone to debug our CRM. Fixed-budget small job.')
    analysis = app.matcher.analyze(opportunity)
    assert not analysis.phrase_matches, 'Regression fixture must bypass phrase matcher'
    pending: list = []
    stats = CycleStats()
    await app.process(opportunity, stats, pending)
    assert stats.matched == 1
    assert stats.inserted == 1
    assert stats.commercial_candidates == 1


@pytest.mark.asyncio
async def test_automatic_process_rejects_seller_without_phrase_match(tmp_path: Path) -> None:
    app = RadarApp(_settings(tmp_path))
    opportunity = _lead('make', 'seller', 'Open to work',
                        'I am available today. Price: $50 fixed. You pay after deliverables.')
    stats = CycleStats()
    await app.process(opportunity, stats, [])
    assert stats.inserted == 0 or app.db.opportunity_row(1)['alert_class'] == 'REJECT'
    assert stats.commercial_candidates == 0


def test_pending_alert_retry_recovers_undelivered_explicit_buyer(tmp_path: Path) -> None:
    app = RadarApp(_settings(tmp_path))
    opportunity = _lead('n8n', 'paid-hire', 'Hiring Python developer for webhook integration',
                        'We need a developer to fix our webhook API. Paid task $250, DM me.')
    from lead_radar.scoring import score
    from lead_radar.commercial import assess_commercial
    from lead_radar.models import ScoredOpportunity
    analysis = app.matcher.analyze(opportunity)
    total, breakdown = score(opportunity, analysis)
    assessment = assess_commercial(opportunity, analysis)
    assert assessment.p0 or app._review_alert_eligible(ScoredOpportunity(
        opportunity, analysis, total, breakdown, assessment))
    stored_id = app.db.insert(ScoredOpportunity(opportunity, analysis, total, breakdown, assessment))
    assert stored_id is not None
    pending: list = []
    app._restore_pending_alerts(pending)
    assert any(item[1] == stored_id for item in pending)
    app.db.mark_notified(stored_id)
    pending = []
    app._restore_pending_alerts(pending)
    assert pending == []


def test_retry_never_replays_rejected_or_contacted(tmp_path: Path) -> None:
    app = RadarApp(_settings(tmp_path))
    from lead_radar.scoring import score
    from lead_radar.commercial import assess_commercial
    from lead_radar.models import ScoredOpportunity
    opportunity = _lead('n8n', 'paid-hire', 'Hiring Python developer for webhook integration',
                        'We need a developer to fix our webhook API. Paid task $250, DM me.')
    analysis = app.matcher.analyze(opportunity)
    total, breakdown = score(opportunity, analysis)
    stored_id = app.db.insert(ScoredOpportunity(
        opportunity, analysis, total, breakdown, assess_commercial(opportunity, analysis)))
    assert stored_id is not None
    app.db.set_status(stored_id, 'contacted', reason='test')
    assert app.db.pending_alert_rows() == []


def test_buyer_board_is_human_review_only_and_excludes_contacted(tmp_path: Path) -> None:
    app = RadarApp(_settings(tmp_path))
    from lead_radar.scoring import score
    from lead_radar.commercial import assess_commercial
    from lead_radar.models import ScoredOpportunity
    opportunity = _lead('make', 'paid-hire', 'Hiring Python developer for webhook integration',
                        'We need a developer to fix our webhook API. Paid task $250, DM me.')
    analysis = app.matcher.analyze(opportunity)
    total, breakdown = score(opportunity, analysis)
    stored_id = app.db.insert(ScoredOpportunity(
        opportunity, analysis, total, breakdown, assess_commercial(opportunity, analysis)))
    assert stored_id is not None
    board = app.buyer_board(days=3)
    assert board and board[0][1]['id'] == stored_id
    assert 'payment unverified' in board[0][0]
    app.db.set_status(stored_id, 'contacted', reason='test')
    assert app.buyer_board(days=3) == []


@pytest.mark.asyncio
async def test_marketplace_discourse_ignores_competing_seller_replies(tmp_path: Path) -> None:
    from lead_radar.database import Database
    db = Database(tmp_path / 'db.sqlite3')
    category = DiscourseCategory('jobs', 'c/jobs/13.json', 'marketplace')
    collector = DiscourseCommunityCollector(
        name='n8n', base_url='https://example.org', db=db,
        categories=(category,), lookback_hours=48,
    )
    class FakeClient:
        pass
    async def get_json(_client, _url, *, params=None):
        return {
            'title': 'Paid n8n test', 'slug': 'paid-n8n',
            'details': {'created_by': {'username': 'owner'}},
            'post_stream': {'posts': [
                {'id': 701, 'post_number': 1, 'username': 'owner',
                 'created_at': NOW.isoformat(), 'cooked': '<p>Need paid QA for n8n agent.</p>'},
                {'id': 702, 'post_number': 2, 'username': 'other',
                 'created_at': NOW.isoformat(), 'cooked': '<p>I can help, hire me.</p>'},
                {'id': 703, 'post_number': 3, 'username': 'owner',
                 'created_at': NOW.isoformat(), 'cooked': '<p>Still looking. Budget available.</p>'},
            ]},
        }
    collector._get_json = get_json  # type: ignore[method-assign]
    topic = {'id': 700, 'created_at': NOW.isoformat(), 'last_posted_at': NOW.isoformat(),
             'highest_post_number': 3, 'reply_count': 2}
    items = await collector._fetch_topic_events(FakeClient(), category, topic, NOW)  # type: ignore[arg-type]
    assert [item.metadata['post_number'] for item in items] == [1, 3]
    assert db.get_state(collector._post_key(category, 700)) == '3'


@pytest.mark.asyncio
async def test_full_marketplace_category_checks_newly_created_topics(tmp_path: Path, monkeypatch) -> None:
    from lead_radar.database import Database
    db = Database(tmp_path / 'db.sqlite3')
    category = DiscourseCategory('jobs', 'c/jobs/13.json', 'marketplace')
    collector = DiscourseCommunityCollector(
        name='n8n', base_url='https://example.org', db=db,
        categories=(category,), lookback_hours=168, max_topics_per_category=2,
    )
    seen = []
    async def fake_get_json(client, url, *, params=None):
        if params is not None:
            assert params == {'order': 'created'}
            return {'topic_list': {'topics': [{'id': 33}, {'id': 22}]}}
        return {'topic_list': {'topics': [{'id': 11}, {'id': 22}]}}
    async def fake_fetch(client, category, topic, now):
        seen.append(topic['id'])
        return []
    monkeypatch.setattr(collector, '_get_json', fake_get_json)
    monkeypatch.setattr(collector, '_fetch_topic_events', fake_fetch)
    await collector.collect()
    assert seen == [33, 11]  # a fresh topic cannot be crowded out by bumped replies


def test_github_default_searches_are_buyer_first() -> None:
    from lead_radar.collectors.github import BROAD_QUERIES
    assert len(BROAD_QUERIES) == 8
    assert all(any(word in q for word in ('bounty', 'paid', 'contractor', 'freelancer', 'willing to pay')) for q in BROAD_QUERIES)
    assert 'webhook' not in BROAD_QUERIES


def test_marketplace_rescan_only_rewinds_whitelisted_cursors(tmp_path: Path) -> None:
    from lead_radar.database import Database
    db = Database(tmp_path / 'radar.db')
    db.set_state('discourse:make:hire_help:topic:42:last_post', '7')
    db.set_state('discourse:make:hire_help:topic:42:activity', 'time')
    db.set_state('discourse:make:hire_help:topic:42:author', 'buyer')
    db.set_state('discourse:n8n:jobs:topic:33:last_post', '5')
    db.set_state('discourse:make:questions:topic:1:last_post', '3')
    assert db.rewind_marketplace_cursors(('make',)) == {'make': 2}
    assert db.get_state('discourse:make:hire_help:topic:42:last_post') is None
    assert db.get_state('discourse:make:hire_help:topic:42:author') == 'buyer'
    assert db.get_state('discourse:make:questions:topic:1:last_post') == '3'
    assert db.get_state('discourse:n8n:jobs:topic:33:last_post') == '5'
    with pytest.raises(ValueError):
        db.rewind_marketplace_cursors(('github',))


@pytest.mark.asyncio
async def test_run_cycle_resends_persisted_confirmed_p0_once(tmp_path: Path) -> None:
    """A Telegram/API outage may not turn an already stored lead into a lost lead."""
    app = RadarApp(_settings(tmp_path))
    from lead_radar.scoring import score
    from lead_radar.commercial import assess_commercial
    from lead_radar.models import ScoredOpportunity

    opportunity = _lead(
        'n8n', 'p0-outage', 'Hiring Python developer for webhook integration',
        'We need a developer to fix our webhook API. Paid task $250, DM me.',
    )
    analysis = app.matcher.analyze(opportunity)
    total, breakdown = score(opportunity, analysis)
    assessment = assess_commercial(opportunity, analysis)
    assert assessment.p0
    opportunity_id = app.db.insert(ScoredOpportunity(opportunity, analysis, total, breakdown, assessment))
    assert opportunity_id is not None

    class FakeTelegram:
        enabled = True

        def __init__(self) -> None:
            self.sent: list[int] = []

        async def poll_actions(self) -> int:
            return 0

        async def send_alert(self, saved_id: int, scored: ScoredOpportunity, brief=None) -> bool:
            self.sent.append(saved_id)
            app.db.mark_notified(saved_id)
            return True

    fake = FakeTelegram()
    app.telegram = fake  # type: ignore[assignment]
    stats = await app.run_cycle()
    assert stats.alerted == 1
    assert fake.sent == [opportunity_id]
    again = await app.run_cycle()
    assert again.alerted == 0
    assert fake.sent == [opportunity_id]
