from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .app import RadarApp
from .config import load_settings
from .llm import LLMAPIError, LLMBudgetExceeded


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Technical/API Problem Radar MVP")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("once", help="Run one collection cycle")
    sub.add_parser("run", help="Run continuously")
    recent = sub.add_parser("recent", help="Show recent matched opportunities")
    recent.add_argument("--limit", type=int, default=20)
    sub.add_parser("stats", help="Show SQLite stats")
    shortlist = sub.add_parser("shortlist", help="Show highest-priority actionable P0/P1 opportunities")
    shortlist.add_argument("--limit", type=int, default=20)
    shortlist.add_argument("--source", action="append", dest="sources", help="Limit to a source; repeatable")
    llm_classify = sub.add_parser("llm-classify", help="Classify current P0/P1 shortlist with the cheap LLM gate")
    llm_classify.add_argument("--limit", type=int, default=20)
    llm_classify.add_argument("--source", action="append", dest="sources", help="Limit to a source; repeatable")
    llm_classify.add_argument("--force", action="store_true", help="Reclassify rows that already have an LLM decision")
    sub.add_parser("llm-stats", help="Show LLM token/cost budget usage")
    prepare = sub.add_parser("prepare", help="Generate or show an action brief and outreach draft for one lead")
    prepare.add_argument("--id", type=int, required=True, dest="opportunity_id")
    action = sub.add_parser("lead-action", help="Record outreach/reply/proposal/win/loss feedback for one lead")
    action.add_argument("--id", type=int, required=True, dest="opportunity_id")
    action.add_argument(
        "--action",
        required=True,
        choices=("contacted", "replied", "proposal", "won", "lost", "ignored", "new"),
    )
    action.add_argument("--revenue", type=float, default=None, help="Revenue in USD; normally used with --action won")
    action.add_argument("--note", default=None, help="Optional short note stored in the action history")
    roi = sub.add_parser("roi", help="Show source/query funnel and revenue performance")
    roi.add_argument("--days", type=int, default=30)
    roi.add_argument("--by", choices=("source", "query"), default="source")
    roi.add_argument("--source", action="append", dest="sources", help="Limit to a source; repeatable")
    reclassify = sub.add_parser("reclassify", help="Recompute commercial scores without sending alerts")
    reclassify.add_argument("--source", action="append", dest="sources", help="Limit to a source; repeatable")
    sub.add_parser("telegram-id", help="Show chat IDs seen by the configured Telegram bot")
    return p


