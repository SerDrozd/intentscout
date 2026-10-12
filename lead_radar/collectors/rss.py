from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import yaml

from ..database import Database
from ..models import Opportunity
from ..text import strip_html


log = logging.getLogger("lead_radar.collectors.rss")


@dataclass(frozen=True, slots=True)
class FeedDefinition:
    key: str
    source: str
    name: str
    url: str
    enabled: bool = True
    category_kind: str = "business_pain"
    lookback_hours: int = 168
    max_items: int = 50
    min_poll_minutes: int = 5
    query_key: str | None = None
    search_query: str | None = None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _first_child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    wanted = set(names)
    for child in list(node):
        if _local_name(child.tag) in wanted:
            text = "".join(child.itertext()).strip()
            if text:
                return text
    return ""


def _all_links(node: ET.Element) -> list[str]:
    out: list[str] = []
    for child in list(node):
        if _local_name(child.tag) != "link":
            continue
        href = str(child.attrib.get("href") or "").strip()
        if href:
            rel = str(child.attrib.get("rel") or "alternate").strip().lower()
            if rel in {"alternate", ""}:
                out.append(href)
                continue
        text = "".join(child.itertext()).strip()
        if text:
            out.append(text)
    return out


def _unwrap_google_result_url(url: str) -> str:
    """Return the real target for Google redirect links without making a network request."""

    value = url.strip()
    if not value:
        return value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (host == "google.com" or host.endswith(".google.com")):
        return value
    if parsed.path.rstrip("/") != "/url":
        return value
    params = parse_qs(parsed.query)
    for key in ("url", "q"):
        candidates = params.get(key, [])
        if not candidates:
            continue
        target = str(candidates[0]).strip()
        target_parsed = urlparse(target)
        if target_parsed.scheme in {"http", "https"} and target_parsed.hostname:
            return target
    return value


