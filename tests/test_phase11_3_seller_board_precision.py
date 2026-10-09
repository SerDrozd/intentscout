"""Existing IGNORE plans and self-offering collaborators must not become buyers."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lead_radar.app import RadarApp
from lead_radar.commercial import assess_commercial, has_direct_buyer_evidence
from lead_radar.config import Settings
from lead_radar.models import ContactStrategy, LeadBrief, Opportunity, ScoredOpportunity
from lead_radar.scoring import score

ROOT = Path(__file__).resolve().parents[1]


def _app(tmp_path: Path) -> RadarApp:
    return RadarApp(Settings(
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
    ))


def _lead(key: str, title: str, body: str, *, source: str = 'n8n') -> Opportunity:
    return Opportunity(
        source=source, external_id=key,
        url=f'https://community.n8n.io/t/{key}/12345/1',
        title=title, body=body, author='someone', created_at=datetime.now(timezone.utc),
        metadata={'platform': 'discourse', 'category_kind': 'marketplace',
                  'event_type': 'topic', 'is_topic_author': True, 'reply_count': 0},
    )


def _save(app: RadarApp, lead: Opportunity) -> int:
    analysis = app.matcher.analyze(lead)
    total, breakdown = score(lead, analysis)
    saved = app.db.insert(ScoredOpportunity(
        lead, analysis, total, breakdown, assess_commercial(lead, analysis)))
    assert saved is not None
    return saved


def test_collaborator_post_not_direct_buyer_and_not_on_board_even_if_legacy_row(tmp_path: Path) -> None:
    app = _app(tmp_path)
    candidate = _lead(
        'collab', '[Open to Collabs] Want to build my n8n skills and assist on projects',
        "I'm running n8n locally to learn the ropes. I'm not looking for paid gigs yet; "
        "I want hands-on real-world experience. I can handle webhooks and help with API credentials. "
        "DM me if you'd like to collaborate on projects."
    )
    analysis = app.matcher.analyze(candidate)
    assert not has_direct_buyer_evidence(candidate, analysis)
    saved = _save(app, candidate)
    # Simulate pre-fix stored metadata, which must not be trusted in a fresh board.
    with app.db.connect() as conn:
        conn.execute("""UPDATE opportunities SET author_type='TECHNICAL_BUYER',
                      alert_class='P0_HIGH_VALUE', cash_score=70, llm_decision='NOT_RUN'
                      WHERE id=?""", (saved,))
    assert not any(int(row['id']) == saved for _, row in app.buyer_board(days=3))


def test_prior_ignore_brief_is_hidden_from_both_queues_without_changing_feedback(tmp_path: Path) -> None:
    app = _app(tmp_path)
    candidate = _lead(
        'paid-hire', 'Hiring Python developer for paid API webhook integrations',
        'We need a Python developer to fix our webhook API integration. Paid $300. DM us.',
        source='make',
    )
    saved = _save(app, candidate)
    assert saved in {int(row['id']) for row in app.db.shortlist()}
    assert saved in {int(row['id']) for _, row in app.buyer_board(days=3)}
    app.db.save_lead_brief(LeadBrief(
        opportunity_id=saved, problem_summary='API integration',
        why_fit='Client requires a credential unavailable to contractor',
        money_signal='Paid task', suggested_scope='Single webhook',
        suggested_price_band='Not quoted', contact_strategy=ContactStrategy.IGNORE,
        draft='', confidence=95,
    ), model='test')
    assert saved not in {int(row['id']) for row in app.db.shortlist()}
    assert saved not in {int(row['id']) for _, row in app.buyer_board(days=3)}
    assert saved not in {int(row['id']) for row in app.db.llm_candidate_rows()}
    row = app.db.opportunity_row(saved)
    assert row is not None
    assert row['status'] == 'new'  # No misleading synthetic user feedback.


def test_legitimate_collaboration_hire_remains_direct(tmp_path: Path) -> None:
    app = _app(tmp_path)
    buyer = _lead(
        'hiring', 'Hiring Python automation contractor to collaborate on a paid project',
        'We need a developer to implement our API integration. We can pay $300 for this task.'
    )
    assert has_direct_buyer_evidence(buyer, app.matcher.analyze(buyer))
