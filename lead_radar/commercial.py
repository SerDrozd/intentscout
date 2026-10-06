from __future__ import annotations

import re
from datetime import datetime, timezone

from .models import (
    AlertClass,
    Analysis,
    AuthorType,
    CommercialAssessment,
    CommercialIntent,
    Opportunity,
    OpportunityClass,
)
from .scoring import PROBLEM_CATEGORIES


STRONG_PAYMENT_TERMS = (
    "willing to pay",
    "happy to pay",
    "we can pay",
    "i can pay",
    "can pay someone",
    "budget available",
    "we have a budget",
    "i have a budget",
    "need a freelancer",
    "looking for a freelancer",
    "need a contractor",
    "looking for a contractor",
    "contractor needed",
    "freelancer needed",
    "paid test",
    "paid task",
    "paid project",
    "fixed price",
)

BUYER_HELP_TERMS = (
    "need someone",
    "need somebody",
    "looking for someone",
    "looking for somebody",
    "looking for help",
    "need help",
    "can someone help",
    "can anyone help",
    "help me fix",
    "help us fix",
    "need a developer",
    "looking for a developer",
    "developer needed",
)

SELLER_TERMS = (
    "open to work",
    "open to contract work",
    "open to freelance work",
    "seeking freelance work",
    "seeking contract work",
    "seeking work",
    "available for work",
    "available for contract work",
    "available for freelance work",
    "available for hire",
    "for hire",
    "hire me",
    "my resume",
    "my résumé",
)

SERVICE_PROVIDER_TERMS = (
    "my agency",
    "our agency offers",
    "we offer development",
    "we build for clients",
    "i build for clients",
    "our services",
    "book a call",
)

SELLER_PROFILE_MARKERS = (
    "location:",
    "remote:",
    "willing to relocate:",
    "technologies:",
    "résumé/cv:",
    "resume/cv:",
    "portfolio:",
    "linkedin:",
    "email:",
)

HN_JOB_MARKERS = (
    "full-time",
    "full time",
    "part-time",
    "part time",
    "apply here",
    "apply at",
    "salary",
    "compensation",
    "staff engineer",
    "senior engineer",
    "software engineer",
    "machine learning engineer",
    "job opening",
)

LEARNER_TERMS = (
    "student project",
    "school project",
    "homework",
    "learning python",
    "beginner",
    "for practice",
    "tutorial",
    "course assignment",
)

OWNED_BUSINESS_TERMS = (
    "our company",
    "my company",
    "our business",
    "my business",
    "our startup",
    "my startup",
    "our team",
    "my team",
    "our client",
    "my client",
    "our customer",
    "our customers",
    "my customer",
    "paying customers",
    "we run",
    "we operate",
)

OWNERSHIP_TERMS = (
    "i'm",
    "i am",
    "i've",
    "i have",
    "my ",
    "we ",
    "we're",
    "we are",
    "we've",
    "we have",
    "our ",
)

AGENCY_RELATIONSHIP_TERMS = (
    "white label",
    "white-label",
    "subcontract",
    "subcontractor",
    "overflow",
    "need extra capacity",
    "extra capacity",
    "delivery partner",
    "implementation partner",
    "technical partner",
    "agency partner",
    "client project",
    "client work",
    "client delivery",
    "multiple projects",
    "ongoing projects",
    "ongoing collaboration",
    "looking for builders",
    "looking for implementers",
)

AGENCY_IDENTITY_TERMS = (
    "our agency",
    "my agency",
    "agency owner",
    "automation agency",
    "web agency",
    "product studio",
    "consultancy",
    "consulting firm",
    "consultant with clients",
)

FOUNDER_TERMS = (
    "founder",
    "co-founder",
    "cofounder",
    "i run a startup",
    "we run a startup",
    "my startup",
    "our startup",
)

URGENCY_HIGH_TERMS = (
    "urgent",
    "asap",
    "production is down",
    "prod is down",
    "customers are waiting",
    "customer is waiting",
    "launch today",
    "launch tomorrow",
    "blocking our launch",
    "blocked our launch",
    "deadline today",
    "deadline tomorrow",
)

