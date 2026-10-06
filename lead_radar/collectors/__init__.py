from .discourse import MakeCommunityCollector, N8nCommunityCollector
from .github import GitHubCollector
from .hackernews import HackerNewsCollector

__all__ = [
    "GitHubCollector",
    "HackerNewsCollector",
    "MakeCommunityCollector",
    "N8nCommunityCollector",
]
