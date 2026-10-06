from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Mapping, Sequence

from .optimizer import (
    AdviceConfidence,
    OptimizationAdvice,
    OptimizationRecommendation,
    advise_roi,
)


@dataclass(frozen=True, slots=True)
class RuntimeSourcePlan:
    source: str
    recommendation: OptimizationRecommendation
    confidence: AdviceConfidence
    interval_cycles: int
    query_multiplier: float
    reason: str


_NEUTRAL_PLAN = RuntimeSourcePlan(
    source="unknown",
    recommendation=OptimizationRecommendation.INSUFFICIENT_DATA,
    confidence=AdviceConfidence.LOW,
    interval_cycles=1,
    query_multiplier=1.0,
    reason="No adaptive history is available; collect at the normal rate.",
)

_INTERVALS: dict[OptimizationRecommendation, int] = {
    OptimizationRecommendation.BOOST: 1,
    OptimizationRecommendation.KEEP: 1,
    OptimizationRecommendation.INSUFFICIENT_DATA: 1,
    OptimizationRecommendation.WATCH: 2,
    OptimizationRecommendation.DEPRIORITIZE: 3,
    OptimizationRecommendation.DISABLE_CANDIDATE: 8,
}

_QUERY_MULTIPLIERS: dict[OptimizationRecommendation, float] = {
    OptimizationRecommendation.BOOST: 1.25,
    OptimizationRecommendation.KEEP: 1.0,
    OptimizationRecommendation.INSUFFICIENT_DATA: 1.0,
    OptimizationRecommendation.WATCH: 0.75,
    OptimizationRecommendation.DEPRIORITIZE: 0.5,
    OptimizationRecommendation.DISABLE_CANDIDATE: 0.25,
}

_QUERY_SLOTS: dict[OptimizationRecommendation, int] = {
    OptimizationRecommendation.BOOST: 5,
    OptimizationRecommendation.KEEP: 4,
    OptimizationRecommendation.INSUFFICIENT_DATA: 3,
    OptimizationRecommendation.WATCH: 2,
    OptimizationRecommendation.DEPRIORITIZE: 1,
    OptimizationRecommendation.DISABLE_CANDIDATE: 1,
}


class AdaptiveScheduler:
    """In-memory, reversible scheduling hints derived from ROI advice.

    The scheduler never changes persisted configuration and never fully disables a
    source or query. Even DISABLE_CANDIDATE keeps an exploration cadence.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        window_days: int = 30,
        refresh_cycles: int = 10,
    ) -> None:
        self.enabled = enabled
        self.window_days = max(1, window_days)
        self.refresh_cycles = max(1, refresh_cycles)
        self._source_plans: dict[str, RuntimeSourcePlan] = {}
        self._query_advice: dict[tuple[str, str], OptimizationAdvice] = {}
        self._last_refresh_cycle: int | None = None

    def needs_refresh(self, cycle_index: int) -> bool:
        if not self.enabled:
            return False
        if self._last_refresh_cycle is None:
            return True
        return cycle_index - self._last_refresh_cycle >= self.refresh_cycles

    def refresh(
        self,
        source_rows: Sequence[Mapping[str, object]],
        query_rows: Sequence[Mapping[str, object]],
        *,
        cycle_index: int,
    ) -> None:
        source_advice = advise_roi(source_rows, by_query=False)
        query_advice = advise_roi(query_rows, by_query=True)
        self._source_plans = {
            item.source: RuntimeSourcePlan(
                source=item.source,
                recommendation=item.recommendation,
                confidence=item.confidence,
                interval_cycles=_INTERVALS[item.recommendation],
                query_multiplier=_QUERY_MULTIPLIERS[item.recommendation],
                reason=item.reason,
            )
            for item in source_advice
        }
        self._query_advice = {(item.source, item.key): item for item in query_advice}
        self._last_refresh_cycle = cycle_index

    def source_plan(self, source: str) -> RuntimeSourcePlan:
        plan = self._source_plans.get(source)
        if plan is not None:
            return plan
        return RuntimeSourcePlan(
            source=source,
            recommendation=_NEUTRAL_PLAN.recommendation,
            confidence=_NEUTRAL_PLAN.confidence,
            interval_cycles=_NEUTRAL_PLAN.interval_cycles,
            query_multiplier=_NEUTRAL_PLAN.query_multiplier,
            reason=_NEUTRAL_PLAN.reason,
        )

    def should_run(self, source: str, cycle_index: int) -> bool:
        if not self.enabled:
            return True
        interval = self.source_plan(source).interval_cycles
        return cycle_index % interval == 0

    def query_limit(self, source: str, base_limit: int, available_count: int) -> int:
        if available_count <= 0:
            return 0
        base = max(1, min(base_limit, available_count))
        if not self.enabled:
            return base
        multiplier = self.source_plan(source).query_multiplier
        return max(1, min(available_count, ceil(base * multiplier)))

    def _query_slots(self, source: str, key: str) -> int:
        advice = self._query_advice.get((source, key))
        recommendation = (
            advice.recommendation if advice is not None else OptimizationRecommendation.INSUFFICIENT_DATA
        )
        return _QUERY_SLOTS[recommendation]

    def select_query_keys(
        self,
        source: str,
        available_keys: Sequence[str],
        *,
        base_limit: int,
        cycle_index: int,
    ) -> tuple[str, ...]:
        keys = tuple(dict.fromkeys(key for key in available_keys if key))
        if not keys:
            return ()
        limit = self.query_limit(source, base_limit, len(keys))
        if not self.enabled:
            return keys[:limit]

        # Interleave weighted rounds rather than grouping repeated keys. This keeps
        # exploration deterministic and prevents a BOOST query from monopolizing a cycle.
        slots = {key: self._query_slots(source, key) for key in keys}
        pool: list[str] = []
        for round_index in range(max(slots.values())):
            for key in keys:
                if slots[key] > round_index:
                    pool.append(key)

        if not pool:
            return keys[:limit]
        start = (cycle_index * max(1, limit)) % len(pool)
        selected: list[str] = []
        seen: set[str] = set()
        scanned = 0
        while len(selected) < limit and scanned < len(pool) * 2:
            key = pool[(start + scanned) % len(pool)]
            scanned += 1
            if key in seen:
                continue
            selected.append(key)
            seen.add(key)

        # Defensive fallback: every configured query keeps an exploration path.
        if len(selected) < limit:
            for key in keys:
                if key not in seen:
                    selected.append(key)
                    seen.add(key)
                    if len(selected) >= limit:
                        break
        return tuple(selected)
