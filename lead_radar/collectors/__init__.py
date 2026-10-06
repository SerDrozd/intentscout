from .discourse import (
    BubbleCommunityCollector,
    MakeCommunityCollector,
    N8nCommunityCollector,
    ShopifyCommunityCollector,
)
from .github import GitHubCollector
from .hackernews import HackerNewsCollector
from .web_community import AirtableWebCollector, HubSpotWebCollector, ZapierWebCollector

__all__ = [
    "GitHubCollector",
    "HackerNewsCollector",
    "AirtableWebCollector",
    "BubbleCommunityCollector",
    "HubSpotWebCollector",
    "MakeCommunityCollector",
    "N8nCommunityCollector",
    "ShopifyCommunityCollector",
    "ZapierWebCollector",
]
