from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lead_radar.collectors.discourse import MakeCommunityCollector, N8nCommunityCollector
from lead_radar.commercial import assess_commercial
from lead_radar.config import load_keyword_config
from lead_radar.database import Database
from lead_radar.matcher import Matcher
from lead_radar.models import AlertClass, AuthorType, CommercialIntent, Opportunity


NOW = datetime.now(timezone.utc)


class FakeResponse:
    def __init__(self, payload, *, status_code=200, headers=None, url="https://example.com"):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.request = type("Request", (), {"url": url})()
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._payload


class FakeMakeClient:
    calls: list[tuple[str, dict | None]] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, **kwargs):
        self.__class__.calls.append((url, params))
        recent = (NOW - timedelta(minutes=8)).isoformat().replace("+00:00", "Z")
        if "hire-help/74.json" in url:
            return FakeResponse(
                {
                    "topic_list": {
                        "topics": [
                            {
                                "id": 9001,
                                "slug": "white-label-automation-partner",
                                "title": "Looking for a white-label automation developer",
                                "created_at": recent,
                                "last_posted_at": recent,
                                "highest_post_number": 2,
                                "reply_count": 1,
                                "views": 20,
                            }
                        ]
                    }
                },
                url=url,
            )
        if "questions/66.json" in url:
            return FakeResponse({"topic_list": {"topics": []}}, url=url)
        if "/t/9001.json" in url:
            return FakeResponse(
                {
                    "title": "Looking for a white-label automation developer",
                    "slug": "white-label-automation-partner",
                    "details": {"created_by": {"username": "agency_owner"}},
                    "post_stream": {
                        "posts": [
                            {
                                "id": 501,
                                "post_number": 1,
                                "username": "agency_owner",
                                "created_at": recent,
                                "cooked": (
                                    "<p>We run an automation agency and need extra capacity for client projects. "
                                    "Looking for a contractor for Python API and webhook work. Paid test first.</p>"
                                ),
                            },
                            {
                                "id": 502,
                                "post_number": 2,
                                "username": "agency_owner",
                                "created_at": recent,
                                "cooked": "<p>Still looking. Budget is available, DM me.</p>",
                            },
                        ]
                    },
                },
                url=url,
            )
        raise AssertionError(f"Unexpected URL: {url}")


class FakeN8nClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, params=None, **kwargs):
        recent = (NOW - timedelta(minutes=12)).isoformat().replace("+00:00", "Z")
        if "c/jobs/13.json" in url:
            return FakeResponse(
                {
                    "topic_list": {
                        "topics": [
                            {
                                "id": 9101,
                                "slug": "for-hire-n8n-builder",
                                "title": "[For Hire] n8n automation builder",
                                "created_at": recent,
                                "last_posted_at": recent,
                                "highest_post_number": 1,
                                "reply_count": 0,
                                "views": 5,
                            }
                        ]
                    }
                },
                url=url,
            )
        if "help-me-build-my-workflow/36.json" in url:
            return FakeResponse({"topic_list": {"topics": []}}, url=url)
        if "/t/9101.json" in url:
            return FakeResponse(
                {
                    "title": "[For Hire] n8n automation builder",
                    "slug": "for-hire-n8n-builder",
                    "details": {"created_by": {"username": "seller"}},
                    "post_stream": {
                        "posts": [
                            {
                                "id": 601,
                                "post_number": 1,
                                "username": "seller",
                                "created_at": recent,
                                "cooked": "<p>I am available for freelance work building n8n and API automations.</p>",
                            }
                        ]
                    },
                },
                url=url,
            )
        raise AssertionError(f"Unexpected URL: {url}")


@pytest.mark.asyncio
async def test_make_collector_emits_topic_and_fresh_author_reply(tmp_path, monkeypatch):
    FakeMakeClient.calls = []
    monkeypatch.setattr("lead_radar.collectors.discourse.httpx.AsyncClient", FakeMakeClient)
    db = Database(tmp_path / "make.db")
    collector = MakeCommunityCollector(db, max_topics_per_category=10)

    items = await collector.collect()

    assert len(items) == 2
    assert items[0].source == "make"
    assert items[0].metadata["event_type"] == "topic"
    assert items[1].metadata["event_type"] == "reply"
    assert items[1].metadata["is_topic_author"] is True
    assert "Still looking" in items[1].body

    # No new category activity means no duplicate events on the next cycle.
    again = await collector.collect()
    assert again == []


