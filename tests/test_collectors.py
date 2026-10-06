import pytest

from lead_radar.collectors.github import GitHubCollector
from lead_radar.collectors.hackernews import HackerNewsCollector
from lead_radar.database import Database


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHNClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        if url.endswith('/maxitem.json'):
            return FakeResponse(100)
        if url.endswith('/item/99.json'):
            return FakeResponse({
                'id': 99,
                'type': 'comment',
                'by': 'founder42',
                'time': 1760000000,
                'text': 'Our <b>webhook stopped working</b> in production',
                'parent': 10,
            })
        if url.endswith('/item/100.json'):
            return FakeResponse({
                'id': 100,
                'type': 'story',
                'by': 'dev',
                'time': 1760000010,
                'title': 'Ask HN: Need help with API',
                'text': 'Customer integration broken',
            })
        return FakeResponse(None)


class FakeGitHubClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, **kwargs):
        return FakeResponse({
            'items': [{
                'id': 555,
                'number': 7,
                'html_url': 'https://github.com/acme/widget/issues/7',
                'title': 'Webhook not working in production',
                'body': 'Need help with API integration for our customer.',
                'created_at': '2026-10-02T10:00:00Z',
                'repository_url': 'https://api.github.com/repos/acme/widget',
                'comments': 2,
                'labels': [{'name': 'bug'}],
                'user': {'login': 'acme-founder'},
            }]
        })


@pytest.mark.asyncio
async def test_hn_collector_parses_comments_and_stories(tmp_path, monkeypatch):
    monkeypatch.setattr('lead_radar.collectors.hackernews.httpx.AsyncClient', FakeHNClient)
    db = Database(tmp_path / 'hn.db')
    collector = HackerNewsCollector(db, bootstrap_items=2, max_items_per_cycle=2)
    items = await collector.collect()
    assert len(items) == 2
    assert any(i.external_id == '99' and 'webhook stopped working' in i.body for i in items)
    assert any(i.external_id == '100' and i.title.startswith('Ask HN') for i in items)
    assert db.get_state('hn_last_item') == '100'


@pytest.mark.asyncio
async def test_github_collector_parses_and_dedupes_broad_queries(monkeypatch):
    monkeypatch.setattr('lead_radar.collectors.github.httpx.AsyncClient', FakeGitHubClient)
    collector = GitHubCollector(token='x', lookback_hours=24, results_per_query=10)
    items = await collector.collect()
    assert len(items) == 1
    item = items[0]
    assert item.external_id == '555'
    assert item.metadata['repository'] == 'acme/widget'
    assert item.author == 'acme-founder'
