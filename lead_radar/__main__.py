from __future__ import annotations

import argparse
import asyncio
import logging
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
    sub.add_parser("telegram-id", help="Show chat IDs seen by the configured Telegram bot")
    return p


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
