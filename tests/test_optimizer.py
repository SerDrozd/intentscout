from __future__ import annotations

from lead_radar.optimizer import (
    AdviceConfidence,
    OptimizationRecommendation,
    advise_roi,
    advise_roi_row,
)


def _row(
    *,
    source: str = "example",
    key: str = "default",
    leads: int,
    p0: int = 0,
    p1: int = 0,
    contacted: int = 0,
    replied: int = 0,
    proposal: int = 0,
    won: int = 0,
    revenue: float = 0.0,
) -> dict[str, object]:
    return {
        "source": source,
        "key": key,
        "leads": leads,
        "p0": p0,
        "p1": p1,
        "contacted": contacted,
        "replied": replied,
        "proposal": proposal,
        "won": won,
        "lost": 0,
        "ignored": 0,
        "revenue": revenue,
        "llm_cost": 0.0002,
    }


def test_small_sample_is_always_insufficient() -> None:
    advice = advise_roi_row(
        _row(leads=12, p1=4, contacted=3, replied=2, won=1, revenue=200.0),
        by_query=True,
    )
    assert advice.recommendation is OptimizationRecommendation.INSUFFICIENT_DATA
    assert advice.confidence is AdviceConfidence.LOW


def test_one_win_keeps_but_does_not_boost() -> None:
    advice = advise_roi_row(
        _row(leads=60, p0=2, p1=5, contacted=4, replied=2, proposal=1, won=1, revenue=300.0),
        by_query=True,
    )
    assert advice.recommendation is OptimizationRecommendation.KEEP
    assert advice.confidence is AdviceConfidence.MEDIUM


def test_two_wins_can_boost() -> None:
    advice = advise_roi_row(
        _row(leads=70, p0=3, p1=8, contacted=8, replied=4, proposal=3, won=2, revenue=700.0),
        by_query=False,
    )
    assert advice.recommendation is OptimizationRecommendation.BOOST
    assert advice.confidence is AdviceConfidence.HIGH


def test_large_zero_signal_query_becomes_disable_candidate() -> None:
    advice = advise_roi_row(_row(source="github", key="webhook", leads=486), by_query=True)
    assert advice.recommendation is OptimizationRecommendation.DISABLE_CANDIDATE
    assert advice.confidence is AdviceConfidence.HIGH


def test_large_zero_signal_source_deprioritizes_before_source_disable_floor() -> None:
    advice = advise_roi_row(_row(source="github", leads=500), by_query=False)
    assert advice.recommendation is OptimizationRecommendation.DEPRIORITIZE
    assert advice.confidence is AdviceConfidence.HIGH


def test_contacted_without_replies_outweighs_discovery_signal() -> None:
    advice = advise_roi_row(
        _row(leads=80, p0=4, p1=8, contacted=6, replied=0, proposal=0),
        by_query=False,
    )
    assert advice.recommendation is OptimizationRecommendation.DEPRIORITIZE
    assert "no replies" in advice.reason


def test_good_p0_p1_yield_without_feedback_does_not_auto_boost() -> None:
    advice = advise_roi_row(_row(leads=50, p0=2, p1=5), by_query=True)
    assert advice.recommendation is OptimizationRecommendation.KEEP
    assert advice.confidence is AdviceConfidence.MEDIUM


def test_advice_sort_is_stable_and_puts_boost_before_negative_recommendations() -> None:
    rows = [
        _row(source="github", leads=1600),
        _row(source="make", leads=60, p0=2, p1=6, contacted=5, replied=3, proposal=2, won=2, revenue=500.0),
        _row(source="airtable", leads=5, p1=1),
    ]
    advice = advise_roi(rows)
    assert [item.recommendation for item in advice] == [
        OptimizationRecommendation.BOOST,
        OptimizationRecommendation.INSUFFICIENT_DATA,
        OptimizationRecommendation.DISABLE_CANDIDATE,
    ]
