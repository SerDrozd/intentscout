from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence


class OptimizationRecommendation(StrEnum):
    BOOST = "BOOST"
    KEEP = "KEEP"
    WATCH = "WATCH"
    DEPRIORITIZE = "DEPRIORITIZE"
    DISABLE_CANDIDATE = "DISABLE_CANDIDATE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class AdviceConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True, slots=True)
class OptimizationAdvice:
    source: str
    key: str
    recommendation: OptimizationRecommendation
    confidence: AdviceConfidence
    leads: int
    actionable: int
    actionable_rate: float
    contacted: int
    replied: int
    proposal: int
    won: int
    lost: int
    ignored: int
    revenue: float
    llm_cost: float
    reason: str


@dataclass(frozen=True, slots=True)
class OptimizerPolicy:
    min_leads: int = 20
    medium_sample_leads: int = 50
    high_sample_leads: int = 100
    source_disable_leads: int = 1500
    query_disable_leads: int = 300
    no_reply_contacted_floor: int = 5


def _as_int(value: object | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, (str, bytes, bytearray)):
        return int(value)
    raise TypeError(f"Cannot convert {type(value).__name__} to int")


def _as_float(value: object | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (str, bytes, bytearray)):
        return float(value)
    raise TypeError(f"Cannot convert {type(value).__name__} to float")


def _confidence_for_sample(leads: int, policy: OptimizerPolicy) -> AdviceConfidence:
    if leads >= policy.high_sample_leads:
        return AdviceConfidence.HIGH
    if leads >= policy.medium_sample_leads:
        return AdviceConfidence.MEDIUM
    return AdviceConfidence.LOW


