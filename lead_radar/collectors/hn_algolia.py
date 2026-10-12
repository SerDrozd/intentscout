from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..database import Database
from ..models import Opportunity
from ..text import strip_html


SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"


@dataclass(frozen=True, slots=True)
class HNQueryFamily:
    key: str
    query: str


QUERY_FAMILIES: tuple[HNQueryFamily, ...] = (
    HNQueryFamily("hire_developer", '"looking for" developer'),
    HNQueryFamily("need_developer", '"need a developer"'),
    HNQueryFamily("contractor", "contractor"),
    HNQueryFamily("freelancer", "freelancer"),
    HNQueryFamily("pay_help", '"willing to pay"'),
    HNQueryFamily("need_help_api", '"need help" API'),
    HNQueryFamily("webhook_help", "webhook help"),
    HNQueryFamily("automation_help", "automation help"),
    HNQueryFamily("manual_process", '"manual process"'),
    HNQueryFamily("client_project", '"client project"'),
    HNQueryFamily("still_looking", '"still looking"'),
    HNQueryFamily("dm_me", '"DM me"'),
)


class HackerNewsAlgoliaCollector:
    name = "hn_algolia"

    def __init__(
        self,
        db: Database,
        *,
        lookback_hours: int = 48,
        results_per_query: int = 50,
        queries_per_cycle: int = 6,
        overlap_seconds: int = 300,
        query_keys: tuple[str, ...] | None = None,
    ) -> None:
        self.db = db
        self.lookback_hours = max(1, lookback_hours)
        self.results_per_query = max(1, min(100, results_per_query))
        self.queries_per_cycle = max(1, min(len(QUERY_FAMILIES), queries_per_cycle))
        self.overlap_seconds = max(0, overlap_seconds)
        self.query_keys = query_keys

    def _selected_queries(self) -> tuple[HNQueryFamily, ...]:
        if self.query_keys is not None:
            wanted = set(self.query_keys)
            return tuple(family for family in QUERY_FAMILIES if family.key in wanted)
        raw = self.db.get_state("hn_algolia_query_index")
        try:
            start = int(raw) if raw is not None else 0
        except ValueError:
            start = 0
        selected = tuple(
            QUERY_FAMILIES[(start + offset) % len(QUERY_FAMILIES)] for offset in range(self.queries_per_cycle)
        )
        self.db.set_state("hn_algolia_query_index", str((start + self.queries_per_cycle) % len(QUERY_FAMILIES)))
        return selected

    def _lower_bound(self, family: HNQueryFamily, now: datetime) -> int:
        fallback = int((now - timedelta(hours=self.lookback_hours)).timestamp())
        raw = self.db.get_state(f"hn_algolia:{family.key}:cursor")
        if raw is None:
            return fallback
        try:
            saved = int(raw)
        except ValueError:
            return fallback
        return max(fallback, saved - self.overlap_seconds)

    async def collect(self) -> list[Opportunity]:
        now = datetime.now(timezone.utc)
        out: dict[str, Opportunity] = {}
        timeout = httpx.Timeout(20.0)
        headers = {"User-Agent": "intentscout/0.6", "Accept": "application/json"}

        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            for family in self._selected_queries():
                lower_bound = self._lower_bound(family, now)
                try:
                    response = await client.get(
                        SEARCH_URL,
                        params={
                            "query": family.query,
                            "tags": "(story,comment)",
                            "numericFilters": f"created_at_i>{lower_bound}",
                            "hitsPerPage": self.results_per_query,
                            "page": 0,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                except (httpx.HTTPError, ValueError):
                    continue

                max_timestamp = lower_bound
                hits = payload.get("hits", []) if isinstance(payload, dict) else []
                if not isinstance(hits, list):
                    continue

                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    external_id = str(hit.get("objectID") or "").strip()
                    created_i = hit.get("created_at_i")
                    if not isinstance(created_i, (int, str)):
                        continue
                    try:
                        created_timestamp = int(created_i)
                    except ValueError:
                        continue
                    max_timestamp = max(max_timestamp, created_timestamp)
                    if not external_id:
                        continue

                    title_raw = hit.get("title") or hit.get("story_title") or ""
                    comment_text = strip_html(str(hit.get("comment_text") or ""))
                    story_text = strip_html(str(hit.get("story_text") or ""))
                    title = strip_html(str(title_raw))
                    item_type = "comment" if comment_text else "story"
                    body = comment_text if comment_text else story_text
                    if not title and not body:
                        continue
                    if item_type == "comment" and title:
                        display_title = f"Reply: {title}"
                    else:
                        display_title = title or "HN story"

                    out[external_id] = Opportunity(
                        source="hackernews",
                        external_id=external_id,
                        url=f"https://news.ycombinator.com/item?id={external_id}",
                        title=display_title,
                        body=body,
                        author=hit.get("author"),
                        created_at=datetime.fromtimestamp(created_timestamp, tz=timezone.utc),
                        metadata={
                            "type": item_type,
                            "platform": "hn_algolia",
                            "query_family": family.key,
                            "query": family.query,
                            "story_id": hit.get("story_id"),
                            "parent_id": hit.get("parent_id"),
                            "points": hit.get("points"),
                            "reply_count": hit.get("num_comments"),
                        },
                    )

                if max_timestamp > lower_bound:
                    self.db.set_state(f"hn_algolia:{family.key}:cursor", str(max_timestamp))

        return list(out.values())
