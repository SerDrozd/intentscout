from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from ..database import Database
from ..models import Opportunity
from ..text import strip_html


log = logging.getLogger("lead_radar.collectors.web_community")


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/141.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


@dataclass(frozen=True, slots=True)
class WebCommunitySection:
    key: str
    index_url: str
    topic_path_pattern: str
    kind: str
    fallback_index_urls: tuple[str, ...] = ()


@dataclass(slots=True)
class ParsedTopic:
    title: str
    body: str
    author: str | None
    created_at: datetime
    reply_count: int | None


class _HTMLSignalsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self.time_values: list[str] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.article_blocks: list[list[str]] = []
        self.main_parts: list[str] = []
        self.jsonld_blocks: list[str] = []

        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._title_depth = 0
        self._h1_depth = 0
        self._article_depth = 0
        self._main_depth = 0
        self._jsonld_depth = 0
        self._jsonld_parts: list[str] = []

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.casefold(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        amap = self._attrs(attrs)
        if tag == "a":
            href = amap.get("href")
            if href:
                self._anchor_href = href
                self._anchor_parts = []
        elif tag == "meta":
            key = (amap.get("property") or amap.get("name") or amap.get("itemprop") or "").casefold()
            content = amap.get("content", "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content
        elif tag == "time":
            value = amap.get("datetime", "").strip()
            if value:
                self.time_values.append(value)
        elif tag == "title":
            self._title_depth += 1
        elif tag == "h1":
            self._h1_depth += 1
        elif tag == "article":
            self._article_depth += 1
            if self._article_depth == 1:
                self.article_blocks.append([])
        elif tag == "main":
            self._main_depth += 1
        elif tag == "script" and amap.get("type", "").casefold() == "application/ld+json":
            self._jsonld_depth += 1
            if self._jsonld_depth == 1:
                self._jsonld_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchor_href is not None:
            text = " ".join(part.strip() for part in self._anchor_parts if part.strip())
            self.links.append((self._anchor_href, text))
            self._anchor_href = None
            self._anchor_parts = []
        elif tag == "title" and self._title_depth:
            self._title_depth -= 1
        elif tag == "h1" and self._h1_depth:
            self._h1_depth -= 1
        elif tag == "article" and self._article_depth:
            self._article_depth -= 1
        elif tag == "main" and self._main_depth:
            self._main_depth -= 1
        elif tag == "script" and self._jsonld_depth:
            self._jsonld_depth -= 1
            if self._jsonld_depth == 0:
                raw = "".join(self._jsonld_parts).strip()
                if raw:
                    self.jsonld_blocks.append(raw)
                self._jsonld_parts = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._anchor_href is not None:
            self._anchor_parts.append(text)
        if self._title_depth:
            self.title_parts.append(text)
        if self._h1_depth:
            self.h1_parts.append(text)
        if self._article_depth and self.article_blocks:
            self.article_blocks[-1].append(text)
        if self._main_depth:
            self.main_parts.append(text)
        if self._jsonld_depth:
            self._jsonld_parts.append(data)


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = strip_html(unescape(value))
    return re.sub(r"\s+", " ", cleaned).strip()


def _canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path.rstrip("/") or "/", "", ""))


