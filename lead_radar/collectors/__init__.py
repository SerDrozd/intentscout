from .discourse import (
    BubbleCommunityCollector,
    MakeCommunityCollector,
    N8nCommunityCollector,
    ShopifyCommunityCollector,
)
from .bluesky import BlueskyCollector
from .github import GitHubCollector
from .hackernews import HackerNewsCollector
from .hn_algolia import HackerNewsAlgoliaCollector
from .web_community import AirtableWebCollector, HubSpotWebCollector, ZapierWebCollector

__all__ = [
    "BlueskyCollector",
    "GitHubCollector",
    "HackerNewsAlgoliaCollector",
    "HackerNewsCollector",
    "AirtableWebCollector",
    "BubbleCommunityCollector",
    "HubSpotWebCollector",
    "MakeCommunityCollector",
    "N8nCommunityCollector",
    "ShopifyCommunityCollector",
    "ZapierWebCollector",
]