def advise_roi_row(
    row: Mapping[str, object],
    *,
    by_query: bool,
    policy: OptimizerPolicy | None = None,
) -> OptimizationAdvice:
    policy = policy or OptimizerPolicy()
    source = str(row.get("source", "unknown"))
    key = str(row.get("key", "all"))
    leads = _as_int(row.get("leads"))
    p0 = _as_int(row.get("p0"))
    p1 = _as_int(row.get("p1"))
    actionable = p0 + p1
    actionable_rate = actionable / leads if leads else 0.0
    contacted = _as_int(row.get("contacted"))
    replied = _as_int(row.get("replied"))
    proposal = _as_int(row.get("proposal"))
    won = _as_int(row.get("won"))
    lost = _as_int(row.get("lost"))
    ignored = _as_int(row.get("ignored"))
    revenue = _as_float(row.get("revenue"))
    llm_cost = _as_float(row.get("llm_cost"))

    recommendation: OptimizationRecommendation
    confidence: AdviceConfidence
    reason: str

    if leads < policy.min_leads:
        recommendation = OptimizationRecommendation.INSUFFICIENT_DATA
        confidence = AdviceConfidence.LOW
        reason = (
            f"Only {leads} observed leads; wait for at least {policy.min_leads} before changing source/query priority."
        )
    elif won >= 2:
        recommendation = OptimizationRecommendation.BOOST
        confidence = AdviceConfidence.HIGH if leads >= policy.medium_sample_leads else AdviceConfidence.MEDIUM
        reason = f"{won} recorded wins and ${revenue:.2f} revenue provide direct downstream evidence."
    elif won == 1:
        recommendation = OptimizationRecommendation.KEEP
        confidence = AdviceConfidence.MEDIUM
        reason = (
            f"One recorded win (${revenue:.2f}) is meaningful, but one outcome is not enough to auto-boost this segment."
        )
    elif replied >= 5 and proposal >= 2:
        recommendation = OptimizationRecommendation.BOOST
        confidence = AdviceConfidence.MEDIUM
        reason = f"Strong downstream engagement: {replied} replies and {proposal} proposals, even without a recorded win yet."
    elif proposal >= 1 or replied >= 1:
        recommendation = OptimizationRecommendation.KEEP
        confidence = AdviceConfidence.MEDIUM
        reason = f"Positive downstream feedback exists ({replied} replies, {proposal} proposals); keep gathering outcomes."
    elif contacted >= policy.no_reply_contacted_floor and replied == 0:
        recommendation = OptimizationRecommendation.DEPRIORITIZE
        confidence = AdviceConfidence.HIGH if contacted >= 10 else AdviceConfidence.MEDIUM
        reason = f"{contacted} contacted leads produced no replies, a stronger negative signal than discovery volume alone."
    elif actionable == 0:
        disable_floor = policy.query_disable_leads if by_query else policy.source_disable_leads
        if leads >= disable_floor:
            recommendation = OptimizationRecommendation.DISABLE_CANDIDATE
            confidence = AdviceConfidence.HIGH
            scope = "query" if by_query else "source"
            reason = (
                f"{leads} leads produced zero P0/P1 opportunities and no positive feedback; this {scope} is a dry-run "
                "disable candidate."
            )
        elif leads >= policy.high_sample_leads:
            recommendation = OptimizationRecommendation.DEPRIORITIZE
            confidence = AdviceConfidence.HIGH
            reason = f"{leads} leads produced zero P0/P1 opportunities; reduce priority unless later feedback contradicts it."
        elif leads >= policy.medium_sample_leads:
            recommendation = OptimizationRecommendation.DEPRIORITIZE
            confidence = AdviceConfidence.MEDIUM
            reason = f"{leads} leads produced zero P0/P1 opportunities; signal efficiency is weak so far."
        else:
            recommendation = OptimizationRecommendation.WATCH
            confidence = AdviceConfidence.LOW
            reason = "No P0/P1 signal yet, but the sample is too small for a strong negative recommendation."
    elif leads >= policy.high_sample_leads and actionable_rate < 0.01:
        recommendation = OptimizationRecommendation.DEPRIORITIZE
        confidence = AdviceConfidence.HIGH
        reason = (
            f"Only {actionable}/{leads} leads ({actionable_rate:.2%}) became P0/P1 and no downstream feedback exists."
        )
    elif leads >= policy.medium_sample_leads and actionable_rate < 0.02:
        recommendation = OptimizationRecommendation.DEPRIORITIZE
        confidence = AdviceConfidence.MEDIUM
        reason = (
            f"Only {actionable}/{leads} leads ({actionable_rate:.2%}) became P0/P1; keep it available but spend less attention here."
        )
    elif actionable_rate >= 0.10:
        recommendation = OptimizationRecommendation.KEEP
        confidence = _confidence_for_sample(leads, policy)
        reason = (
            f"Discovery quality is promising ({actionable}/{leads}, {actionable_rate:.2%} P0/P1), but there is not enough "
            "downstream feedback to justify BOOST."
        )
    elif actionable_rate >= 0.03:
        recommendation = OptimizationRecommendation.KEEP
        confidence = _confidence_for_sample(leads, policy)
        reason = f"P0/P1 yield is usable ({actionable}/{leads}, {actionable_rate:.2%}); keep collecting outcome feedback."
    else:
        recommendation = OptimizationRecommendation.WATCH
        confidence = _confidence_for_sample(leads, policy)
        reason = (
            f"P0/P1 yield is modest ({actionable}/{leads}, {actionable_rate:.2%}) and no downstream feedback exists yet."
        )

    return OptimizationAdvice(
        source=source,
        key=key,
        recommendation=recommendation,
        confidence=confidence,
        leads=leads,
        actionable=actionable,
        actionable_rate=actionable_rate,
        contacted=contacted,
        replied=replied,
        proposal=proposal,
        won=won,
        lost=lost,
        ignored=ignored,
        revenue=revenue,
        llm_cost=llm_cost,
        reason=reason,
    )


def advise_roi(
    rows: Sequence[Mapping[str, object]],
    *,
    by_query: bool = False,
    policy: OptimizerPolicy | None = None,
) -> list[OptimizationAdvice]:
    advice = [advise_roi_row(row, by_query=by_query, policy=policy) for row in rows]
    recommendation_rank = {
        OptimizationRecommendation.BOOST: 0,
        OptimizationRecommendation.KEEP: 1,
        OptimizationRecommendation.WATCH: 2,
        OptimizationRecommendation.INSUFFICIENT_DATA: 3,
        OptimizationRecommendation.DEPRIORITIZE: 4,
        OptimizationRecommendation.DISABLE_CANDIDATE: 5,
    }
    confidence_rank = {
        AdviceConfidence.HIGH: 0,
        AdviceConfidence.MEDIUM: 1,
        AdviceConfidence.LOW: 2,
    }
    advice.sort(
        key=lambda item: (
            recommendation_rank[item.recommendation],
            confidence_rank[item.confidence],
            -item.revenue,
            -item.won,
            -item.replied,
            -item.actionable,
            -item.leads,
            item.source,
            item.key,
        )
    )
    return advice
