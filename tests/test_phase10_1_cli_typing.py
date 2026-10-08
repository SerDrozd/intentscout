"""Exercise the CLI branches whose result containers must have distinct types."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from lead_radar import __main__ as cli
from lead_radar.app import ManualIngestResult


class _FakeDB:
    def rewind_marketplace_cursors(self, sources: tuple[str, ...]) -> dict[str, int]:
        assert sources == ("make", "n8n")
        return {"make": 3, "n8n": 4}


class _FakeRadar:
    def __init__(self, settings: object) -> None:
        self.db = _FakeDB()
        self.manual_status = "duplicate"

    def buyer_board(self, *, days: int, limit: int) -> list[tuple[str, dict[str, object]]]:
        assert (days, limit) == (3, 5)
        return [("DIRECT HIRING (payment unverified)", {
            "id": 12, "source": "make", "created_at": datetime.now(timezone.utc).isoformat(),
            "cash_score": 58, "llm_decision": "NOT_RUN", "title": "Paid API contractor",
            "url": "https://example.org/t/12",
        })]

    def adaptive_schedule_rows(self, *, days: int | None) -> list[dict[str, object]]:
        assert days == 7
        return [{
            "source": "n8n", "recommendation": "KEEP", "confidence": "HIGH",
            "interval_cycles": 1, "query_limit": None, "protected": True,
            "adaptive_interval_cycles": 3, "reason": "Buyer-first polling",
        }]

    def ingest_manual(self, opportunity: object) -> ManualIngestResult:
        return ManualIngestResult(status=self.manual_status)


@pytest.mark.asyncio
async def test_buyer_rescan_cli_formats_mapping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "RadarApp", _FakeRadar)
    args = cli.build_parser().parse_args([
        "buyer-rescan", "--source", "make", "--source", "n8n", "--yes",
    ])
    assert await cli.async_main(args) == 0
    assert "'make': 3" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_buyer_board_cli_formats_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "RadarApp", _FakeRadar)
    args = cli.build_parser().parse_args(["buyer-board", "--days", "3", "--limit", "5"])
    assert await cli.async_main(args) == 0
    output = capsys.readouterr().out
    assert "#12 [DIRECT HIRING (payment unverified)]" in output
    assert "Paid API contractor" in output


@pytest.mark.asyncio
async def test_schedule_plan_cli_formats_dict_rows(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(
        log_level="INFO", adaptive_window_days=30, adaptive_scheduling_enabled=True,
    ))
    monkeypatch.setattr(cli, "RadarApp", _FakeRadar)
    args = cli.build_parser().parse_args(["schedule-plan", "--days", "7"])
    assert await cli.async_main(args) == 0
    output = capsys.readouterr().out
    assert "n8n recommendation=KEEP" in output
    assert "protected=true adaptive_poll=3" in output


@pytest.mark.asyncio
async def test_manual_ingest_cli_keeps_result_type(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(cli, "RadarApp", _FakeRadar)
    monkeypatch.setattr(cli, "build_linkedin_opportunity", lambda **kwargs: SimpleNamespace(url=kwargs["url"]))
    args = cli.build_parser().parse_args([
        "linkedin-add", "--url", "https://linkedin.com/posts/example", "--text", "Paid API developer needed",
    ])
    assert await cli.async_main(args) == 0
    assert "already in the radar database" in capsys.readouterr().out
