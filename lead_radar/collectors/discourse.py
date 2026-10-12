from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import httpx

from ..database import Database
from ..models import Opportunity
from ..text import strip_html


log = logging.getLogger("lead_radar.collectors.discourse")


@dataclass(frozen=True, slots=True)
class DiscourseCategory:
    key: str
    path: str
    kind: str


class DiscourseCommunityCollector:
    """Read public Discourse categories and emit fresh topics + fresh replies.

    The collector stores a lightweight per-topic cursor in SQLite. On first sight it
    only emits posts inside the configured lookback window. After that it emits only
    post numbers newer than the stored cursor, so a fresh buyer clarification on an
    older thread becomes a new opportunity event instead of a duplicate.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        db: Database,
        categories: tuple[DiscourseCategory, ...],
        lookback_hours: int = 48,
        max_topics_per_category: int = 20,
        concurrency: int = 6,
        request_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/") + "/"
        self.db = db
        self.categories = categories
        self.lookback_hours = max(1, lookback_hours)
        self.max_topics_per_category = max(1, min(50, max_topics_per_category))
        self.concurrency = max(1, min(12, concurrency))
        self.request_headers = dict(request_headers or {})

    @staticmethod
    def _parse_time(raw: Any) -> datetime | None:
        if not isinstance(raw, str) or not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _topic_activity_raw(topic: dict[str, Any]) -> str | None:
        for key in ("last_posted_at", "bumped_at", "created_at"):
            value = topic.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    async def _get_json(self, client: httpx.AsyncClient, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(3):
            response = await client.get(url, params=params)
            if response.status_code == 429:
                retry_raw = response.headers.get("Retry-After", "2")
                try:
                    retry_after = max(1.0, min(30.0, float(retry_raw)))
                except ValueError:
                    retry_after = 2.0
                await asyncio.sleep(retry_after)
                continue
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(1.0 + attempt)
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Expected JSON object from {url}")
            return payload
        raise httpx.HTTPStatusError(
            "Discourse request failed after retries",
            request=response.request,
            response=response,
        )

    def _activity_key(self, category: DiscourseCategory, topic_id: int) -> str:
        return f"discourse:{self.name}:{category.key}:topic:{topic_id}:activity"

    def _post_key(self, category: DiscourseCategory, topic_id: int) -> str:
        return f"discourse:{self.name}:{category.key}:topic:{topic_id}:last_post"

    def _author_key(self, category: DiscourseCategory, topic_id: int) -> str:
        return f"discourse:{self.name}:{category.key}:topic:{topic_id}:author"

    async def _fetch_topic_events(
        self,
        client: httpx.AsyncClient,
        category: DiscourseCategory,
        topic: dict[str, Any],
        now: datetime,
    ) -> list[Opportunity]:
        topic_id_raw = topic.get("id")
        if not isinstance(topic_id_raw, int):
            return []
        topic_id = topic_id_raw
        activity_raw = self._topic_activity_raw(topic)
        if activity_raw is None:
            return []

        previous_activity = self.db.get_state(self._activity_key(category, topic_id))
        previous_post_raw = self.db.get_state(self._post_key(category, topic_id))
        previous_post = int(previous_post_raw) if previous_post_raw and previous_post_raw.isdigit() else 0

        # A prior bootstrap may have recorded only the topic activity while the
        # topic itself was outside the then-current lookback window. If the
        # lookback is later widened, there is still no post cursor, so we must
        # re-evaluate the topic instead of permanently treating it as seen.
        if previous_post > 0 and previous_activity == activity_raw:
            return []

        activity_at = self._parse_time(activity_raw)
        cutoff = now - timedelta(hours=self.lookback_hours)

        # On bootstrap, ignore dormant topics. Once a topic has a cursor, any new
        # activity is interesting even when the original thread is old.
        if previous_post == 0 and activity_at is not None and activity_at < cutoff:
            self.db.set_state(self._activity_key(category, topic_id), activity_raw)
            return []

        highest_raw = topic.get("highest_post_number") or topic.get("posts_count") or 1
        try:
            highest = max(1, int(highest_raw))
        except (TypeError, ValueError):
            highest = 1

        topic_url = urljoin(self.base_url, f"t/{topic_id}.json")
        latest_url = urljoin(self.base_url, f"t/-/{topic_id}/last.json")

        if previous_post == 0:
            payload = await self._get_json(client, topic_url)
            posts = payload.get("post_stream", {}).get("posts", [])
            if not isinstance(posts, list):
                posts = []
            # Discourse returns only a chunk of posts from a topic. For a busy new
            # thread also fetch the official `last.json` view so fresh replies are
            # represented without downloading the whole history.
            if highest > 20:
                latest_payload = await self._get_json(client, latest_url)
                latest_posts = latest_payload.get("post_stream", {}).get("posts", [])
                if isinstance(latest_posts, list):
                    posts = posts + latest_posts
        else:
            payload = await self._get_json(client, latest_url)
            posts = payload.get("post_stream", {}).get("posts", [])
            if not isinstance(posts, list):
                posts = []

        unique_posts: dict[int, dict[str, Any]] = {}
        for post in posts:
            if not isinstance(post, dict):
                continue
            post_id = post.get("id")
            if isinstance(post_id, int):
                unique_posts[post_id] = post

        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        created_by = details.get("created_by") if isinstance(details, dict) else None
        topic_author = created_by.get("username") if isinstance(created_by, dict) else None
        if not topic_author:
            for post in unique_posts.values():
                if post.get("post_number") == 1 and isinstance(post.get("username"), str):
                    topic_author = post["username"]
                    break
        if not topic_author:
            topic_author = self.db.get_state(self._author_key(category, topic_id))
        elif isinstance(topic_author, str):
            self.db.set_state(self._author_key(category, topic_id), topic_author)

        title = strip_html(str(payload.get("title") or topic.get("title") or "Discourse topic"))
        slug = str(payload.get("slug") or topic.get("slug") or "topic")
        reply_count_raw = topic.get("reply_count")
        if reply_count_raw is None:
            reply_count_raw = max(0, highest - 1)
        try:
            reply_count = max(0, int(reply_count_raw))
        except (TypeError, ValueError):
            reply_count = max(0, highest - 1)

        out: list[Opportunity] = []
        max_seen_post = previous_post
        for post in sorted(unique_posts.values(), key=lambda item: int(item.get("post_number") or 0)):
            try:
                post_number = int(post.get("post_number") or 0)
            except (TypeError, ValueError):
                continue
            if post_number <= previous_post:
                continue

            created_at = self._parse_time(post.get("created_at"))
            if created_at is None:
                continue
            if previous_post == 0 and created_at < cutoff:
                max_seen_post = max(max_seen_post, post_number)
                continue

            body = strip_html(post.get("cooked") or post.get("raw") or "")
            if not body:
                max_seen_post = max(max_seen_post, post_number)
                continue

            username = post.get("username") if isinstance(post.get("username"), str) else None
            event_type = "topic" if post_number == 1 else "reply"
            # In hire/jobs categories the replies are overwhelmingly competing
            # freelancers, not new job posts. Preserve the original buyer's
            # clarifications, but advance the cursor over third-party replies.
            if category.kind == "marketplace" and post_number > 1 and not (
                topic_author and username == topic_author
            ):
                max_seen_post = max(max_seen_post, post_number)
                continue
            direct_url = f"{self.base_url}t/{slug}/{topic_id}/{post_number}"
            out.append(
                Opportunity(
                    source=self.name,
                    external_id=f"topic:{topic_id}:post:{post.get('id', post_number)}",
                    url=direct_url,
                    title=title if event_type == "topic" else f"Reply: {title}",
                    body=body,
                    author=username,
                    created_at=created_at,
                    metadata={
                        "platform": "discourse",
                        "category_key": category.key,
                        "category_kind": category.kind,
                        "topic_id": topic_id,
                        "post_id": post.get("id"),
                        "post_number": post_number,
                        "event_type": event_type,
                        "topic_author": topic_author,
                        "is_topic_author": bool(topic_author and username == topic_author),
                        "reply_to_post_number": post.get("reply_to_post_number"),
                        "reply_count": reply_count,
                        "views": topic.get("views"),
                        "topic_created_at": topic.get("created_at"),
                        "topic_last_posted_at": activity_raw,
                    },
                )
            )
            max_seen_post = max(max_seen_post, post_number)

        # Use the greatest post actually observed instead of blindly jumping to
        # highest_post_number. That makes a later cycle retry if Discourse returned
        # only a partial post window.
        if max_seen_post > previous_post:
            self.db.set_state(self._post_key(category, topic_id), str(max_seen_post))
        elif previous_post == 0 and highest == 1:
            self.db.set_state(self._post_key(category, topic_id), "1")
        self.db.set_state(self._activity_key(category, topic_id), activity_raw)
        return out

    async def collect(self) -> list[Opportunity]:
        timeout = httpx.Timeout(20.0)
        headers = {
            "User-Agent": "BuyerIntentRadar/0.16 (public Discourse monitoring; read-only)",
            "Accept": "application/json",
        }
        headers.update(self.request_headers)
        now = datetime.now(timezone.utc)
        sem = asyncio.Semaphore(self.concurrency)
        out: list[Opportunity] = []

        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            for category in self.categories:
                category_url = urljoin(self.base_url, category.path.lstrip("/"))
                try:
                    payload = await self._get_json(client, category_url)
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else "unknown"
                    log.warning("%s category %s failed: HTTP %s", self.name, category.key, status)
                    continue
                except Exception as exc:
                    log.warning("%s category %s failed: %s", self.name, category.key, type(exc).__name__)
                    continue

                topic_list = payload.get("topic_list")
                topics = topic_list.get("topics", []) if isinstance(topic_list, dict) else []
                if not isinstance(topics, list):
                    continue
                topics = topics[: self.max_topics_per_category]
                if category.kind == "marketplace" and len(topics) >= self.max_topics_per_category:
                    # Busy old threads accumulate replies and dominate default
                    # bump sorting. A created-time view gives new job posts a
                    # chance to enter the bounded queue. Unsupported sorting
                    # must never disrupt the normal category fetch.
                    try:
                        created_payload = await self._get_json(client, category_url, params={"order": "created"})
                        created_list = created_payload.get("topic_list")
                        created_topics = created_list.get("topics", []) if isinstance(created_list, dict) else []
                        if isinstance(created_topics, list):
                            by_id = {t.get("id"): t for t in topics if isinstance(t, dict)}
                            for topic in created_topics[: self.max_topics_per_category]:
                                if isinstance(topic, dict) and isinstance(topic.get("id"), int):
                                    by_id.setdefault(topic["id"], topic)
                            # Keep bounded polling cost. Reserve half the slots
                            # for new-by-created threads before bump-sorted ones.
                            newer_ids = [t.get("id") for t in created_topics if isinstance(t, dict)]
                            chosen = [by_id[i] for i in newer_ids[: self.max_topics_per_category // 2] if i in by_id]
                            chosen_ids = {t.get("id") for t in chosen}
                            topics = chosen + [t for t in by_id.values() if t.get("id") not in chosen_ids][
                                : self.max_topics_per_category - len(chosen)
                            ]
                    except (httpx.HTTPError, ValueError, TypeError) as exc:
                        log.debug("%s category %s created order unavailable: %s", self.name, category.key, type(exc).__name__)

                async def fetch(topic: dict[str, Any]) -> list[Opportunity]:
                    async with sem:
                        return await self._fetch_topic_events(client, category, topic, now)

                batches = await asyncio.gather(
                    *(fetch(topic) for topic in topics if isinstance(topic, dict)),
                    return_exceptions=True,
                )
                for batch in batches:
                    if isinstance(batch, BaseException):
                        log.warning(
                            "%s category %s topic fetch failed: %s",
                            self.name,
                            category.key,
                            type(batch).__name__,
                        )
                        continue
                    out.extend(batch)

        out.sort(key=lambda item: item.created_at)
        return out


MAKE_CATEGORIES = (
    DiscourseCategory("hire_help", "c/hire-a-pro/hire-help/74.json", "marketplace"),
    DiscourseCategory("questions", "c/questions/66.json", "business_pain"),
)

N8N_CATEGORIES = (
    DiscourseCategory("jobs", "c/jobs/13.json", "marketplace"),
    DiscourseCategory("build_workflow", "c/help-me-build-my-workflow/36.json", "business_pain"),
)


HUBSPOT_CATEGORIES = (
    DiscourseCategory("apis_integrations", "c/develop-build-integrate/apis-integrations/80", "business_pain"),
)

SHOPIFY_CATEGORIES = (
    DiscourseCategory("webhooks_events", "c/webhooks-and-events/18.json", "business_pain"),
    DiscourseCategory("fulfillment_inventory", "c/fulfillment-inventory/7.json", "business_pain"),
    DiscourseCategory("products_orders", "c/product-and-orders-apis/13.json", "business_pain"),
    DiscourseCategory("authentication", "c/authentication/6.json", "business_pain"),
)

BUBBLE_CATEGORIES = (
    DiscourseCategory("jobs_freelance", "c/jobs-freelance/13.json", "marketplace"),
)

RETOOL_CATEGORIES = (
    DiscourseCategory("discussion", "c/discussion/13.json", "business_pain"),
    DiscourseCategory("workflows", "c/workflows/23.json", "business_pain"),
    DiscourseCategory("app_building", "c/support/5.json", "business_pain"),
)


class MakeCommunityCollector(DiscourseCommunityCollector):
    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 48,
        max_topics_per_category: int = 20,
        concurrency: int = 6,
    ) -> None:
        super().__init__(
            name="make",
            base_url="https://community.make.com/",
            db=db,
            categories=MAKE_CATEGORIES,
            lookback_hours=lookback_hours,
            max_topics_per_category=max_topics_per_category,
            concurrency=concurrency,
        )


class N8nCommunityCollector(DiscourseCommunityCollector):
    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 48,
        max_topics_per_category: int = 20,
        concurrency: int = 6,
    ) -> None:
        super().__init__(
            name="n8n",
            base_url="https://community.n8n.io/",
            db=db,
            categories=N8N_CATEGORIES,
            lookback_hours=lookback_hours,
            max_topics_per_category=max_topics_per_category,
            concurrency=concurrency,
        )


class HubSpotCommunityCollector(DiscourseCommunityCollector):
    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 48,
        max_topics_per_category: int = 20,
        concurrency: int = 6,
    ) -> None:
        super().__init__(
            name="hubspot",
            base_url="https://community.hubspot.com/",
            db=db,
            categories=HUBSPOT_CATEGORIES,
            lookback_hours=lookback_hours,
            max_topics_per_category=max_topics_per_category,
            concurrency=concurrency,
            request_headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/141.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://community.hubspot.com/",
            },
        )


class ShopifyCommunityCollector(DiscourseCommunityCollector):
    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 48,
        max_topics_per_category: int = 20,
        concurrency: int = 6,
    ) -> None:
        super().__init__(
            name="shopify",
            base_url="https://community.shopify.dev/",
            db=db,
            categories=SHOPIFY_CATEGORIES,
            lookback_hours=lookback_hours,
            max_topics_per_category=max_topics_per_category,
            concurrency=concurrency,
        )


class BubbleCommunityCollector(DiscourseCommunityCollector):
    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 72,
        max_topics_per_category: int = 30,
        concurrency: int = 6,
    ) -> None:
        super().__init__(
            name="bubble",
            base_url="https://forum.bubble.io/",
            db=db,
            categories=BUBBLE_CATEGORIES,
            lookback_hours=lookback_hours,
            max_topics_per_category=max_topics_per_category,
            concurrency=concurrency,
        )

class RetoolCommunityCollector(DiscourseCommunityCollector):
    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 168,
        max_topics_per_category: int = 25,
        concurrency: int = 6,
    ) -> None:
        super().__init__(
            name="retool",
            base_url="https://community.retool.com/",
            db=db,
            categories=RETOOL_CATEGORIES,
            lookback_hours=lookback_hours,
            max_topics_per_category=max_topics_per_category,
            concurrency=concurrency,
        )
