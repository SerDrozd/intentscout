"""Social plan and LLM classification use separate result types in the CLI."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from lead_radar import __main__ as cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_social_plan_cli_displays_alert_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = SimpleNamespace(
        log_level="INFO",
        google_alerts_queries_path=ROOT / "config/google_alerts_queries.yaml",
        rss_feeds_path=ROOT / "config/feeds.yaml",
        rss_local_feeds_path=tmp_path / "feeds.local.yaml",
    )
    monkeypatch.setattr(cli, "load_settings", lambda _: settings)
    monkeypatch.setattr(cli, "RadarApp", lambda _: object())
    assert await cli.async_main(cli.build_parser().parse_args(["social-plan"])) == 0
    output = capsys.readouterr().out
    assert "social_reddit_api_hiring status=UNCONFIGURED" in output
    assert "social_x_python_contract status=UNCONFIGURED" in output


@pytest.mark.asyncio
async def test_llm_classify_cli_displays_classification_not_alert_row(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeRadar:
        def __init__(self, _: object) -> None:
            self.db = SimpleNamespace(llm_usage_summary=lambda: {"llm_requests_today": 1})

        async def classify_shortlist(
            self, limit: int, sources: tuple[str, ...] | None, *, force: bool
        ) -> list[SimpleNamespace]:
            assert limit == 4 and sources == ("n8n",) and force is False
            return [SimpleNamespace(
                opportunity_id=42,
                decision=SimpleNamespace(value="REJECT"),
                confidence=0.9,
                rejection_reason="This is a seller, not a buyer",
                problem_summary="",
                author_type=SimpleNamespace(value="SELLER"),
                commercial_intent=SimpleNamespace(value="NONE"),
                should_contact=False,
                technical_fit=False,
                payment_intent="NONE",
            )]

    monkeypatch.setattr(cli, "load_settings", lambda _: SimpleNamespace(
        log_level="INFO", llm_enabled=True, openai_api_key="test-only-value",
    ))
    monkeypatch.setattr(cli, "RadarApp", FakeRadar)
    args = cli.build_parser().parse_args(["llm-classify", "--limit", "4", "--source", "n8n"])
    assert await cli.async_main(args) == 0
    output = capsys.readouterr().out
    assert "#42 REJECT confidence=0.9" in output
    assert "This is a seller, not a buyer" in output
    assert "llm_requests_today" in output
