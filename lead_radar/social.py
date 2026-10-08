"""User-initiated ingestion of public social posts; never fetches or posts to a network."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Opportunity

SOCIAL_PLATFORMS = ("x", "facebook", "reddit", "threads", "telegram", "discord")
_ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "x": frozenset({"x.com", "twitter.com", "mobile.twitter.com"}),
    "facebook": frozenset({"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}),
    "reddit": frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "redd.it"}),
    "threads": frozenset({"threads.com", "www.threads.com", "threads.net", "www.threads.net"}),
    "telegram": frozenset({"t.me", "telegram.me"}),
    "discord": frozenset({"discord.com", "www.discord.com", "discordapp.com"}),
}
_TRACKING_PARAMS = {"fbclid", "gclid", "ref", "ref_src", "s", "si", "share_id", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


def canonical_social_url(platform: str, url: str) -> str:
    """Keep canonical post identity; reject profiles, search pages and look-alike hosts."""
    if platform not in SOCIAL_PLATFORMS:
        raise ValueError(f"Unsupported social platform: {platform}")
    raw = url.strip()
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"https", "http"} or host not in _ALLOWED_HOSTS[platform]:
        raise ValueError(f"Expected an absolute {platform} post URL on a supported domain.")
    if parts.username or parts.password or parts.port is not None:
        raise ValueError("Post URLs cannot contain credentials or custom ports.")
    path = parts.path.rstrip("/")
    segments = [segment.casefold() for segment in path.split("/") if segment]
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=False)
              if key.casefold() not in _TRACKING_PARAMS and not key.casefold().startswith("utm_")]
    if platform == "x":
        if len(segments) != 3 or segments[1] != "status" or not segments[2].isdigit():
            raise ValueError("Provide an X/Twitter post URL like x.com/author/status/123.")
        host = "x.com"
        path = "/" + "/".join((segments[0], "status", segments[2]))
        params = []
    elif platform == "reddit":
        if host == "redd.it":
            if len(segments) != 1:
                raise ValueError("Provide a Reddit post URL, not a profile or listing.")
        elif "comments" not in segments or segments.index("comments") + 1 >= len(segments):
            raise ValueError("Provide a Reddit comments/post URL, not a subreddit listing.")
        host = "reddit.com" if host != "redd.it" else host
        params = []
    elif platform == "facebook":
        is_post = any(key in segments and segments.index(key) + 1 < len(segments)
                      for key in ("posts", "permalink")) or path in ("/permalink.php", "/story.php")
        if not is_post or (path.endswith(".php") and not any(k in {"story_fbid", "id"} for k, _ in params)):
            raise ValueError("Provide a specific public Facebook post/permalink URL, not a group listing.")
        host = "facebook.com"
    elif platform == "threads":
        if "post" not in segments or segments.index("post") + 1 >= len(segments):
            raise ValueError("Provide a Threads /post/ permalink.")
        host = "threads.com"
        params = []
    elif platform == "telegram":
        if len(segments) != 2 or segments[0] in {"c", "joinchat", "s"} or not segments[1].isdigit():
            raise ValueError("Provide a public Telegram channel post URL t.me/channel/123, not a private link.")
        host = "t.me"
        params = []
    elif platform == "discord":
        if len(segments) != 4 or segments[0] != "channels" or not all(part.isdigit() for part in segments[1:]):
            raise ValueError("Provide a Discord message URL discord.com/channels/guild/channel/message.")
        host = "discord.com"
        params = []
    return urlunsplit(("https", host, path, urlencode(sorted(params)), ""))


def build_social_opportunity(
    *, platform: str, url: str, body: str, title: str | None = None,
    author: str | None = None, created_at: str | None = None, query_key: str | None = None,
) -> Opportunity:
    """Turn text copied by the user into a reviewable lead, without any API access."""
    canonical = canonical_social_url(platform, url)
    text = body.strip()
    if len(text) < 20:
        raise ValueError("Copied post text is too short (minimum 20 characters).")
    if created_at is None or not created_at.strip():
        published = datetime.now(timezone.utc)
        assumed = True
    else:
        try:
            published = datetime.fromisoformat(created_at.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("--created-at must use ISO 8601 format.") from exc
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        published = published.astimezone(timezone.utc)
        assumed = False
    if published > datetime.now(timezone.utc):
        raise ValueError("Post publication time cannot be in the future.")
    heading = (title or "").strip() or next((line.strip() for line in text.splitlines() if line.strip()), "Social post")
    meta: dict[str, object] = {
        "platform": "manual_social", "social_platform": platform,
        "category_kind": "marketplace", "event_type": "topic",
        "manual_ingest": True, "created_at_assumed": assumed,
        "query_key": (query_key or "").strip() or f"social_manual_{platform}",
    }
    return Opportunity(
        source=platform, external_id=f"{platform}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}",
        url=canonical, title=heading[:180], body=text[:12000],
        author=(author or "").strip() or None, created_at=published, metadata=meta,
    )