URGENCY_MEDIUM_TERMS = (
    "deadline",
    "stopped working",
    "keeps failing",
    "production issue",
    "production bug",
    "blocked",
    "can't ship",
    "cannot ship",
    "need to finish",
    "developer disappeared",
    "previous developer left",
)

BUSINESS_IMPACT_CRITICAL = (
    "lost leads",
    "leads stopped",
    "payments failing",
    "payment failing",
    "checkout broken",
    "orders failing",
    "orders stopped",
    "customers are waiting",
    "customer is waiting",
    "production is down",
    "client delivery blocked",
)

BUSINESS_IMPACT_HIGH = (
    "sales",
    "revenue",
    "leads",
    "crm",
    "orders",
    "inventory",
    "invoice",
    "invoices",
    "payments",
    "checkout",
    "shipping",
    "appointments",
    "reporting deadline",
    "launch",
    "demo",
    "client delivery",
    "support",
    "production",
)

BUSINESS_IMPACT_MEDIUM = (
    "manual work",
    "data entry",
    "takes hours",
    "every week",
    "every day",
    "repetitive",
    "not syncing",
    "sync issue",
    "workflow failing",
    "integration stopped",
    "customer",
    "client",
)

BOUNDED_TERMS = (
    "webhook",
    "one endpoint",
    "endpoint",
    "api integration",
    "oauth",
    "data sync",
    "csv",
    "excel",
    "spreadsheet",
    "pdf",
    "document parser",
    "parser",
    "scraper",
    "fastapi",
    "import",
    "export",
    "background job",
    "scheduled job",
    "cron job",
    "retry",
    "idempotency",
    "deduplication",
)

SMALL_SCOPE_TERMS = (
    "one ",
    "single ",
    "small ",
    "quick fix",
    "quick backend fix",
    "one-off",
    "one off",
)

LARGE_SCOPE_TERMS = (
    "entire platform",
    "complete platform",
    "whole platform",
    "entire app",
    "complete app",
    "whole backend",
    "rewrite everything",
    "from scratch",
    "full rebuild",
    "complete system",
)

HIGH_VALUE_TERMS = (
    "internal tool",
    "automation system",
    "backend for",
    "document processing system",
    "multiple integrations",
    "several integrations",
    "production backend",
    "existing saas",
    "data pipeline",
    "workflow system",
    "platform",
    "multiple clients",
    "ongoing",
)

CURRENCY_PATTERN = re.compile(
    r"(?:budget|pay|paying|offering|price|fixed|bounty|reward).{0,50}(?:\$|€|£)\s?\d+",
    re.IGNORECASE,
)