@pytest.mark.asyncio
async def test_n8n_collector_keeps_for_hire_post_for_deterministic_rejection(tmp_path, monkeypatch):
    monkeypatch.setattr("lead_radar.collectors.discourse.httpx.AsyncClient", FakeN8nClient)
    db = Database(tmp_path / "n8n.db")
    collector = N8nCommunityCollector(db, max_topics_per_category=10)

    items = await collector.collect()

    assert len(items) == 1
    assert items[0].source == "n8n"
    assert items[0].metadata["category_key"] == "jobs"
    assert items[0].metadata["category_kind"] == "marketplace"


def _matcher() -> Matcher:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return Matcher(load_keyword_config(root / "config" / "keywords.yaml"))


def _assess(opportunity: Opportunity):
    matcher = _matcher()
    return assess_commercial(opportunity, matcher.analyze(opportunity), now=NOW)


def test_make_hire_help_category_is_a_real_buyer_signal():
    opportunity = Opportunity(
        source="make",
        external_id="topic:1:post:1",
        url="https://community.make.com/t/x/1/1",
        title="Looking for White-Label Make.com Automation Developers (Agency Partner)",
        body=(
            "We run an automation agency with several client projects and need extra capacity. "
            "Looking for a contractor for API integrations and webhook work. Paid test first."
        ),
        author="owner",
        created_at=NOW - timedelta(minutes=5),
        metadata={
            "category_key": "hire_help",
            "category_kind": "marketplace",
            "event_type": "topic",
            "is_topic_author": True,
            "reply_count": 0,
        },
    )
    assessment = _assess(opportunity)
    assert assessment.author_type == AuthorType.AGENCY_BUYER
    assert assessment.commercial_intent in {CommercialIntent.IMPLICIT, CommercialIntent.EXPLICIT}
    assert assessment.alert_class in {
        AlertClass.P0_RELATIONSHIP,
        AlertClass.P0_CASH,
        AlertClass.P0_HIGH_VALUE,
    }


def test_fresh_topic_author_reply_reactivates_old_thread():
    opportunity = Opportunity(
        source="n8n",
        external_id="topic:2:post:99",
        url="https://community.n8n.io/t/x/2/99",
        title="Reply: Need help finishing a client workflow",
        body="Still looking. Our contractor disappeared, budget is $300 and the client deadline is tomorrow. DM me.",
        author="original_buyer",
        created_at=NOW - timedelta(minutes=3),
        metadata={
            "category_key": "jobs",
            "category_kind": "marketplace",
            "event_type": "reply",
            "topic_author": "original_buyer",
            "is_topic_author": True,
            "reply_count": 8,
            "topic_created_at": (NOW - timedelta(days=8)).isoformat(),
        },
    )
    assessment = _assess(opportunity)
    assert assessment.author_type in {AuthorType.BUYER, AuthorType.AGENCY_BUYER}
    assert assessment.commercial_intent == CommercialIntent.EXPLICIT
    assert assessment.features["fresh_buyer_update"] is True
    assert assessment.p0


def test_n8n_for_hire_seller_is_rejected():
    opportunity = Opportunity(
        source="n8n",
        external_id="topic:3:post:1",
        url="https://community.n8n.io/t/x/3/1",
        title="[For Hire] n8n, Make.com, API integrations",
        body="I am available for freelance work. I build automations for clients and you can book a call.",
        author="seller",
        created_at=NOW - timedelta(minutes=4),
        metadata={
            "category_key": "jobs",
            "category_kind": "marketplace",
            "event_type": "topic",
            "is_topic_author": True,
            "reply_count": 0,
        },
    )
    assessment = _assess(opportunity)
    assert assessment.author_type in {AuthorType.JOB_SEEKER, AuthorType.SERVICE_PROVIDER}
    assert assessment.commercial_intent == CommercialIntent.NONE
    assert assessment.alert_class == AlertClass.REJECT


