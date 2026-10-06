from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Any

import httpx

from ..database import Database
from ..models import Opportunity


SEARCH_URLS = (
    "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
    "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
)


@dataclass(frozen=True, slots=True)
class BlueskyQueryFamily:
    key: str
    query: str


QUERY_FAMILIES: tuple[BlueskyQueryFamily, ...] = (
    BlueskyQueryFamily("looking_developer", '"looking for developer"'),
    BlueskyQueryFamily("need_developer", '"need a developer"'),
    BlueskyQueryFamily("looking_contractor", '"looking for contractor"'),
    BlueskyQueryFamily("need_freelancer", '"need a freelancer"'),
    BlueskyQueryFamily("need_automation", '"need automation"'),
    BlueskyQueryFamily("automation_help", '"automation help"'),
    BlueskyQueryFamily("api_integration", '"API integration" help'),
    BlueskyQueryFamily("webhook_help", '"webhook" help'),
    BlueskyQueryFamily("willing_pay", '"willing to pay"'),
    BlueskyQueryFamily("paid_project", '"paid project" automation'),
    BlueskyQueryFamily("need_backend", '"need backend"'),
    BlueskyQueryFamily("client_project", '"client project" developer'),
)


class BlueskyCollector:
    name = "bluesky"

    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 24,
        results_per_query: int = 25,
        queries_per_cycle: int = 6,
    ) -> None:
        self.db = db
        self.lookback_hours = max(1, lookback_hours)
        self.results_per_query = max(1, min(100, results_per_query))
        self.queries_per_cycle = max(1, min(len(QUERY_FAMILIES), queries_per_cycle))

    def _selected_queries(self) -> tuple[BlueskyQueryFamily, ...]:
        raw = self.db.get_state("bluesky_query_index")
        try:
            start = int(raw) if raw is not None else 0
        except ValueError:
            start = 0
        selected = tuple(
            QUERY_FAMILIES[(start + offset) % len(QUERY_FAMILIES)] for offset in range(self.queries_per_cycle)
        )
        self.db.set_state("bluesky_query_index", str((start + self.queries_per_cycle) % len(QUERY_FAMILIES)))
        return selected

    @staticmethod
    def _post_url(uri: str, handle: str | None) -> str:
        rkey = uri.rsplit("/", 1)[-1]
        if handle:
            return f"https://bsky.app/profile/{quote(handle, safe='.:')}/post/{quote(rkey, safe='')}"
        return uri

    async def collect(self) -> list[Opportunity]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        out: dict[str, Opportunity] = {}
        timeout = httpx.Timeout(20.0)
        headers = {"User-Agent": "technical-api-problem-radar/0.6", "Accept": "application/json"}

        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            for family in self._selected_queries():
                payload: dict[str, Any] | None = None
                for search_url in SEARCH_URLS:
                    try:
                        response = await client.get(
                            search_url,
                            params={
                                "q": family.query,
                                "sort": "latest",
                                "limit": self.results_per_query,
                            },
                        )
                        response.raise_for_status()
                        parsed = response.json()
                        if isinstance(parsed, dict):
                            payload = parsed
                            break
                    except (httpx.HTTPError, ValueError):
                        continue
                if payload is None:
                    continue

                posts = payload.get("posts", [])
                if not isinstance(posts, list):
                    continue
                for post in posts:
                    if not isinstance(post, dict):
                        continue
                    uri = str(post.get("uri") or "").strip()
                    record_raw = post.get("record")
                    author_raw = post.get("author")
                    record: dict[str, Any] = record_raw if isinstance(record_raw, dict) else {}
                    author: dict[str, Any] = author_raw if isinstance(author_raw, dict) else {}
                    text = str(record.get("text") or "").strip()
                    created_raw = record.get("createdAt") or post.get("indexedAt")
                    if not uri or not text or not created_raw:
                        continue
                    try:
                        created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    created = created.astimezone(timezone.utc)
                    if created < cutoff:
                        continue

                    handle = str(author.get("handle") or "").strip() or None
                    first_line = text.splitlines()[0].strip()
                    title = first_line[:140] if first_line else "Bluesky post"
                    out[uri] = Opportunity(
                        source=self.name,
                        external_id=uri,
                        url=self._post_url(uri, handle),
                        title=title,
                        body=text,
                        author=handle,
                        created_at=created,
                        metadata={
                            "platform": "bluesky",
                            "query_family": family.key,
                            "query": family.query,
                            "author_did": author.get("did"),
                            "display_name": author.get("displayName"),
                            "reply_count": post.get("replyCount"),
                            "like_count": post.get("likeCount"),
                            "repost_count": post.get("repostCount"),
                            "quote_count": post.get("quoteCount"),
                        },
                    )

        return list(out.values())