def _pattern(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    left = r"(?<![\w])" if term and term[0].isalnum() else ""
    right = r"(?![\w])" if term and term[-1].isalnum() else ""
    return re.compile(left + escaped + right, re.IGNORECASE)


def _has(text: str, term: str) -> bool:
    return bool(_pattern(term).search(text))


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(_has(text, term) for term in terms)


def _count(text: str, terms: tuple[str, ...]) -> int:
    return sum(1 for term in terms if _has(text, term))


def _categories(analysis: Analysis) -> set[str]:
    return {match.category for match in analysis.phrase_matches}


def _match_types_for_problem(analysis: Analysis) -> set[str]:
    return {
        match.match_type
        for match in analysis.phrase_matches
        if match.category in PROBLEM_CATEGORIES
    }


def freshness_score(opportunity: Opportunity, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    created = opportunity.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_minutes = max(0.0, (now - created).total_seconds() / 60)
    if age_minutes <= 10:
        return 100
    if age_minutes <= 30:
        return 92
    if age_minutes <= 60:
        return 82
    if age_minutes <= 180:
        return 68
    if age_minutes <= 720:
        return 48
    if age_minutes <= 1440:
        return 32
    if age_minutes <= 4320:
        return 14
    return 0


def competition_score(opportunity: Opportunity) -> int:
    raw = opportunity.metadata.get("reply_count")
    if raw is None:
        raw = opportunity.metadata.get("comments")
    try:
        replies = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        replies = None
    if replies is None:
        return 65
    if replies <= 0:
        return 100
    if replies <= 2:
        return 90
    if replies <= 5:
        return 76
    if replies <= 10:
        return 58
    if replies <= 25:
        return 38
    if replies <= 50:
        return 22
    return 10


def _seller_like(opportunity: Opportunity, text: str) -> bool:
    if _has_any(text, SELLER_TERMS):
        return True
    profile_hits = sum(1 for marker in SELLER_PROFILE_MARKERS if marker in text.casefold())
    return opportunity.source == "hackernews" and profile_hits >= 3


def _hn_job_like(opportunity: Opportunity, text: str) -> bool:
    if opportunity.source != "hackernews":
        return False
    if opportunity.metadata.get("type") == "job":
        return True
    return _count(text, HN_JOB_MARKERS) >= 2


def _strong_payment(text: str) -> bool:
    return _has_any(text, STRONG_PAYMENT_TERMS) or bool(CURRENCY_PATTERN.search(text))


def _explicit_help(text: str, analysis: Analysis) -> bool:
    categories = _categories(analysis)
    return "explicit_help_hire" in categories or _has_any(text, BUYER_HELP_TERMS)


def _owned_signal(text: str) -> bool:
    return _has_any(text, OWNERSHIP_TERMS)


def _owned_business(text: str) -> bool:
    return _has_any(text, OWNED_BUSINESS_TERMS)


def _agency_signal(text: str) -> bool:
    return _has_any(text, AGENCY_RELATIONSHIP_TERMS) or _has_any(text, AGENCY_IDENTITY_TERMS)


def classify_author(opportunity: Opportunity, analysis: Analysis) -> AuthorType:
    text = opportunity.text.casefold()
    categories = _categories(analysis)
    strong_payment = _strong_payment(text)
    explicit_help = _explicit_help(text, analysis)
    owned_business = _owned_business(text)
    owned_problem = bool(categories & {"buyer_owned_problem", "buyer_business_problem", "buyer_manual_pain"})

    if _seller_like(opportunity, text):
        return AuthorType.JOB_SEEKER
    if _has_any(text, SERVICE_PROVIDER_TERMS):
        return AuthorType.SERVICE_PROVIDER
    if _hn_job_like(opportunity, text):
        return AuthorType.RECRUITER
    if _has_any(text, LEARNER_TERMS) and not owned_business:
        return AuthorType.LEARNER
    if _agency_signal(text) and (strong_payment or explicit_help or owned_problem):
        return AuthorType.AGENCY_BUYER
    if strong_payment and owned_business:
        return AuthorType.BUYER
    if strong_payment or explicit_help:
        return AuthorType.TECHNICAL_BUYER
    if owned_business and owned_problem:
        return AuthorType.POTENTIAL_BUYER
    if opportunity.source == "github":
        return AuthorType.OSS_MAINTAINER
    if owned_problem or (owned_business and bool(categories & PROBLEM_CATEGORIES)):
        return AuthorType.POTENTIAL_BUYER
    return AuthorType.UNKNOWN


def classify_commercial_intent(opportunity: Opportunity, analysis: Analysis, author_type: AuthorType) -> CommercialIntent:
    text = opportunity.text.casefold()
    categories = _categories(analysis)
    if author_type in {AuthorType.JOB_SEEKER, AuthorType.SERVICE_PROVIDER, AuthorType.LEARNER, AuthorType.RECRUITER}:
        return CommercialIntent.NONE
    if _strong_payment(text):
        return CommercialIntent.EXPLICIT
    if _explicit_help(text, analysis) or _agency_signal(text):
        return CommercialIntent.IMPLICIT
    if categories & {"buyer_build_request", "buyer_manual_pain", "buyer_business_problem"}:
        return CommercialIntent.IMPLICIT
    if categories & {"buyer_owned_problem", "buyer_help_question"} or _owned_business(text):
        return CommercialIntent.WEAK
    return CommercialIntent.NONE


def _technical_fit(analysis: Analysis) -> int:
    match_types = _match_types_for_problem(analysis)
    if "phrase" in match_types:
        return 94
    if "grammar" in match_types:
        return 82
    if "rule" in match_types:
        return 72
    categories = _categories(analysis)
    if categories & PROBLEM_CATEGORIES:
        return 60
    return 15


def _business_impact(text: str, owned_business: bool) -> int:
    if _has_any(text, BUSINESS_IMPACT_CRITICAL):
        return 100
    high = _count(text, BUSINESS_IMPACT_HIGH)
    medium = _count(text, BUSINESS_IMPACT_MEDIUM)
    if high >= 2:
        return 88
    if high == 1:
        return 78
    if medium >= 2:
        return 66
    if medium == 1:
        return 55
    if owned_business:
        return 46
    return 24


def _urgency(text: str, author_type: AuthorType) -> int:
    if _has_any(text, URGENCY_HIGH_TERMS):
        return 100
    if _has_any(text, URGENCY_MEDIUM_TERMS):
        return 76
    if _has_any(text, BUYER_HELP_TERMS) and author_type in {
        AuthorType.BUYER,
        AuthorType.POTENTIAL_BUYER,
        AuthorType.AGENCY_BUYER,
        AuthorType.TECHNICAL_BUYER,
    }:
        return 52
    return 20


def _bounded_scope(text: str, analysis: Analysis) -> int:
    categories = _categories(analysis)
    score = 35
    if _has_any(text, BOUNDED_TERMS):
        score = 72
    if _has_any(text, SMALL_SCOPE_TERMS):
        score += 14
    if "buyer_manual_pain" in categories:
        score = max(score, 74)
    if _has_any(text, LARGE_SCOPE_TERMS):
        score -= 34
    return max(0, min(100, score))


def _payment_readiness(text: str, intent: CommercialIntent, author_type: AuthorType) -> int:
    if _strong_payment(text):
        return 100
    if intent == CommercialIntent.EXPLICIT:
        return 92
    if intent == CommercialIntent.IMPLICIT:
        return 68
    if intent == CommercialIntent.WEAK and author_type in {
        AuthorType.BUYER,
        AuthorType.POTENTIAL_BUYER,
        AuthorType.AGENCY_BUYER,
        AuthorType.TECHNICAL_BUYER,
    }:
        return 46
    return 12


def _decision_maker_probability(text: str, author_type: AuthorType) -> int:
    if author_type == AuthorType.AGENCY_BUYER:
        return 88
    if _has_any(text, FOUNDER_TERMS):
        return 92
    if author_type == AuthorType.BUYER:
        return 82
    if author_type == AuthorType.POTENTIAL_BUYER:
        return 70
    if author_type == AuthorType.TECHNICAL_BUYER:
        return 58
    if author_type == AuthorType.RECRUITER:
        return 48
    return 28


def _contactability(opportunity: Opportunity) -> int:
    base = 72 if opportunity.source == "hackernews" else 60 if opportunity.source == "github" else 65
    if opportunity.author:
        base += 8
    return min(100, base)


def _relationship_signal(text: str, author_type: AuthorType) -> int:
    score = 18
    if author_type == AuthorType.AGENCY_BUYER:
        score = 92
    elif _has_any(text, AGENCY_RELATIONSHIP_TERMS):
        score = 82
    elif _has_any(text, AGENCY_IDENTITY_TERMS):
        score = 76
    if _has_any(text, ("ongoing projects", "multiple projects", "ongoing collaboration", "client work", "client project")):
        score = max(score, 86)
    return score


def _repeat_potential(text: str, relationship_signal: int) -> int:
    if relationship_signal >= 85:
        return 92
    if _has_any(text, ("crm", "automation", "data sync", "support", "maintenance", "monitoring")):
        return 68
    return 34


def _deal_value_proxy(text: str, impact: int, bounded: int, relationship_signal: int) -> int:
    score = 36
    if _has_any(text, HIGH_VALUE_TERMS):
        score = 78
    if impact >= 80:
        score = max(score, 72)
    if relationship_signal >= 80:
        score = max(score, 70)
    if bounded >= 80 and not _has_any(text, HIGH_VALUE_TERMS):
        score = min(score, 52)
    return score


def _buyer_quality(author_type: AuthorType, decision_maker: int, impact: int) -> int:
    type_score = {
        AuthorType.AGENCY_BUYER: 92,
        AuthorType.BUYER: 88,
        AuthorType.POTENTIAL_BUYER: 72,
        AuthorType.TECHNICAL_BUYER: 68,
        AuthorType.RECRUITER: 46,
        AuthorType.OSS_MAINTAINER: 30,
        AuthorType.UNKNOWN: 38,
        AuthorType.JOB_SEEKER: 5,
        AuthorType.SERVICE_PROVIDER: 8,
        AuthorType.LEARNER: 12,
        AuthorType.SPAM: 0,
    }[author_type]
    return round((type_score * 0.55) + (decision_maker * 0.25) + (impact * 0.20))


def _close_probability(payment: int, buyer_quality: int, bounded: int, competition: int, contactability: int) -> int:
    return round(
        payment * 0.32
        + buyer_quality * 0.22
        + bounded * 0.18
        + competition * 0.14
        + contactability * 0.14
    )


def _weighted(parts: tuple[tuple[int, float], ...]) -> int:
    return max(0, min(100, round(sum(value * weight for value, weight in parts))))


def assess_commercial(
    opportunity: Opportunity,
    analysis: Analysis,
    now: datetime | None = None,
    *,
    cash_threshold: int = 72,
    value_threshold: int = 74,
    relationship_threshold: int = 76,
) -> CommercialAssessment:
    text = opportunity.text.casefold()
    author_type = classify_author(opportunity, analysis)
    intent = classify_commercial_intent(opportunity, analysis, author_type)
    freshness = freshness_score(opportunity, now)
    competition = competition_score(opportunity)
    tech_fit = _technical_fit(analysis)
    owned_business = _owned_business(text)
    impact = _business_impact(text, owned_business)
    urgency = _urgency(text, author_type)
    bounded = _bounded_scope(text, analysis)
    payment = _payment_readiness(text, intent, author_type)
    decision_maker = _decision_maker_probability(text, author_type)
    contactability = _contactability(opportunity)
    relationship_signal = _relationship_signal(text, author_type)
    repeat = _repeat_potential(text, relationship_signal)
    deal_value = _deal_value_proxy(text, impact, bounded, relationship_signal)
    buyer_quality = _buyer_quality(author_type, decision_maker, impact)
    close_probability = _close_probability(payment, buyer_quality, bounded, competition, contactability)
    expansion = max(repeat, relationship_signal, min(100, deal_value + 8))
    clarity = max(20, min(100, bounded + (14 if _explicit_help(text, analysis) else 0)))

    cash = _weighted(
        (
            (payment, 0.18),
            (urgency, 0.13),
            (bounded, 0.12),
            (freshness, 0.15),
            (competition, 0.08),
            (tech_fit, 0.12),
            (impact, 0.10),
            (decision_maker, 0.06),
            (contactability, 0.04),
            (buyer_quality, 0.02),
        )
    )
    value = _weighted(
        (
            (deal_value, 0.20),
            (tech_fit, 0.16),
            (close_probability, 0.14),
            (buyer_quality, 0.12),
            (clarity, 0.10),
            (repeat, 0.10),
            (expansion, 0.08),
            (impact, 0.06),
            (payment, 0.04),
        )
    )
    relationship = _weighted(
        (
            (relationship_signal, 0.30),
            (repeat, 0.20),
            (buyer_quality, 0.12),
            (tech_fit, 0.10),
            (payment, 0.10),
            (contactability, 0.08),
            (expansion, 0.07),
            (freshness, 0.03),
        )
    )

    rejected_types = {
        AuthorType.JOB_SEEKER,
        AuthorType.SERVICE_PROVIDER,
        AuthorType.LEARNER,
        AuthorType.SPAM,
        AuthorType.RECRUITER,
    }
    if author_type in rejected_types:
        cash = min(cash, 12)
        value = min(value, 24)
        relationship = min(relationship, 12)
    if author_type == AuthorType.OSS_MAINTAINER and intent == CommercialIntent.NONE:
        cash = min(cash, 18)
        value = min(value, 30)
        relationship = min(relationship, 16)

    buyer_types = {
        AuthorType.BUYER,
        AuthorType.POTENTIAL_BUYER,
        AuthorType.AGENCY_BUYER,
        AuthorType.TECHNICAL_BUYER,
    }
    can_interrupt = author_type in buyer_types and intent in {CommercialIntent.IMPLICIT, CommercialIntent.EXPLICIT}
    high_value_scope = _has_any(text, HIGH_VALUE_TERMS)

    if can_interrupt and relationship >= relationship_threshold:
        alert_class = AlertClass.P0_RELATIONSHIP
        opportunity_class = OpportunityClass.RELATIONSHIP
    elif can_interrupt and high_value_scope and value >= value_threshold:
        alert_class = AlertClass.P0_HIGH_VALUE
        opportunity_class = OpportunityClass.HIGH_VALUE
    elif can_interrupt and cash >= cash_threshold:
        alert_class = AlertClass.P0_CASH
        opportunity_class = OpportunityClass.CASH_NOW
    elif can_interrupt and value >= value_threshold:
        alert_class = AlertClass.P0_HIGH_VALUE
        opportunity_class = OpportunityClass.HIGH_VALUE
    elif author_type in rejected_types:
        alert_class = AlertClass.REJECT
        opportunity_class = OpportunityClass.REJECT
    elif max(cash, value, relationship) >= 58 and author_type in buyer_types:
        alert_class = AlertClass.P1
        if relationship >= max(cash, value):
            opportunity_class = OpportunityClass.RELATIONSHIP
        elif value >= cash:
            opportunity_class = OpportunityClass.HIGH_VALUE
        else:
            opportunity_class = OpportunityClass.CASH_NOW
    else:
        alert_class = AlertClass.P2
        opportunity_class = OpportunityClass.STORE_ONLY

    reasons: list[str] = []
    if intent == CommercialIntent.EXPLICIT:
        reasons.append("explicit commercial intent")
    elif intent == CommercialIntent.IMPLICIT:
        reasons.append("implicit buyer intent")
    if freshness >= 82:
        reasons.append("fresh signal")
    if competition >= 90:
        reasons.append("low visible competition")
    if impact >= 78:
        reasons.append("strong business impact")
    if bounded >= 72:
        reasons.append("bounded technical scope")
    if relationship_signal >= 80:
        reasons.append("recurring/agency potential")

    features: dict[str, int | float | str | bool] = {
        "technical_fit": tech_fit,
        "business_impact": impact,
        "urgency": urgency,
        "bounded_scope": bounded,
        "payment_readiness": payment,
        "decision_maker_probability": decision_maker,
        "contactability": contactability,
        "relationship_signal": relationship_signal,
        "repeat_potential": repeat,
        "deal_value_proxy": deal_value,
        "buyer_quality": buyer_quality,
        "close_probability": close_probability,
        "expansion_potential": expansion,
        "project_clarity": clarity,
        "owned_business_context": owned_business,
    }

    return CommercialAssessment(
        author_type=author_type,
        commercial_intent=intent,
        opportunity_class=opportunity_class,
        alert_class=alert_class,
        cash_score=cash,
        value_score=value,
        relationship_score=relationship,
        freshness_score=freshness,
        competition_score=competition,
        features=features,
        reasons=reasons,
    )
