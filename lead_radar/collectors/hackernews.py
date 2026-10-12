from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from ..database import Database
from ..models import Opportunity
from ..text import strip_html


BASE = "https://hacker-news.firebaseio.com/v0"


class HackerNewsCollector:
    name = "hackernews"

    def __init__(self, db: Database, bootstrap_items: int = 300, max_items_per_cycle: int = 500, concurrency: int = 20) -> None:
        self.db = db
        self.bootstrap_items = bootstrap_items
        self.max_items_per_cycle = max_items_per_cycle
        self.concurrency = concurrency

    async def collect(self) -> list[Opportunity]:
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "intentscout/0.1"}) as client:
            r = await client.get(f"{BASE}/maxitem.json")
            r.raise_for_status()
            current_max = int(r.json())

            state_raw = self.db.get_state("hn_last_item")
            if state_raw is None:
                start = max(1, current_max - self.bootstrap_items + 1)
            else:
                start = int(state_raw) + 1

            if start > current_max:
                return []

            end = min(current_max, start + self.max_items_per_cycle - 1)
            ids = list(range(start, end + 1))
            sem = asyncio.Semaphore(self.concurrency)

            async def fetch(item_id: int) -> dict | None:
                async with sem:
                    try:
                        resp = await client.get(f"{BASE}/item/{item_id}.json")
                        resp.raise_for_status()
                        return resp.json()
                    except (httpx.HTTPError, ValueError):
                        return None

            raw_items = await asyncio.gather(*(fetch(item_id) for item_id in ids))
            self.db.set_state("hn_last_item", str(end))

        out: list[Opportunity] = []
        for item in raw_items:
            if not item or item.get("deleted") or item.get("dead"):
                continue
            item_type = item.get("type")
            if item_type not in {"story", "comment", "job"}:
                continue
            item_id = str(item["id"])
            title = strip_html(item.get("title") or "")
            body = strip_html(item.get("text") or "")
            if not title and not body:
                continue
            out.append(
                Opportunity(
                    source=self.name,
                    external_id=item_id,
                    url=f"https://news.ycombinator.com/item?id={item_id}",
                    title=title or f"HN {item_type}",
                    body=body,
                    author=item.get("by"),
                    created_at=datetime.fromtimestamp(int(item.get("time", 0)), tz=timezone.utc),
                    metadata={
                        "type": item_type,
                        "parent": item.get("parent"),
                        "reply_count": len(item.get("kids") or []),
                        "descendants": item.get("descendants"),
                    },
                )
            )
        return out