def _age_label(created_at: str) -> str:
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return "?"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def async_main(args: argparse.Namespace) -> int:
    settings = load_settings(Path.cwd())
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs complete request URLs. Telegram Bot API URLs contain the bot
    # token, so never emit httpx/httpcore request logs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app = RadarApp(settings)

    if args.command == "once":
        stats = await app.run_cycle()
        print(
            f"fetched={stats.fetched} matched={stats.matched} inserted={stats.inserted} "
            f"alerted={stats.alerted} excluded={stats.excluded} "
            f"llm_classified={stats.llm_classified} llm_rejected={stats.llm_rejected}"
        )
        return 0
    if args.command == "run":
        await app.run_forever()
        return 0
    if args.command == "stats":
        print(app.db.stats())
        return 0
    if args.command == "lead-action":
        try:
            app.db.set_status(
                args.opportunity_id,
                args.action,
                reason=args.note or "cli",
                revenue=args.revenue,
            )
        except ValueError as exc:
            print(str(exc))
            return 2
        suffix = f" revenue=${args.revenue:.2f}" if args.revenue is not None else ""
        print(f"#{args.opportunity_id} -> {args.action}{suffix}")
        return 0
    if args.command == "roi":
        sources = tuple(args.sources) if args.sources else None
        rows = app.db.roi_report(days=args.days, by_query=args.by == "query", sources=sources)
        if not rows:
            print("No ROI data in the selected window.")
            return 0
        print(f"ROI window: {args.days}d | by={args.by}")
        for row in rows:
            label = str(row["source"]) if args.by == "source" else f"{row['source']}:{row['key']}"
            fetched = row.get("fetched")
            fetch_part = f" fetched={fetched}" if fetched is not None else ""
            print(
                f"{label}{fetch_part} leads={row['leads']} p0={row['p0']} p1={row['p1']} "
                f"contacted={row['contacted']} replied={row['replied']} proposal={row['proposal']} "
                f"won={row['won']} lost={row['lost']} ignored={row['ignored']} "
                f"revenue=${float(row['revenue']):.2f} llm=${float(row['llm_cost']):.6f}"
            )
        return 0
    if args.command == "llm-stats":
        usage: dict[str, object] = dict(app.db.llm_usage_summary())
        usage["daily_budget_usd"] = settings.daily_llm_budget_usd
        usage["monthly_budget_usd"] = settings.monthly_llm_budget_usd
        usage["model"] = settings.llm_model
        print(usage)
        return 0
    if args.command == "prepare":
        if not settings.llm_enabled:
            print("LLM generation is disabled by LLM_ENABLED=false.")
            return 2
        if not settings.openai_api_key:
            print("OPENAI_API_KEY is not configured in .env; no API request was made.")
            return 2
        try:
            brief = await app.prepare_lead(args.opportunity_id)
        except (LLMAPIError, LLMBudgetExceeded) as exc:
            print(f"Lead brief generation stopped safely: {exc}")
            return 2
        if brief is None:
            print("Lead not found, rejected, or not eligible for a brief.")
            return 2
        print(
            f"#{brief.opportunity_id} confidence={brief.confidence} action={brief.contact_strategy.value}\n"
            f"PROBLEM: {brief.problem_summary}\n"
            f"WHY FIT: {brief.why_fit}\n"
            f"MONEY: {brief.money_signal}\n"
            f"SCOPE: {brief.suggested_scope}\n"
            f"PRICE: {brief.suggested_price_band}\n"
            f"DRAFT:\n{brief.draft}"
        )
        return 0
    if args.command == "llm-classify":
        if not settings.llm_enabled:
            print("LLM classification is disabled by LLM_ENABLED=false.")
            return 2
        if not settings.openai_api_key:
            print("OPENAI_API_KEY is not configured in .env; no API request was made.")
            return 2
        sources = tuple(args.sources) if args.sources else None
        try:
            results = await app.classify_shortlist(args.limit, sources, force=args.force)
        except (LLMAPIError, LLMBudgetExceeded) as exc:
            print(f"LLM classification stopped safely: {exc}")
            return 2
        if not results:
            print("No unclassified P0/P1 candidates in the current shortlist.")
        for item in results:
            reason = item.rejection_reason if item.decision.value == "REJECT" else item.problem_summary
            print(
                f"#{item.opportunity_id} {item.decision.value} confidence={item.confidence} "
                f"author={item.author_type.value} intent={item.commercial_intent.value} "
                f"contact={item.should_contact} fit={item.technical_fit} payment={item.payment_intent}\n"
                f"  {reason}"
            )
        print(app.db.llm_usage_summary())
        return 0
    if args.command == "shortlist":
        sources = tuple(args.sources) if args.sources else None
        rows = app.db.shortlist(args.limit, sources)
        if not rows:
            print("No actionable P0/P1 opportunities in the current shortlist.")
            return 0
        for row in rows:
            print(
                f"#{row['id']} [{row['alert_class']}] priority={row['priority_score']} "
                f"cash={row['cash_score']} value={row['value_score']} rel={row['relationship_score']} "
                f"fresh={row['freshness_score']} comp={row['competition_score']} age={_age_label(row['created_at'])} "
                f"llm={row['llm_decision']} conf={row['llm_confidence'] if row['llm_confidence'] is not None else '-'}\n"
                f"  {row['author_type']} / {row['commercial_intent']} / {row['opportunity_class']} "
                f"[{row['source']}] {row['title']}\n"
                f"  {row['url']}"
            )
        return 0
    if args.command == "reclassify":
        sources = tuple(args.sources) if args.sources else None
        print(app.reclassify_stored(sources))
        return 0
    if args.command == "recent":
        for row in app.db.recent(args.limit):
            print(
                f"#{row['id']} [{row['alert_class']}] cash={row['cash_score']} value={row['value_score']} "
                f"rel={row['relationship_score']} author={row['author_type']} intent={row['commercial_intent']} "
                f"[{row['source']}] {row['title']}\n  {row['url']}\n  status={row['status']} created={row['created_at']}"
            )
        return 0
    if args.command == "telegram-id":
        chats = await app.telegram.discover_chat_ids()
        if not chats:
            print("No chats found. Send /start or any message to the bot, then run this command again.")
        else:
            for chat_id, name in chats:
                print(f"{chat_id}\t{name}")
        return 0
    return 1


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(async_main(args)))


if __name__ == "__main__":
    main()