def _parse_date(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_author(node: ET.Element) -> str | None:
    creator = _first_child_text(node, ("creator",))
    if creator:
        return strip_html(creator) or None
    for child in list(node):
        if _local_name(child.tag) != "author":
            continue
        nested = _first_child_text(child, ("name", "email"))
        if nested:
            return strip_html(nested) or None
        direct = "".join(child.itertext()).strip()
        if direct:
            return strip_html(direct) or None
    return None


def _entry_body(node: ET.Element) -> str:
    raw = _first_child_text(node, ("encoded", "content", "description", "summary"))
    return strip_html(raw)


def _entry_external_id(node: ET.Element, url: str, title: str, created: datetime) -> str:
    explicit = _first_child_text(node, ("guid", "id"))
    if explicit:
        return explicit
    fingerprint = f"{url}\n{title}\n{created.isoformat()}".encode("utf-8", "replace")
    return hashlib.sha256(fingerprint).hexdigest()


def _iter_feed_entries(root: ET.Element) -> list[ET.Element]:
    root_name = _local_name(root.tag).lower()
    if root_name == "feed":
        return [node for node in list(root) if _local_name(node.tag).lower() == "entry"]
    if root_name in {"rss", "rdf", "rdf:rdf"}:
        entries: list[ET.Element] = []
        for node in root.iter():
            if _local_name(node.tag).lower() == "item":
                entries.append(node)
        return entries
    return [node for node in root.iter() if _local_name(node.tag).lower() in {"item", "entry"}]


def load_feed_definitions(path: Path) -> tuple[FeedDefinition, ...]:
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    raw_feeds = payload.get("feeds", []) if isinstance(payload, dict) else []
    if not isinstance(raw_feeds, list):
        raise ValueError(f"Invalid RSS feeds config: {path}")

    feeds: list[FeedDefinition] = []
    seen_keys: set[str] = set()
    for raw in raw_feeds:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        url = str(raw.get("url") or "").strip()
        if not key or not url:
            continue
        if key in seen_keys:
            raise ValueError(f"Duplicate RSS feed key: {key}")
        seen_keys.add(key)
        source = str(raw.get("source") or "rss").strip() or "rss"
        name = str(raw.get("name") or key).strip() or key
        category_kind = str(raw.get("category_kind") or "business_pain").strip() or "business_pain"
        feeds.append(
            FeedDefinition(
                key=key,
                source=source,
                name=name,
                url=url,
                enabled=bool(raw.get("enabled", True)),
                category_kind=category_kind,
                lookback_hours=max(1, int(raw.get("lookback_hours", 168))),
                max_items=max(1, min(200, int(raw.get("max_items", 50)))),
                min_poll_minutes=max(0, int(raw.get("min_poll_minutes", 5))),
                query_key=(str(raw.get("query_key") or "").strip() or None),
                search_query=(str(raw.get("search_query") or "").strip() or None),
            )
        )
    return tuple(feeds)


class RSSCollector:
    name = "rss"

    def __init__(
        self,
        db: Database,
        *,
        config_path: Path,
        extra_config_paths: tuple[Path, ...] = (),
        concurrency: int = 4,
        overlap_seconds: int = 300,
    ) -> None:
        self.db = db
        self.config_path = config_path
        self.extra_config_paths = extra_config_paths
        self.concurrency = max(1, min(8, concurrency))
        self.overlap_seconds = max(0, overlap_seconds)

    def _state(self, feed: FeedDefinition) -> dict[str, Any]:
        raw = self.db.get_state(f"rss:{feed.key}:http")
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _lower_bound(self, feed: FeedDefinition, now: datetime) -> datetime:
        fallback = now - timedelta(hours=feed.lookback_hours)
        raw = self.db.get_state(f"rss:{feed.key}:cursor")
        if not raw:
            return fallback
        try:
            saved = datetime.fromisoformat(raw)
        except ValueError:
            return fallback
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=timezone.utc)
        saved = saved.astimezone(timezone.utc) - timedelta(seconds=self.overlap_seconds)
        return max(fallback, saved)

    async def _collect_feed(self, client: httpx.AsyncClient, feed: FeedDefinition) -> list[Opportunity]:
        now = datetime.now(timezone.utc)
        last_fetch_raw = self.db.get_state(f"rss:{feed.key}:last_fetch")
        if last_fetch_raw and feed.min_poll_minutes > 0:
            try:
                last_fetch = datetime.fromisoformat(last_fetch_raw)
            except ValueError:
                last_fetch = None
            if last_fetch is not None:
                if last_fetch.tzinfo is None:
                    last_fetch = last_fetch.replace(tzinfo=timezone.utc)
                age = now - last_fetch.astimezone(timezone.utc)
                if age < timedelta(minutes=feed.min_poll_minutes):
                    return []
        self.db.set_state(f"rss:{feed.key}:last_fetch", now.isoformat())

        state = self._state(feed)
        headers = {
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        }
        etag = str(state.get("etag") or "").strip()
        last_modified = str(state.get("last_modified") or "").strip()
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        try:
            response = await client.get(feed.url, headers=headers)
            if response.status_code == 304:
                return []
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("RSS feed %s failed: HTTP %s", feed.key, exc.response.status_code)
            return []
        except httpx.HTTPError:
            log.warning("RSS feed %s failed: HTTP error", feed.key)
            return []

        http_state: dict[str, str] = {}
        response_etag = response.headers.get("ETag")
        response_modified = response.headers.get("Last-Modified")
        if response_etag:
            http_state["etag"] = response_etag
        if response_modified:
            http_state["last_modified"] = response_modified
        if http_state:
            self.db.set_state(f"rss:{feed.key}:http", json.dumps(http_state, ensure_ascii=False))

        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            log.warning("RSS feed %s returned invalid XML", feed.key)
            return []

        lower_bound = self._lower_bound(feed, now)
        newest = lower_bound
        opportunities: list[Opportunity] = []
        entries = _iter_feed_entries(root)
        for node in entries[: feed.max_items]:
            created_raw = _first_child_text(node, ("pubDate", "published", "updated", "date"))
            created = _parse_date(created_raw)
            if created is None or created < lower_bound:
                continue
            newest = max(newest, created)

            title = strip_html(_first_child_text(node, ("title",)))
            links = _all_links(node)
            raw_url = links[0] if links else _first_child_text(node, ("link",))
            url = _unwrap_google_result_url(raw_url) if feed.source == "google_alerts" else raw_url
            body = _entry_body(node)
            if not title and not body:
                continue
            external_id = _entry_external_id(node, raw_url or url, title, created)
            target_host = (urlparse(url).hostname or "").lower() if url else ""
            metadata: dict[str, Any] = {
                "platform": "rss",
                "feed_key": feed.key,
                "feed_name": feed.name,
                "feed_url": feed.url,
                "category_kind": feed.category_kind,
                "query_key": feed.query_key,
                "search_query": feed.search_query,
                "event_type": "topic",
                "reply_count": None,
                "target_host": target_host or None,
            }
            if feed.source == "google_alerts" and raw_url and raw_url != url:
                metadata["google_redirect_url"] = raw_url
            opportunities.append(
                Opportunity(
                    source=feed.source,
                    external_id=external_id,
                    url=url or feed.url,
                    title=title or feed.name,
                    body=body,
                    author=_entry_author(node),
                    created_at=created,
                    metadata=metadata,
                )
            )

        if newest > lower_bound:
            self.db.set_state(f"rss:{feed.key}:cursor", newest.isoformat())
        return opportunities

    async def collect(self) -> list[Opportunity]:
        by_key: dict[str, FeedDefinition] = {feed.key: feed for feed in load_feed_definitions(self.config_path)}
        for path in self.extra_config_paths:
            if not path.exists():
                continue
            for feed in load_feed_definitions(path):
                by_key[feed.key] = feed
        feeds = tuple(feed for feed in by_key.values() if feed.enabled)
        if not feeds:
            return []

        semaphore = asyncio.Semaphore(self.concurrency)
        timeout = httpx.Timeout(20.0)
        headers = {"User-Agent": "intentscout/0.7"}

        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            async def run(feed: FeedDefinition) -> list[Opportunity]:
                async with semaphore:
                    return await self._collect_feed(client, feed)

            results = await asyncio.gather(*(run(feed) for feed in feeds))

        out: dict[tuple[str, str], Opportunity] = {}
        for items in results:
            for item in items:
                out[(item.source, item.external_id)] = item
        return list(out.values())
