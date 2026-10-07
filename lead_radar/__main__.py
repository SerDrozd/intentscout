from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .app import RadarApp
from .config import load_settings
from .google_alerts import (
    build_google_alert_plan,
    combined_feed_definitions,
    load_google_alert_queries,
    upsert_google_alert_feed,
)
from .llm import LLMAPIError, LLMBudgetExceeded
from .optimizer import advise_roi


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
        choices=("contacted", "replied", "proposal", "won", "lost", "ignored", "new", "reset"),
    )
    action.add_argument("--revenue", type=float, default=None, help="Revenue in USD; normally used with --action won")
    action.add_argument("--note", default=None, help="Optional short note stored in the action history")
    action.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive --action reset",
    )
    roi = sub.add_parser("roi", help="Show source/query funnel and revenue performance")
    roi.add_argument("--days", type=int, default=30)
    roi.add_argument("--by", choices=("source", "query"), default="source")
    roi.add_argument("--source", action="append", dest="sources", help="Limit to a source; repeatable")
    optimize = sub.add_parser("optimize", help="Recommend source/query priorities without changing configuration")
    optimize.add_argument("--days", type=int, default=30)
    optimize.add_argument("--by", choices=("source", "query"), default="source")
    optimize.add_argument("--source", action="append", dest="sources", help="Limit to a source; repeatable")
    schedule_plan = sub.add_parser("schedule-plan", help="Preview safe adaptive polling/query allocation")
    schedule_plan.add_argument("--days", type=int, default=None, help="ROI window; defaults to ADAPTIVE_WINDOW_DAYS")
    alerts_plan = sub.add_parser("alerts-plan", help="Show buyer-intent Google Alerts setup and ROI status")
    alerts_plan.add_argument("--days", type=int, default=30, help="ROI window for configured Google Alerts")
    alerts_add = sub.add_parser("alerts-add", help="Add or update one real Google Alerts RSS URL locally")
    alerts_add.add_argument("--key", required=True, help="Query key from alerts-plan")
    alerts_add.add_argument("--url", required=True, help="Real Google Alerts RSS feed URL")
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




