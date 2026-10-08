from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .models import Opportunity


def _canonical_linkedin_url(url: str) -> str:
    raw = url.strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").casefold()
    if parts.scheme not in {"http", "https"} or not host:
        raise ValueError("LinkedIn URL must be an absolute http(s) URL.")
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        raise ValueError("linkedin-add accepts only linkedin.com URLs.")
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def _parse_created_at(raw: str | None) -> tuple[datetime, bool]:
    if raw is None or not raw.strip():
        return datetime.now(timezone.utc), True
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--created-at must be ISO 8601, for example 2026-10-08T12:30:00+03:00") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), False


def build_linkedin_opportunity(
    *,
    url: str,
    body: str,
    title: str | None = None,
    author: str | None = None,
    created_at: str | None = None,
    query_key: str | None = None,
) -> Opportunity:
    text = body.strip()
    if len(text) < 20:
        raise ValueError("LinkedIn post text is too short; copy the actual post body.")
    canonical_url = _canonical_linkedin_url(url)
    created, assumed = _parse_created_at(created_at)
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
    clean_title = (title or "").strip()
    if not clean_title:
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "LinkedIn post")
        clean_title = first_line[:180]
    metadata: dict[str, object] = {
        "platform": "manual_social",
        "category_kind": "marketplace",
        "event_type": "topic",
        "manual_ingest": True,
        "created_at_assumed": assumed,
    }
    if query_key:
        metadata["query_key"] = query_key.strip()
    return Opportunity(
        source="linkedin",
        external_id=f"linkedin:{digest}",
        url=canonical_url,
        title=clean_title[:500],
        body=text[:12000],
        author=(author or "").strip() or None,
        created_at=created,
        metadata=metadata,
    )
