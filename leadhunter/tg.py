from datetime import timezone

from .settings import Settings
from .util import log


class TgReader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.tg_api_id and self.settings.tg_api_hash)

    def _make(self):
        from telethon import TelegramClient

        return TelegramClient(str(self.settings.tg_session_path), self.settings.tg_api_id, self.settings.tg_api_hash)

    async def login(self) -> None:
        client = self._make()
        await client.start()
        me = await client.get_me()
        print(f"Готово: вошёл как {getattr(me, 'username', None) or me.id}")
        await client.disconnect()

    async def start(self) -> bool:
        if not self.configured:
            return False
        if not self.settings.tg_session_path.with_suffix(".session").exists():
            log.warning("Telegram-сессии нет, запусти: python -m leadhunter tg-login")
            return False
        self.client = self._make()
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.warning("Telegram-сессия не авторизована, запусти: python -m leadhunter tg-login")
            await self.client.disconnect()
            self.client = None
            return False
        return True

    async def fetch(self, username: str, min_id: int, limit: int) -> list[dict]:
        if not self.client:
            return []
        entity = await self.client.get_entity(username)
        out = []
        async for msg in self.client.iter_messages(entity, min_id=min_id, limit=limit):
            text = msg.message or ""
            if not text.strip():
                continue
            sender = msg.sender
            author = None
            if sender is not None and getattr(sender, "username", None):
                author = "@" + sender.username
            out.append(
                {
                    "id": msg.id,
                    "text": text,
                    "date": msg.date.replace(tzinfo=msg.date.tzinfo or timezone.utc).timestamp(),
                    "author": author,
                    "url": f"https://t.me/{username}/{msg.id}",
                }
            )
        return out

    async def search_chats(self, query: str, limit: int = 20) -> list[dict]:
        if not self.client:
            return []
        from telethon.tl.functions.contacts import SearchRequest

        result = await self.client(SearchRequest(q=query, limit=limit))
        out = []
        for chat in result.chats:
            username = getattr(chat, "username", None)
            if not username:
                continue
            out.append(
                {
                    "username": username,
                    "title": getattr(chat, "title", username),
                    "members": getattr(chat, "participants_count", None),
                    "broadcast": bool(getattr(chat, "broadcast", False)),
                }
            )
        return out

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()
            self.client = None