def _parse_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    value = raw.strip()
    parsed: datetime | None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            parsed = None
    if parsed is None:
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _walk_json(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _jsonld_topic(parser: _HTMLSignalsParser) -> tuple[str, str, str | None, datetime | None, int | None] | None:
    preferred_types = {
        "discussionforumposting",
        "question",
        "article",
        "blogposting",
        "socialmediaposting",
    }
    candidates: list[tuple[int, dict[str, Any]]] = []
    for block in parser.jsonld_blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        for item in _walk_json(payload):
            raw_type = item.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            normalized = {str(entry).casefold() for entry in types if entry}
            score = 3 if normalized & preferred_types else 0
            if any(key in item for key in ("headline", "articleBody", "text", "datePublished")):
                score += 1
            if score:
                candidates.append((score, item))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    item = candidates[0][1]
    title = _clean_text(item.get("headline") or item.get("name"))
    body = _clean_text(item.get("articleBody") or item.get("text") or item.get("description"))
    created_at = _parse_datetime(item.get("datePublished") or item.get("dateCreated"))
    author_raw = item.get("author")
    author: str | None = None
    if isinstance(author_raw, dict):
        author = _clean_text(author_raw.get("name")) or None
    elif isinstance(author_raw, list):
        for entry in author_raw:
            if isinstance(entry, dict) and _clean_text(entry.get("name")):
                author = _clean_text(entry.get("name"))
                break
    elif isinstance(author_raw, str):
        author = _clean_text(author_raw) or None
    replies_raw = item.get("commentCount") or item.get("replyCount")
    try:
        reply_count = int(replies_raw) if replies_raw is not None else None
    except (TypeError, ValueError):
        reply_count = None
    return title, body, author, created_at, reply_count


def _parse_topic_html(html: str) -> ParsedTopic | None:
    parser = _HTMLSignalsParser()
    parser.feed(html)

    jsonld = _jsonld_topic(parser)
    title = ""
    body = ""
    author: str | None = None
    created_at: datetime | None = None
    reply_count: int | None = None
    if jsonld is not None:
        title, body, author, created_at, reply_count = jsonld

    if not title:
        title = _clean_text(
            parser.meta.get("og:title")
            or parser.meta.get("twitter:title")
            or " ".join(parser.h1_parts)
            or " ".join(parser.title_parts)
        )
    if not body:
        for block in parser.article_blocks:
            candidate = _clean_text(" ".join(block))
            if len(candidate) >= 60:
                body = candidate
                break
    if not body:
        body = _clean_text(parser.meta.get("description") or parser.meta.get("og:description"))
    if not body:
        candidate = _clean_text(" ".join(parser.main_parts))
        if len(candidate) >= 60:
            body = candidate

    if author is None:
        author = _clean_text(parser.meta.get("author")) or None
    if created_at is None:
        for key in (
            "article:published_time",
            "datepublished",
            "date",
            "publish-date",
            "publish_date",
        ):
            created_at = _parse_datetime(parser.meta.get(key))
            if created_at is not None:
                break
    if created_at is None:
        for raw in parser.time_values:
            created_at = _parse_datetime(raw)
            if created_at is not None:
                break

    if not title or not body or created_at is None:
        return None
    # Keep enough context for deterministic + LLM stages without storing entire pages.
    return ParsedTopic(
        title=title[:500],
        body=body[:12000],
        author=author,
        created_at=created_at,
        reply_count=reply_count,
    )


class PublicCommunityWebCollector:
    """Safe public HTML collector for communities without stable JSON/RSS APIs.

    This adapter intentionally collects topic starters only. It never logs in,
    bypasses CAPTCHA, or executes a browser. Fresh reply monitoring remains on the
    dedicated Discourse adapters until a similarly stable public mechanism exists.
    """

    def __init__(
        self,
        *,
        name: str,
        db: Database,
        sections: tuple[WebCommunitySection, ...],
        lookback_hours: int,
        max_topics_per_section: int,
        concurrency: int = 4,
    ) -> None:
        self.name = name
        self.db = db
        self.sections = sections
        self.lookback_hours = max(1, lookback_hours)
        self.max_topics_per_section = max(1, min(40, max_topics_per_section))
        self.concurrency = max(1, min(8, concurrency))

    async def _get_text(self, client: httpx.AsyncClient, url: str) -> str:
        response: httpx.Response | None = None
        for attempt in range(3):
            response = await client.get(url)
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
            return response.text
        assert response is not None
        raise httpx.HTTPStatusError("public page request failed", request=response.request, response=response)

    @staticmethod
    def _topic_links(html: str, section: WebCommunitySection, index_url: str) -> list[str]:
        parser = _HTMLSignalsParser()
        parser.feed(html)
        index = urlsplit(index_url)
        pattern = re.compile(section.topic_path_pattern)
        found: list[str] = []
        seen: set[str] = set()
        for href, _anchor_text in parser.links:
            absolute = _canonical_url(urljoin(index_url, href))
            parts = urlsplit(absolute)
            if parts.netloc.casefold() != index.netloc.casefold():
                continue
            if not pattern.search(parts.path):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            found.append(absolute)
        return found

    def _seen_key(self, section: WebCommunitySection, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8"), usedforsecurity=False).hexdigest()
        # Include lookback in the key so widening the bootstrap window can safely
        # revisit items that were previously outside the accepted age range.
        return f"web:{self.name}:{section.key}:{self.lookback_hours}:{digest}:seen"

    @staticmethod
    def _external_id(url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"url:{digest}"

    async def collect(self) -> list[Opportunity]:
        timeout = httpx.Timeout(20.0)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=self.lookback_hours)
        sem = asyncio.Semaphore(self.concurrency)
        out: list[Opportunity] = []

        async with httpx.AsyncClient(
            timeout=timeout,
            headers=_BROWSER_HEADERS,
            follow_redirects=True,
        ) as client:
            for section in self.sections:
                index_html: str | None = None
                active_index_url = section.index_url
                for candidate_url in (section.index_url, *section.fallback_index_urls):
                    try:
                        index_html = await self._get_text(client, candidate_url)
                        active_index_url = candidate_url
                        break
                    except httpx.HTTPStatusError as exc:
                        log.warning(
                            "%s section %s failed at %s: HTTP %s",
                            self.name,
                            section.key,
                            candidate_url,
                            exc.response.status_code,
                        )
                    except Exception as exc:
                        log.warning(
                            "%s section %s failed at %s: %s",
                            self.name,
                            section.key,
                            candidate_url,
                            type(exc).__name__,
                        )
                if index_html is None:
                    continue

                links = self._topic_links(index_html, section, active_index_url)[: self.max_topics_per_section]

                async def fetch(url: str) -> Opportunity | None:
                    external_id = self._external_id(url)
                    if self.db.exists(self.name, external_id):
                        return None
                    state_key = self._seen_key(section, url)
                    if self.db.get_state(state_key) is not None:
                        return None
                    async with sem:
                        try:
                            topic_html = await self._get_text(client, url)
                        except httpx.HTTPStatusError as exc:
                            log.warning("%s topic failed: HTTP %s", self.name, exc.response.status_code)
                            return None
                        except Exception as exc:
                            log.warning("%s topic failed: %s", self.name, type(exc).__name__)
                            return None
                    parsed = _parse_topic_html(topic_html)
                    if parsed is None:
                        # Do not permanently mark unparseable pages: the source may
                        # change markup and a later release can recover them.
                        return None
                    self.db.set_state(state_key, parsed.created_at.isoformat())
                    if parsed.created_at < cutoff or parsed.created_at > now + timedelta(hours=2):
                        return None
                    return Opportunity(
                        source=self.name,
                        external_id=external_id,
                        url=url,
                        title=parsed.title,
                        body=parsed.body,
                        author=parsed.author,
                        created_at=parsed.created_at,
                        metadata={
                            "platform": "community_web",
                            "category_key": section.key,
                            "category_kind": section.kind,
                            "event_type": "topic",
                            "reply_count": parsed.reply_count,
                            "listing_url": active_index_url,
                        },
                    )

                batches = await asyncio.gather(*(fetch(url) for url in links), return_exceptions=True)
                for item in batches:
                    if isinstance(item, BaseException):
                        log.warning("%s section %s topic task failed: %s", self.name, section.key, type(item).__name__)
                    elif item is not None:
                        out.append(item)

        out.sort(key=lambda item: item.created_at)
        return out


HUBSPOT_WEB_SECTIONS = (
    WebCommunitySection(
        "apis_integrations",
        "https://community.hubspot.com/c/develop-build-integrate/apis-integrations/80",
        r"^/t/.+/\d+$",
        "business_pain",
        fallback_index_urls=(
            "https://community.hubspot.com/c/develop-build-integrate/apis-integrations/80?_nospa=true",
            "https://community.hubspot.com/c/develop-build-integrate/apis-integrations/80?retURL=%2F",
        ),
    ),
)

AIRTABLE_WEB_SECTIONS = (
    WebCommunitySection(
        "jobs_board",
        "https://community.airtable.com/jobs-board-16",
        r"^/jobs-board-16/.+-\d+$",
        "marketplace",
    ),
)

ZAPIER_WEB_SECTIONS = (
    WebCommunitySection(
        "code_webhooks",
        "https://community.zapier.com/code-webhooks-52",
        r"^/code-webhooks-52/.+-\d+$",
        "business_pain",
    ),
    WebCommunitySection(
        "troubleshooting",
        "https://community.zapier.com/troubleshooting-99",
        r"^/troubleshooting-99/.+-\d+$",
        "business_pain",
    ),
    WebCommunitySection(
        "how_do_i",
        "https://community.zapier.com/how-do-i-3",
        r"^/how-do-i-3/.+-\d+$",
        "business_pain",
    ),
)


class HubSpotWebCollector(PublicCommunityWebCollector):
    def __init__(self, db: Database, *, lookback_hours: int = 72, max_topics_per_section: int = 20, concurrency: int = 4) -> None:
        super().__init__(
            name="hubspot",
            db=db,
            sections=HUBSPOT_WEB_SECTIONS,
            lookback_hours=lookback_hours,
            max_topics_per_section=max_topics_per_section,
            concurrency=concurrency,
        )


class AirtableWebCollector(PublicCommunityWebCollector):
    def __init__(self, db: Database, *, lookback_hours: int = 336, max_topics_per_section: int = 20, concurrency: int = 4) -> None:
        super().__init__(
            name="airtable",
            db=db,
            sections=AIRTABLE_WEB_SECTIONS,
            lookback_hours=lookback_hours,
            max_topics_per_section=max_topics_per_section,
            concurrency=concurrency,
        )


class ZapierWebCollector(PublicCommunityWebCollector):
    def __init__(self, db: Database, *, lookback_hours: int = 168, max_topics_per_section: int = 15, concurrency: int = 4) -> None:
        super().__init__(
            name="zapier",
            db=db,
            sections=ZAPIER_WEB_SECTIONS,
            lookback_hours=lookback_hours,
            max_topics_per_section=max_topics_per_section,
            concurrency=concurrency,
        )
