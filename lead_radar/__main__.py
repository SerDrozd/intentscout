from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from .app import RadarApp
from .config import load_settings


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
        print(f"fetched={stats.fetched} matched={stats.matched} inserted={stats.inserted} alerted={stats.alerted} excluded={stats.excluded}")
        return 0
    if args.command == "run":
        await app.run_forever()
        return 0
    if args.command == "stats":
        print(app.db.stats())
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
                f"fresh={row['freshness_score']} comp={row['competition_score']} age={_age_label(row['created_at'])}\n"
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