def _number(value: object | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (str, bytes, bytearray)):
        return float(value)
    raise TypeError(f"Cannot convert {type(value).__name__} to number")

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
            if args.action == "reset":
                if args.revenue is not None:
                    raise ValueError("--revenue cannot be used with --action reset")
                if not args.yes:
                    print("Refusing to reset feedback without --yes.")
                    return 2
                app.db.reset_feedback(args.opportunity_id)
                print(f"#{args.opportunity_id} feedback reset -> new")
                return 0
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
        roi_rows = app.db.roi_report(days=args.days, by_query=args.by == "query", sources=sources)
        if not roi_rows:
            print("No ROI data in the selected window.")
            return 0
        print(f"ROI window: {args.days}d | by={args.by}")
        for roi_row in roi_rows:
            label = str(roi_row["source"]) if args.by == "source" else f"{roi_row['source']}:{roi_row['key']}"
            fetched = roi_row.get("fetched")
            fetch_part = f" fetched={fetched}" if fetched is not None else ""
            print(
                f"{label}{fetch_part} leads={roi_row['leads']} p0={roi_row['p0']} p1={roi_row['p1']} "
                f"contacted={roi_row['contacted']} replied={roi_row['replied']} proposal={roi_row['proposal']} "
                f"won={roi_row['won']} lost={roi_row['lost']} ignored={roi_row['ignored']} "
                f"revenue=${_number(roi_row['revenue']):.2f} llm=${_number(roi_row['llm_cost']):.6f}"
            )
        return 0
    if args.command == "optimize":
        sources = tuple(args.sources) if args.sources else None
        by_query = args.by == "query"
        roi_rows = app.db.roi_report(days=args.days, by_query=by_query, sources=sources)
        if not roi_rows:
            print("No ROI data in the selected window.")
            return 0
        advice = advise_roi(roi_rows, by_query=by_query)
        print(f"Optimizer advisor: {args.days}d | by={args.by} | dry_run=true")
        for optimization_item in advice:
            label = (
                optimization_item.source
                if not by_query
                else f"{optimization_item.source}:{optimization_item.key}"
            )
            print(
                f"{label} recommendation={optimization_item.recommendation.value} "
                f"confidence={optimization_item.confidence.value} "
                f"leads={optimization_item.leads} actionable={optimization_item.actionable} "
                f"rate={optimization_item.actionable_rate:.2%} "
                f"contacted={optimization_item.contacted} replied={optimization_item.replied} "
                f"proposal={optimization_item.proposal} won={optimization_item.won} "
                f"revenue=${optimization_item.revenue:.2f} llm=${optimization_item.llm_cost:.6f}"
            )
            print(f"  reason: {optimization_item.reason}")
        print("Dry-run only: no source, query, schedule, or budget setting was changed.")
        return 0
    if args.command == "schedule-plan":
        rows = app.adaptive_schedule_rows(days=args.days)
        window_days = args.days or settings.adaptive_window_days
        print(
            f"Adaptive schedule preview: {window_days}d | enabled={str(settings.adaptive_scheduling_enabled).lower()} "
            "| in_memory=true | permanent_disable=false"
        )
        for schedule_row in rows:
            query_part = ""
            if schedule_row["query_limit"] is not None:
                query_part = f" queries={schedule_row['query_limit']}/{schedule_row['query_available']}"
            print(
                f"{schedule_row['source']} recommendation={schedule_row['recommendation']} "
                f"confidence={schedule_row['confidence']} poll_every={schedule_row['interval_cycles']} cycle(s)"
                f"{query_part}"
            )
            print(f"  reason: {schedule_row['reason']}")
        print("Preview only: no .env/YAML/source-enable setting was changed.")
        return 0
    if args.command == "alerts-plan":
        queries = load_google_alert_queries(settings.google_alerts_queries_path)
        feeds = combined_feed_definitions(settings.rss_feeds_path, settings.rss_local_feeds_path)
        plan_rows = build_google_alert_plan(queries, feeds)
        roi_rows = app.db.roi_report(days=args.days, by_query=True, sources=("google_alerts",))
        roi_by_key = {str(row["key"]): row for row in roi_rows}
        print(
            f"Google Alerts buyer-signal plan: {args.days}d | "
            f"local_config={settings.rss_local_feeds_path} | committed_urls=false"
        )
        for alert_row in plan_rows:
            alert_roi_row = roi_by_key.get(alert_row.key)
            metrics = ""
            if alert_roi_row is not None:
                actionable = int(_number(alert_roi_row["p0"])) + int(_number(alert_roi_row["p1"]))
                metrics = (
                    f" leads={alert_roi_row['leads']} actionable={actionable} "
                    f"contacted={alert_roi_row['contacted']} replied={alert_roi_row['replied']} "
                    f"won={alert_roi_row['won']} revenue=${_number(alert_roi_row['revenue']):.2f}"
                )
            print(
                f"{alert_row.priority} {alert_row.key} status={alert_row.status} "
                f"category={alert_row.category_kind}{metrics}\n"
                f"  query: {alert_row.query}\n"
                f"  why: {alert_row.rationale}"
            )
        print("Real feed URLs belong only in the gitignored local RSS config.")
        return 0
    if args.command == "alerts-add":
        queries = load_google_alert_queries(settings.google_alerts_queries_path)
        query_by_key = {query.key: query for query in queries}
        selected = query_by_key.get(str(args.key))
        if selected is None:
            print(f"Unknown Google Alerts query key: {args.key}. Run alerts-plan first.")
            return 2
        try:
            upsert_google_alert_feed(settings.rss_local_feeds_path, selected, str(args.url))
        except ValueError as exc:
            print(str(exc))
            return 2
        print(
            f"Configured {selected.key} in {settings.rss_local_feeds_path}. "
            "The local file is gitignored; run alerts-plan to verify ACTIVE status."
        )
        return 0
    if args.command == "llm-stats":
        usage: dict[str, object] = dict(app.db.llm_usage_summary())
        usage["daily_budget_usd"] = settings.daily_llm_budget_usd
        usage["monthly_budget_usd"] = settings.monthly_llm_budget_usd
        usage["max_candidates_per_hour"] = settings.max_llm_candidates_per_hour
        usage["max_candidates_per_day"] = settings.max_llm_candidates_per_day
        usage["alert_reserve_per_hour"] = settings.llm_alert_reserve_per_hour
        usage["alert_reserve_per_day"] = settings.llm_alert_reserve_per_day
        usage["review_alerts_enabled"] = settings.review_alerts_enabled
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
        shortlist_rows = app.db.shortlist(args.limit, sources)
        if not shortlist_rows:
            print("No actionable P0/P1 opportunities in the current shortlist.")
            return 0
        for shortlist_row in shortlist_rows:
            print(
                f"#{shortlist_row['id']} [{shortlist_row['alert_class']}] priority={shortlist_row['priority_score']} "
                f"cash={shortlist_row['cash_score']} value={shortlist_row['value_score']} rel={shortlist_row['relationship_score']} "
                f"fresh={shortlist_row['freshness_score']} comp={shortlist_row['competition_score']} age={_age_label(str(shortlist_row['created_at']))} "
                f"llm={shortlist_row['llm_decision']} conf={shortlist_row['llm_confidence'] if shortlist_row['llm_confidence'] is not None else '-'}\n"
                f"  {shortlist_row['author_type']} / {shortlist_row['commercial_intent']} / {shortlist_row['opportunity_class']} "
                f"[{shortlist_row['source']}] {shortlist_row['title']}\n"
                f"  {shortlist_row['url']}"
            )
        return 0
    if args.command == "reclassify":
        sources = tuple(args.sources) if args.sources else None
        print(app.reclassify_stored(sources))
        return 0
    if args.command == "recent":
        for recent_row in app.db.recent(args.limit):
            print(
                f"#{recent_row['id']} [{recent_row['alert_class']}] cash={recent_row['cash_score']} value={recent_row['value_score']} "
                f"rel={recent_row['relationship_score']} author={recent_row['author_type']} intent={recent_row['commercial_intent']} "
                f"[{recent_row['source']}] {recent_row['title']}\n  {recent_row['url']}\n  status={recent_row['status']} created={recent_row['created_at']}"
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
