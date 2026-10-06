from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass(slots=True)
class Settings:
    db_path: Path
    keywords_path: Path
    poll_interval_seconds: int
    log_level: str
    alert_threshold: int
    review_threshold: int
    alert_review_queue: bool
    cash_alert_threshold: int
    value_alert_threshold: int
    relationship_alert_threshold: int
    max_alerts_per_cycle: int
    telegram_min_send_interval_seconds: float
    hn_enabled: bool
    hn_bootstrap_items: int
    hn_max_items_per_cycle: int
    hn_concurrency: int
    github_enabled: bool
    github_token: str | None
    github_lookback_hours: int
    github_results_per_query: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(base_dir: Path | None = None) -> Settings:
    base_dir = (base_dir or Path.cwd()).resolve()
    load_dotenv(base_dir / ".env")

    def relpath(name: str, default: str) -> Path:
        raw = Path(os.getenv(name, default))
        return raw if raw.is_absolute() else (base_dir / raw).resolve()

    return Settings(
        db_path=relpath("RADAR_DB", "./data/radar.db"),
        keywords_path=relpath("KEYWORDS_CONFIG", "./config/keywords.yaml"),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "180")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        alert_threshold=int(os.getenv("ALERT_THRESHOLD", "13")),
        review_threshold=int(os.getenv("REVIEW_THRESHOLD", "8")),
        alert_review_queue=_bool("ALERT_REVIEW_QUEUE", False),
        cash_alert_threshold=int(os.getenv("CASH_ALERT_THRESHOLD", "72")),
        value_alert_threshold=int(os.getenv("VALUE_ALERT_THRESHOLD", "74")),
        relationship_alert_threshold=int(os.getenv("RELATIONSHIP_ALERT_THRESHOLD", "76")),
        max_alerts_per_cycle=max(1, int(os.getenv("MAX_ALERTS_PER_CYCLE", "5"))),
        telegram_min_send_interval_seconds=max(1.05, float(os.getenv("TELEGRAM_MIN_SEND_INTERVAL_SECONDS", "1.15"))),
        hn_enabled=_bool("HN_ENABLED", True),
        hn_bootstrap_items=int(os.getenv("HN_BOOTSTRAP_ITEMS", "300")),
        hn_max_items_per_cycle=int(os.getenv("HN_MAX_ITEMS_PER_CYCLE", "500")),
        hn_concurrency=int(os.getenv("HN_CONCURRENCY", "20")),
        github_enabled=_bool("GITHUB_ENABLED", True),
        github_token=os.getenv("GITHUB_TOKEN") or None,
        github_lookback_hours=int(os.getenv("GITHUB_LOOKBACK_HOURS", "24")),
        github_results_per_query=int(os.getenv("GITHUB_RESULTS_PER_QUERY", "50")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
    )


def load_keyword_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "matching" not in data:
        raise ValueError(f"Invalid keywords config: {path}")
    return data
