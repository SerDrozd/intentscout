from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..models import Opportunity


SEARCH_URL = "https://api.github.com/search/issues"

# Buyer-first GitHub discovery. Broad technical terms produced thousands of
# ordinary maintainer tasks, almost none of which were paid external work.
# Deliberately require bounty, payment or contractor language in the GitHub
# SEARCH query, then retain the strict downstream directionality classifier.
BROAD_QUERIES = [
    '"paid bounty" webhook',
    '"paid bounty" API',
    'bounty integration',
    'bounty python',
    '"paid task" API',
    '"looking for a contractor" API',
    '"looking for a freelancer" integration',
    '"willing to pay" webhook',
]


class GitHubCollector:
    name = "github"

    def __init__(
        self,
        token: str | None,
        lookback_hours: int = 24,
        results_per_query: int = 50,
        queries: tuple[str, ...] | None = None,
    ) -> None:
        self.token = token
        self.lookback_hours = lookback_hours
        self.results_per_query = max(1, min(100, results_per_query))
        self.queries = queries or tuple(BROAD_QUERIES)

    async def collect(self) -> list[Opportunity]:
        since = (datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)).date().isoformat()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "intentscout/0.1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        timeout = httpx.Timeout(25.0)
        out: dict[str, Opportunity] = {}
        async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
            for broad in self.queries:
                query = f"{broad} is:issue is:open created:>={since}"
                try:
                    r = await client.get(
                        SEARCH_URL,
                        params={
                            "q": query,
                            "sort": "created",
                            "order": "desc",
                            "per_page": self.results_per_query,
                        },
                    )
                    r.raise_for_status()
                    payload = r.json()
                except (httpx.HTTPError, ValueError):
                    continue

                for item in payload.get("items", []):
                    if "pull_request" in item:
                        continue
                    external_id = str(item.get("id"))
                    if not external_id or external_id in out:
                        continue
                    created_raw = item.get("created_at")
                    if not created_raw:
                        continue
                    created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                    repo_url = (item.get("repository_url") or "").rstrip("/")
                    repo_name = repo_url.rsplit("/", 2)[-2:] if repo_url else []
                    out[external_id] = Opportunity(
                        source=self.name,
                        external_id=external_id,
                        url=item.get("html_url") or "",
                        title=item.get("title") or "GitHub issue",
                        body=item.get("body") or "",
                        author=(item.get("user") or {}).get("login"),
                        created_at=created,
                        metadata={
                            "number": item.get("number"),
                            "repository": "/".join(repo_name),
                            "comments": item.get("comments"),
                            "labels": [label.get("name") for label in item.get("labels", []) if isinstance(label, dict)],
                            "broad_query": broad,
                        },
                    )
        return list(out.values())
