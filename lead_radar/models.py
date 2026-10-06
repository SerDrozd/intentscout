from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class AuthorType(StrEnum):
    BUYER = "BUYER"
    POTENTIAL_BUYER = "POTENTIAL_BUYER"
    AGENCY_BUYER = "AGENCY_BUYER"
    TECHNICAL_BUYER = "TECHNICAL_BUYER"
    RECRUITER = "RECRUITER"
    JOB_SEEKER = "JOB_SEEKER"
    SERVICE_PROVIDER = "SERVICE_PROVIDER"
    LEARNER = "LEARNER"
    OSS_MAINTAINER = "OSS_MAINTAINER"
    SPAM = "SPAM"
    UNKNOWN = "UNKNOWN"


class CommercialIntent(StrEnum):
    NONE = "NONE"
    WEAK = "WEAK"
    IMPLICIT = "IMPLICIT"
    EXPLICIT = "EXPLICIT"


class OpportunityClass(StrEnum):
    CASH_NOW = "CASH_NOW"
    HIGH_VALUE = "HIGH_VALUE"
    RELATIONSHIP = "RELATIONSHIP"
    STORE_ONLY = "STORE_ONLY"
    REJECT = "REJECT"


class AlertClass(StrEnum):
    P0_CASH = "P0_CASH"
    P0_HIGH_VALUE = "P0_HIGH_VALUE"
    P0_RELATIONSHIP = "P0_RELATIONSHIP"
    P1 = "P1"
    P2 = "P2"
    REJECT = "REJECT"


@dataclass(slots=True)
class Opportunity:
    source: str
    external_id: str
    url: str
    title: str
    body: str
    author: str | None
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.title}\n{self.body}".strip()


@dataclass(slots=True)
class PhraseMatch:
    category: str
    phrase: str
    match_type: str = "phrase"


@dataclass(slots=True)
class Analysis:
    phrase_matches: list[PhraseMatch]
    hard_excludes: list[str]
    soft_penalties: list[str]
    positive_context: list[str]

    @property
    def excluded(self) -> bool:
        return bool(self.hard_excludes)


@dataclass(slots=True)
class CommercialAssessment:
    author_type: AuthorType
    commercial_intent: CommercialIntent
    opportunity_class: OpportunityClass
    alert_class: AlertClass
    cash_score: int
    value_score: int
    relationship_score: int
    freshness_score: int
    competition_score: int
    features: dict[str, int | float | str | bool]
    reasons: list[str] = field(default_factory=list)

    @property
    def p0(self) -> bool:
        return self.alert_class in {
            AlertClass.P0_CASH,
            AlertClass.P0_HIGH_VALUE,
            AlertClass.P0_RELATIONSHIP,
        }

    @property
    def priority_score(self) -> int:
        return max(self.cash_score, self.value_score, self.relationship_score)


@dataclass(slots=True)
class ScoredOpportunity:
    opportunity: Opportunity
    analysis: Analysis
    score: int
    breakdown: dict[str, int]
    assessment: CommercialAssessment | None = None
    discovered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