def test_generic_full_time_community_role_is_recruiter_not_cash_lead():
    opportunity = Opportunity(
        source="n8n",
        external_id="topic:4:post:1",
        url="https://community.n8n.io/t/x/4/1",
        title="Hiring Senior Automation Engineer - Full-Time",
        body="Full-time employment with annual salary and benefits. Submit your resume and apply here.",
        author="recruiter",
        created_at=NOW - timedelta(minutes=4),
        metadata={
            "category_key": "jobs",
            "category_kind": "marketplace",
            "event_type": "topic",
            "is_topic_author": True,
            "reply_count": 0,
        },
    )
    assessment = _assess(opportunity)
    assert assessment.author_type == AuthorType.RECRUITER
    assert assessment.alert_class == AlertClass.REJECT


def test_non_topic_author_marketplace_reply_is_not_a_buyer_alert():
    opportunity = Opportunity(
        source="make",
        external_id="topic:5:post:2",
        url="https://community.make.com/t/x/5/2",
        title="Reply: Looking for White-Label Make.com Automation Developers (Agency Partner)",
        body=(
            "Hi Barry, I can take a scoped Make project from brief to tested white-label handoff. "
            "I run a small automation team and have capacity for additional client projects."
        ),
        author="applicant",
        created_at=NOW - timedelta(minutes=2),
        metadata={
            "category_key": "hire_help",
            "category_kind": "marketplace",
            "event_type": "reply",
            "topic_author": "barry",
            "is_topic_author": False,
            "reply_count": 4,
        },
    )
    assessment = _assess(opportunity)
    assert assessment.author_type == AuthorType.SERVICE_PROVIDER
    assert assessment.commercial_intent == CommercialIntent.NONE
    assert assessment.alert_class == AlertClass.REJECT
    assert assessment.features["community_non_author_reply"] is True


def test_non_author_reply_cannot_inherit_buyer_intent_from_title():
    opportunity = Opportunity(
        source="n8n",
        external_id="topic:6:post:20",
        url="https://community.n8n.io/t/x/6/20",
        title="Reply: Looking for an n8n Freelancer / Automation Builder",
        body="I can help with this. I build n8n, API and webhook automations for clients.",
        author="seller",
        created_at=NOW - timedelta(minutes=1),
        metadata={
            "category_key": "jobs",
            "category_kind": "marketplace",
            "event_type": "reply",
            "topic_author": "buyer",
            "is_topic_author": False,
            "reply_count": 20,
        },
    )
    assessment = _assess(opportunity)
    assert not assessment.p0
    assert assessment.alert_class == AlertClass.REJECT


@pytest.mark.asyncio
async def test_saved_topic_author_is_used_for_later_last_json_reply(tmp_path, monkeypatch):
    db = Database(tmp_path / "author-state.db")
    db.set_state("discourse:make:hire_help:topic:9200:last_post", "1")
    db.set_state("discourse:make:hire_help:topic:9200:activity", "old")
    db.set_state("discourse:make:hire_help:topic:9200:author", "buyer_owner")

    recent = (NOW - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")

    class LaterReplyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, params=None, **kwargs):
            if "hire-help/74.json" in url:
                return FakeResponse({"topic_list": {"topics": [{
                    "id": 9200,
                    "slug": "need-api-help",
                    "title": "Need API help",
                    "created_at": recent,
                    "last_posted_at": recent,
                    "highest_post_number": 2,
                    "reply_count": 1,
                }]}}, url=url)
            if "questions/66.json" in url:
                return FakeResponse({"topic_list": {"topics": []}}, url=url)
            if "/last.json" in url:
                return FakeResponse({
                    "title": "Need API help",
                    "slug": "need-api-help",
                    "post_stream": {"posts": [{
                        "id": 702,
                        "post_number": 2,
                        "username": "buyer_owner",
                        "created_at": recent,
                        "cooked": "<p>Still looking. Budget is $250. DM me.</p>",
                    }]},
                }, url=url)
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("lead_radar.collectors.discourse.httpx.AsyncClient", LaterReplyClient)
    collector = MakeCommunityCollector(db, max_topics_per_category=10)
    items = await collector.collect()

    assert len(items) == 1
    assert items[0].metadata["topic_author"] == "buyer_owner"
    assert items[0].metadata["is_topic_author"] is True
    assessment = _assess(items[0])
    assert assessment.commercial_intent == CommercialIntent.EXPLICIT
    assert assessment.features["fresh_buyer_update"] is True
