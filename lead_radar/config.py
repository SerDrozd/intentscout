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
    make_enabled: bool
    make_lookback_hours: int
    make_topics_per_category: int
    n8n_enabled: bool
    n8n_lookback_hours: int
    n8n_topics_per_category: int
    discourse_concurrency: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    hubspot_enabled: bool = True
    hubspot_lookback_hours: int = 168
    hubspot_topics_per_category: int = 20
    shopify_enabled: bool = True
    shopify_lookback_hours: int = 48
    shopify_topics_per_category: int = 15
    bubble_enabled: bool = True
    bubble_lookback_hours: int = 168
    bubble_topics_per_category: int = 30
    airtable_enabled: bool = True
    airtable_lookback_hours: int = 336
    airtable_topics_per_section: int = 20
    zapier_enabled: bool = True
    zapier_lookback_hours: int = 168
    zapier_topics_per_section: int = 15
    web_fetch_concurrency: int = 4
    llm_enabled: bool = True
    openai_api_key: str | None = None
    llm_model: str = "gpt-6-luna"
    daily_llm_budget_usd: float = 0.03
    monthly_llm_budget_usd: float = 0.75
    max_llm_candidates_per_hour: int = 10
    max_llm_candidates_per_day: int = 50
    llm_batch_size: int = 5
    max_post_chars: int = 5000
    llm_max_output_tokens: int = 1200
    llm_input_price_per_mtok: float = 0.10
    llm_cached_input_price_per_mtok: float = 0.01
    llm_cache_write_price_per_mtok: float = 0.125
    llm_output_price_per_mtok: float = 0.50


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
        make_enabled=_bool("MAKE_ENABLED", True),
        make_lookback_hours=int(os.getenv("MAKE_LOOKBACK_HOURS", "48")),
        make_topics_per_category=int(os.getenv("MAKE_TOPICS_PER_CATEGORY", "20")),
        n8n_enabled=_bool("N8N_ENABLED", True),
        n8n_lookback_hours=int(os.getenv("N8N_LOOKBACK_HOURS", "48")),
        n8n_topics_per_category=int(os.getenv("N8N_TOPICS_PER_CATEGORY", "20")),
        hubspot_enabled=_bool("HUBSPOT_ENABLED", True),
        hubspot_lookback_hours=int(os.getenv("HUBSPOT_LOOKBACK_HOURS", "168")),
        hubspot_topics_per_category=int(os.getenv("HUBSPOT_TOPICS_PER_CATEGORY", "20")),
        shopify_enabled=_bool("SHOPIFY_ENABLED", True),
        shopify_lookback_hours=int(os.getenv("SHOPIFY_LOOKBACK_HOURS", "48")),
        shopify_topics_per_category=int(os.getenv("SHOPIFY_TOPICS_PER_CATEGORY", "15")),
        bubble_enabled=_bool("BUBBLE_ENABLED", True),
        bubble_lookback_hours=int(os.getenv("BUBBLE_LOOKBACK_HOURS", "168")),
        bubble_topics_per_category=int(os.getenv("BUBBLE_TOPICS_PER_CATEGORY", "30")),
        airtable_enabled=_bool("AIRTABLE_ENABLED", True),
        airtable_lookback_hours=int(os.getenv("AIRTABLE_LOOKBACK_HOURS", "336")),
        airtable_topics_per_section=int(os.getenv("AIRTABLE_TOPICS_PER_SECTION", "20")),
        zapier_enabled=_bool("ZAPIER_ENABLED", True),
        zapier_lookback_hours=int(os.getenv("ZAPIER_LOOKBACK_HOURS", "168")),
        zapier_topics_per_section=int(os.getenv("ZAPIER_TOPICS_PER_SECTION", "15")),
        web_fetch_concurrency=max(1, min(8, int(os.getenv("WEB_FETCH_CONCURRENCY", "4")))),
        discourse_concurrency=int(os.getenv("DISCOURSE_CONCURRENCY", "6")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        llm_enabled=_bool("LLM_ENABLED", True),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        llm_model=os.getenv("LLM_MODEL", "gpt-6-luna"),
        daily_llm_budget_usd=max(0.0, float(os.getenv("DAILY_LLM_BUDGET_USD", "0.03"))),
        monthly_llm_budget_usd=max(0.0, float(os.getenv("MONTHLY_LLM_BUDGET_USD", "0.75"))),
        max_llm_candidates_per_hour=max(1, int(os.getenv("MAX_LLM_CANDIDATES_PER_HOUR", "10"))),
        max_llm_candidates_per_day=max(1, int(os.getenv("MAX_LLM_CANDIDATES_PER_DAY", "50"))),
        llm_batch_size=max(1, min(10, int(os.getenv("LLM_BATCH_SIZE", "5")))),
        max_post_chars=max(500, int(os.getenv("MAX_POST_CHARS", "5000"))),
        llm_max_output_tokens=max(200, int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "1200"))),
        llm_input_price_per_mtok=max(0.0, float(os.getenv("LLM_INPUT_PRICE_PER_MTOK", "0.10"))),
        llm_cached_input_price_per_mtok=max(0.0, float(os.getenv("LLM_CACHED_INPUT_PRICE_PER_MTOK", "0.01"))),
        llm_cache_write_price_per_mtok=max(0.0, float(os.getenv("LLM_CACHE_WRITE_PRICE_PER_MTOK", "0.125"))),
        llm_output_price_per_mtok=max(0.0, float(os.getenv("LLM_OUTPUT_PRICE_PER_MTOK", "0.50"))),
    )


def load_keyword_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "matching" not in data:
        raise ValueError(f"Invalid keywords config: {path}")
    return data
