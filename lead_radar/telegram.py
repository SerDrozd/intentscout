from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from .database import Database
from .models import LeadBrief, ScoredOpportunity
from .text import normalize_text


class TelegramAPIError(RuntimeError):
    """Sanitized Telegram error that never contains the bot token or request URL."""


class TelegramClient:
    def __init__(self, token: str | None, chat_id: str | None, db: Database) -> None:
        self.token = token
        self.chat_id = chat_id
        self.db = db

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    @property
    def base_url(self) -> str:
        if not self.token:
            raise RuntimeError("Telegram token is not configured")
        return f"https://api.telegram.org/bot{self.token}"

    @staticmethod
    def _retry_after(response: httpx.Response) -> int | None:
        try:
            payload = response.json()
            value = ((payload.get("parameters") or {}).get("retry_after"))
            return int(value) if value is not None else None
        except (ValueError, TypeError, AttributeError):
            return None

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        api_method: str,
        *,
        retry_flood_once: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            response = await client.request(method, f"{self.base_url}/{api_method}", **kwargs)
        except httpx.HTTPError:
            raise TelegramAPIError("Telegram network request failed") from None

        if response.status_code == 429 and retry_flood_once:
            flood_retry_after = self._retry_after(response) or 2
            await asyncio.sleep(max(1, flood_retry_after) + 0.2)
            try:
                response = await client.request(method, f"{self.base_url}/{api_method}", **kwargs)
            except httpx.HTTPError:
                raise TelegramAPIError("Telegram network request failed after flood-control wait") from None

        if response.status_code >= 400:
            api_retry_after = self._retry_after(response)
            suffix = f"; retry_after={api_retry_after}s" if api_retry_after is not None else ""
            raise TelegramAPIError(f"Telegram API returned HTTP {response.status_code}{suffix}") from None
        return response

    @staticmethod
    def _age_label(created_at: datetime) -> str:
        created = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        seconds = max(0, int((datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()))
        if seconds < 3600:
            return f"{max(1, seconds // 60)}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"

    async def send_alert(
        self,
        opportunity_id: int,
        scored: ScoredOpportunity,
        brief: LeadBrief | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        o = scored.opportunity
        a = scored.assessment
        if a is not None:
            icon = {
                "P0_CASH": "🔥",
                "P0_HIGH_VALUE": "💰",
                "P0_RELATIONSHIP": "♻️",
            }.get(a.alert_class.value, "🔥")
            headline = (
                f"{icon} {a.alert_class.value} | CASH {a.cash_score} | VALUE {a.value_score} | REL {a.relationship_score}"
            )
            commercial = (
                f"{a.author_type.value} | {a.commercial_intent.value} | "
                f"AGE {self._age_label(o.created_at)} | COMP {a.competition_score}"
            )
        else:
            headline = f"🔥 {scored.score} - HIGH INTENT"
            commercial = f"AGE {self._age_label(o.created_at)}"

        author = o.author or "unknown"
        replies = o.metadata.get("reply_count", o.metadata.get("comments"))
        reply_line = f" | replies={replies}" if replies is not None else ""

        if brief is not None:
            draft = brief.draft or "(no draft generated)"
            text = (
                f"{headline}\n{commercial}\n\n"
                f"{o.source} · {author}{reply_line}\n{o.title[:220]}\n{o.url}\n\n"
                f"EXACT PROBLEM:\n{brief.problem_summary}\n\n"
                f"WHY FIT:\n{brief.why_fit}\n\n"
                f"MONEY SIGNAL:\n{brief.money_signal}\n\n"
                f"SUGGESTED SCOPE:\n{brief.suggested_scope}\n\n"
                f"PRICE:\n{brief.suggested_price_band}\n\n"
                f"NEXT ACTION:\n{brief.contact_strategy.value}\n\n"
                f"DRAFT:\n{draft}"
            )[:3900]
        else:
            matches = scored.analysis.phrase_matches[:6]
            match_lines = "\n".join(f"• {m.phrase}" for m in matches)
            excerpt = normalize_text(o.body)[:1000]
            text = (
                f"{headline}\n{commercial}\n\n"
                f"{o.source} · {author}{reply_line}\n{o.title[:220]}\n{o.url}\n\n"
                f"{excerpt}\n\nMATCHES\n{match_lines}"
            )[:3900]
        keyboard = {
            "inline_keyboard": [
                [{"text": "Open", "url": o.url}],
                [
                    {"text": "Ignore", "callback_data": f"status:{opportunity_id}:ignored"},
                    {"text": "Contacted", "callback_data": f"status:{opportunity_id}:contacted"},
                ],
                [
                    {"text": "Replied", "callback_data": f"status:{opportunity_id}:replied"},
                    {"text": "Won", "callback_data": f"status:{opportunity_id}:won"},
                ],
            ]
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            await self._request(
                client,
                "POST",
                "sendMessage",
                retry_flood_once=True,
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "reply_markup": keyboard,
                    "disable_web_page_preview": True,
                },
            )
        self.db.mark_notified(opportunity_id)
        return True

    async def poll_actions(self) -> int:
        if not self.enabled:
            return 0
        offset_raw = self.db.get_state("telegram_update_offset")
        params: dict[str, str | int] = {"timeout": 0, "allowed_updates": '["callback_query"]'}
        if offset_raw is not None:
            params["offset"] = int(offset_raw)

        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await self._request(client, "GET", "getUpdates", params=params)
            payload = response.json()
            handled = 0
            max_update_id: int | None = None
            for update in payload.get("result", []):
                update_id = int(update["update_id"])
                max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)
                cq = update.get("callback_query")
                if not cq:
                    continue
                data = cq.get("data", "")
                chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
                if self.chat_id and str(chat) != str(self.chat_id):
                    continue
                try:
                    prefix, opp_id_raw, status = data.split(":", 2)
                    if prefix != "status" or status not in {"ignored", "contacted", "replied", "won"}:
                        continue
                    self.db.set_status(int(opp_id_raw), status, reason="telegram_callback")
                    await self._request(
                        client,
                        "POST",
                        "answerCallbackQuery",
                        json={"callback_query_id": cq["id"], "text": f"Marked {status}"},
                    )
                    handled += 1
                except (ValueError, KeyError):
                    continue
            if max_update_id is not None:
                self.db.set_state("telegram_update_offset", str(max_update_id + 1))
        return handled

    async def discover_chat_ids(self) -> list[tuple[str, str]]:
        if not self.token:
            raise RuntimeError("Set TELEGRAM_BOT_TOKEN first")
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await self._request(client, "GET", "getUpdates", params={"timeout": 0})
            payload = response.json()
        chats: dict[str, str] = {}
        for update in payload.get("result", []):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if "id" not in chat:
                continue
            name = (
                chat.get("title")
                or " ".join(x for x in [chat.get("first_name"), chat.get("last_name")] if x)
                or chat.get("username")
                or "chat"
            )
            chats[str(chat["id"])] = name
        return sorted(chats.items())
