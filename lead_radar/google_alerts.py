from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .collectors.rss import FeedDefinition, load_feed_definitions


@dataclass(frozen=True, slots=True)
class GoogleAlertQuery:
    key: str
    query: str
    feed_key: str
    category_kind: str
    priority: str = "P1"
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class GoogleAlertPlanRow:
    key: str
    query: str
    priority: str
    category_kind: str
    feed_key: str
    status: str
    rationale: str


def load_google_alert_queries(path: Path) -> tuple[GoogleAlertQuery, ...]:
    if not path.exists():
        return ()
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    raw_alerts = payload.get("alerts", []) if isinstance(payload, dict) else []
    if not isinstance(raw_alerts, list):
        raise ValueError(f"Invalid Google Alerts query pack: {path}")

    out: list[GoogleAlertQuery] = []
    seen_keys: set[str] = set()
    seen_feed_keys: set[str] = set()
    for raw in raw_alerts:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("key") or "").strip()
        query = str(raw.get("query") or "").strip()
        feed_key = str(raw.get("feed_key") or "").strip()
        if not key or not query or not feed_key:
            continue
        if key in seen_keys:
            raise ValueError(f"Duplicate Google Alerts query key: {key}")
        if feed_key in seen_feed_keys:
            raise ValueError(f"Duplicate Google Alerts feed key: {feed_key}")
        seen_keys.add(key)
        seen_feed_keys.add(feed_key)
        out.append(
            GoogleAlertQuery(
                key=key,
                query=query,
                feed_key=feed_key,
                category_kind=str(raw.get("category_kind") or "business_pain").strip() or "business_pain",
                priority=str(raw.get("priority") or "P1").strip().upper() or "P1",
                rationale=str(raw.get("rationale") or "").strip(),
            )
        )
    return tuple(out)


def is_google_alert_feed_url(url: str) -> bool:
    value = url.strip()
    if not value or "REPLACE_WITH" in value.upper():
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if parsed.scheme != "https":
        return False
    is_google_host = host == "google.com" or host.endswith(".google.com") or host.startswith("www.google.")
    if not is_google_host:
        return False
    return path.startswith("/alerts/feeds/") and len(path.split("/")) >= 4


def combined_feed_definitions(primary_path: Path, local_path: Path | None = None) -> tuple[FeedDefinition, ...]:
    by_key: dict[str, FeedDefinition] = {feed.key: feed for feed in load_feed_definitions(primary_path)}
    if local_path is not None and local_path.exists():
        for feed in load_feed_definitions(local_path):
            by_key[feed.key] = feed
    return tuple(by_key.values())


def build_google_alert_plan(
    queries: tuple[GoogleAlertQuery, ...],
    feeds: tuple[FeedDefinition, ...],
) -> tuple[GoogleAlertPlanRow, ...]:
    feeds_by_key = {feed.key: feed for feed in feeds}
    rows: list[GoogleAlertPlanRow] = []
    priority_order = {"P0": 0, "P1": 1, "P2": 2}
    for query in queries:
        feed = feeds_by_key.get(query.feed_key)
        if feed is None:
            status = "UNCONFIGURED"
        elif feed.source != "google_alerts":
            status = "CONFLICT"
        elif not feed.enabled:
            status = "DISABLED"
        elif not is_google_alert_feed_url(feed.url):
            status = "INVALID_URL"
        else:
            status = "ACTIVE"
        rows.append(
            GoogleAlertPlanRow(
                key=query.key,
                query=query.query,
                priority=query.priority,
                category_kind=query.category_kind,
                feed_key=query.feed_key,
                status=status,
                rationale=query.rationale,
            )
        )
    rows.sort(key=lambda row: (priority_order.get(row.priority, 9), row.key))
    return tuple(rows)


def _read_feed_payload(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"feeds": []}
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid RSS feeds config: {path}")
    raw_feeds = payload.get("feeds", [])
    if not isinstance(raw_feeds, list):
        raise ValueError(f"Invalid RSS feeds config: {path}")
    return {"feeds": raw_feeds}


def upsert_google_alert_feed(path: Path, query: GoogleAlertQuery, url: str) -> None:
    if not is_google_alert_feed_url(url):
        raise ValueError("Expected an HTTPS Google Alerts RSS URL under /alerts/feeds/; no file was changed.")
    payload = _read_feed_payload(path)
    raw_feeds = payload["feeds"]
    assert isinstance(raw_feeds, list)
    entry: dict[str, object] = {
        "key": query.feed_key,
        "source": "google_alerts",
        "name": f"Google Alerts: {query.key}",
        "url": url.strip(),
        "enabled": True,
        "category_kind": query.category_kind,
        "query_key": query.key,
        "search_query": query.query,
        "lookback_hours": 168,
        "max_items": 50,
        "min_poll_minutes": 15,
    }
    replaced = False
    for index, raw in enumerate(raw_feeds):
        if isinstance(raw, dict) and str(raw.get("key") or "").strip() == query.feed_key:
            raw_feeds[index] = entry
            replaced = True
            break
    if not replaced:
        raw_feeds.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(payload, handle, sort_keys=False, allow_unicode=True, width=120)
