import asyncio
import json
from typing import Awaitable, Callable

import httpx

from .util import clip, log


Handler = Callable[[str, str], Awaitable[None]]
CallbackHandler = Callable[[str, dict], Awaitable[str | None]]


class Bot:
    def __init__(self, token: str, owner_id: int | None):
        self.token = token
        self.owner_id = owner_id
        self.base = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(70.0, connect=15.0))
        self.on_command: Handler | None = None
        self.on_text: Handler | None = None
        self.on_callback: CallbackHandler | None = None
        self.offset = 0

    async def call(self, method: str, files: dict | None = None, **payload):
        for attempt in range(4):
            try:
                if files:
                    data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else str(v)) for k, v in payload.items() if v is not None}
                    response = await self.client.post(f"{self.base}/{method}", data=data, files=files)
                else:
                    response = await self.client.post(f"{self.base}/{method}", json={k: v for k, v in payload.items() if v is not None})
                body = response.json()
                if body.get("ok"):
                    return body.get("result")
                if response.status_code == 429:
                    wait = (body.get("parameters") or {}).get("retry_after", 5)
                    await asyncio.sleep(int(wait) + 1)
                    continue
                log.warning("telegram %s failed: %s", method, body.get("description"))
                return None
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("telegram %s error: %s", method, exc)
                await asyncio.sleep(2 ** attempt)
        return None

    async def send(self, text: str, markup: dict | None = None, chat_id: int | None = None, preview: bool = False) -> int | None:
        target = chat_id or self.owner_id
        if not target:
            log.warning("TELEGRAM_OWNER_ID не задан, сообщение не отправлено")
            return None
        result = None
        chunks = self._chunks(text)
        for i, chunk in enumerate(chunks):
            result = await self.call(
                "sendMessage",
                chat_id=target,
                text=chunk,
                parse_mode="HTML",
                reply_markup=markup if i == len(chunks) - 1 else None,
                link_preview_options={"is_disabled": not preview},
            )
        return result.get("message_id") if result else None

    async def send_photo(self, photo: bytes, caption: str, markup: dict | None = None) -> int | None:
        if not self.owner_id:
            return None
        result = await self.call(
            "sendPhoto",
            files={"photo": ("site.jpg", photo, "image/jpeg")},
            chat_id=self.owner_id,
            caption=clip(caption, 1024),
            parse_mode="HTML",
            reply_markup=markup,
        )
        return result.get("message_id") if result else None

    @staticmethod
    def _chunks(text: str, limit: int = 4000) -> list[str]:
        if len(text) <= limit:
            return [text]
        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > limit:
                parts.append(current)
                current = ""
            current += line + "\n"
        if current:
            parts.append(current)
        return [p for p in parts if p.strip()]

    async def edit_markup(self, chat_id: int, message_id: int, markup: dict) -> None:
        await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=markup)

    async def poll_forever(self) -> None:
        while True:
            try:
                updates = await self.call("getUpdates", offset=self.offset, timeout=50, allowed_updates=["message", "callback_query"])
            except Exception as exc:
                log.warning("getUpdates crashed: %s", exc)
                updates = None
            if updates is None:
                await asyncio.sleep(5)
                continue
            for update in updates:
                self.offset = max(self.offset, update["update_id"] + 1)
                try:
                    await self._handle(update)
                except Exception:
                    log.exception("update handling failed")

    async def _handle(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            user_id = (query.get("from") or {}).get("id")
            if user_id != self.owner_id:
                await self.call("answerCallbackQuery", callback_query_id=query["id"], text="Нет доступа")
                return
            note = None
            if self.on_callback:
                note = await self.on_callback(query.get("data") or "", query)
            await self.call("answerCallbackQuery", callback_query_id=query["id"], text=note or "")
            return
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        user_id = (message.get("from") or {}).get("id")
        chat_id = (message.get("chat") or {}).get("id")
        if not text:
            return
        if user_id != self.owner_id:
            if text.startswith("/start"):
                await self.send(
                    f"Твой Telegram id: <code>{user_id}</code>\nПропиши его в .env как TELEGRAM_OWNER_ID и перезапусти агента.",
                    chat_id=chat_id,
                )
            return
        if text.startswith("/"):
            if self.on_command:
                await self.on_command(text, str(chat_id))
        elif self.on_text:
            await self.on_text(text, str(chat_id))

    async def close(self) -> None:
        await self.client.aclose()
