import calendar
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

import feedparser
import httpx

from .db import DB
from .llm import LLM
from .memory import Memory
from .prompts import ORDER_SCHEMA, ORDER_SYSTEM
from .settings import Settings
from .tg import TgReader
from .util import clip, html_to_text, keyword_hit, log, text_hash


@dataclass
class Post:
    arm_id: str
    source: str
    source_label: str
    external_id: str
    title: str
    text: str
    url: str | None = None
    author: str | None = None
    published: float | None = None
    extra: dict = field(default_factory=dict)
    prefilter: bool = True


def parse_feed(content: bytes, arm: dict) -> list[Post]:
    feed = feedparser.parse(content)
    out = []
    for entry in feed.entries:
        ident = entry.get("id") or entry.get("link") or entry.get("title")
        if not ident:
            continue
        published = None
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        if stamp:
            published = float(calendar.timegm(stamp))
        summary = entry.get("summary") or ""
        if not summary and entry.get("content"):
            summary = entry["content"][0].get("value", "")
        out.append(
            Post(
                arm_id=arm["id"],
                source="rss",
                source_label=arm["label"],
                external_id=f"{arm['id']}:{ident}",
                title=html_to_text(entry.get("title") or ""),
                text=html_to_text(summary, 3000),
                url=entry.get("link"),
                author=entry.get("author"),
                published=published,
            )
        )
    return out


def parse_hh(data: dict, arm: dict) -> list[Post]:
    out = []
    for item in data.get("items", []):
        snippet = item.get("snippet") or {}
        salary = item.get("salary") or {}
        money = ""
        if salary:
            money = " ".join(str(x) for x in (salary.get("from"), "–", salary.get("to"), salary.get("currency")) if x)
        employer = (item.get("employer") or {}).get("name", "")
        text = "\n".join(
            x
            for x in (
                f"Работодатель: {employer}" if employer else "",
                f"Зарплата: {money}" if money else "",
                html_to_text(snippet.get("requirement") or ""),
                html_to_text(snippet.get("responsibility") or ""),
                f"Занятость: {(item.get('employment') or {}).get('name', '')}",
                f"График: {(item.get('schedule') or {}).get('name', '')}",
            )
            if x
        )
        published = None
        if item.get("published_at"):
            try:
                published = time.mktime(time.strptime(item["published_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            except ValueError:
                published = None
        out.append(
            Post(
                arm_id=arm["id"],
                source="hh",
                source_label=arm["label"],
                external_id=f"hh:{item.get('id')}",
                title=item.get("name") or "",
                text=text,
                url=item.get("alternate_url"),
                author=employer or None,
                published=published,
                prefilter=False,
            )
        )
    return out


class Orders:
    def __init__(self, db: DB, memory: Memory, llm: LLM, settings: Settings, http: httpx.AsyncClient, tg: TgReader, deliver: Callable[[int], Awaitable[None]]):
        self.db = db
        self.memory = memory
        self.llm = llm
        self.settings = settings
        self.http = http
        self.tg = tg
        self.deliver = deliver
        self.cfg = settings.section("orders")

    def _cooling(self, arm: dict) -> bool:
        until = arm["state"].get("cooldown_until", 0)
        return time.time() < until

    def _fail(self, arm: dict, reason: str, hours: float = 6) -> int:
        state = dict(arm["state"])
        state["errors"] = int(state.get("errors", 0)) + 1
        state["last_error"] = reason[:200]
        state["cooldown_until"] = time.time() + hours * 3600
        self.db.set_arm_state(arm["id"], state)
        log.info("source %s failed: %s", arm["id"], reason)
        return state["errors"]

    def _ok(self, arm: dict, **extra) -> None:
        state = dict(arm["state"])
        state.update(extra)
        state["errors"] = 0
        state["last_ok"] = time.time()
        state.pop("cooldown_until", None)
        self.db.set_arm_state(arm["id"], state)

    async def poll_rss(self) -> int:
        posts = []
        for arm in self.db.arms(kind="order", source="rss"):
            if self._cooling(arm):
                continue
            try:
                response = await self.http.get(arm["params"]["url"], timeout=30)
                if response.status_code != 200:
                    self._fail(arm, f"HTTP {response.status_code}", 1)
                    continue
                posts += parse_feed(response.content, arm)
                self._ok(arm)
            except httpx.HTTPError as exc:
                self._fail(arm, str(exc), 1)
        return await self.process(posts)

    async def poll_hh(self) -> int:
        posts = []
        agent = self.cfg.get("hh_user_agent", "LeadHunter/1.0")
        for arm in self.db.arms(kind="order", source="hh"):
            if self._cooling(arm):
                continue
            params = {"text": arm["params"]["query"], "per_page": 50, "order_by": "publication_time", "period": 2}
            try:
                response = await self.http.get(
                    "https://api.hh.ru/vacancies",
                    params=params,
                    headers={"HH-User-Agent": agent, "User-Agent": agent},
                    timeout=30,
                )
                if response.status_code != 200:
                    self._fail(arm, f"HTTP {response.status_code}", 24 if response.status_code == 403 else 2)
                    continue
                posts += parse_hh(response.json(), arm)
                self._ok(arm)
            except (httpx.HTTPError, ValueError) as exc:
                self._fail(arm, str(exc), 2)
        return await self.process(posts)

    async def poll_telegram(self) -> int:
        if not self.tg.client:
            return 0
        posts = []
        for arm in self.db.arms(kind="order", source="telegram"):
            if self._cooling(arm):
                continue
            username = arm["params"]["username"]
            last_id = int(arm["state"].get("last_id", 0))
            try:
                messages = await self.tg.fetch(username, last_id, 50 if last_id else 20)
            except Exception as exc:
                if self._fail(arm, str(exc), 6) >= 3:
                    self.db.set_arm_active(arm["id"], False)
                    log.warning("telegram chat %s disabled after errors", username)
                continue
            for m in messages:
                posts.append(
                    Post(
                        arm_id=arm["id"],
                        source="telegram",
                        source_label=arm["label"],
                        external_id=f"{arm['id']}:{m['id']}",
                        title="",
                        text=m["text"][:3000],
                        url=m["url"],
                        author=m["author"],
                        published=m["date"],
                    )
                )
            top = max([m["id"] for m in messages] + [last_id])
            self._ok(arm, last_id=top)
        return await self.process(posts)

    def _fresh(self, posts: list[Post]) -> list[Post]:
        max_age = float(self.cfg.get("max_age_hours", 48)) * 3600
        include = self.memory.keywords("include")
        exclude = self.memory.keywords("exclude")
        now = time.time()
        out = []
        for post in posts:
            if self.db.is_seen(post.external_id):
                continue
            self.db.mark_seen(post.external_id)
            if post.published and now - post.published > max_age:
                continue
            body = f"{post.title}\n{post.text}"
            if not self.db.mark_seen("hash:" + text_hash(body)):
                continue
            if post.prefilter and not keyword_hit(body, include, exclude):
                continue
            if not post.prefilter and keyword_hit(body, exclude, []):
                continue
            out.append(post)
        return out

    async def process(self, posts: list[Post]) -> int:
        fresh = self._fresh(posts)
        if not fresh:
            return 0
        size = int(self.cfg.get("classify_batch", 8))
        created = 0
        for i in range(0, len(fresh), size):
            batch = fresh[i : i + size]
            results = await self.classify(batch)
            for post, item in results:
                score = max(0, min(100, int(item.get("score", 0))))
                if not item.get("is_order"):
                    continue
                lead_id = self.db.add_lead(
                    "order",
                    post.arm_id,
                    post.source,
                    post.external_id,
                    clip(post.title or item.get("summary") or post.text, 200),
                    post.url,
                    {"post": asdict(post), "source_label": post.source_label},
                    item,
                    score,
                )
                if lead_id:
                    created += 1
                    await self.deliver(lead_id)
        return created

    async def classify(self, batch: list[Post]) -> list[tuple[Post, dict]]:
        payload = [
            {"idx": i, "source": p.source_label, "title": p.title, "text": clip(p.text, 1500), "author": p.author or ""}
            for i, p in enumerate(batch)
        ]
        context = self.memory.context("order")
        user = (context + "\n\n" if context else "") + "Посты:\n" + json.dumps(payload, ensure_ascii=False)
        result = await self.llm.json(ORDER_SYSTEM, user, ORDER_SCHEMA, "orders", max_tokens=600 * len(batch) + 500)
        if not result:
            return []
        out = []
        for item in result.get("items", []):
            idx = item.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(batch):
                out.append((batch[idx], item))
        return out

    async def discover_chats(self, limit: int = 10) -> list[dict]:
        if not self.tg.client:
            return []
        known = {a["id"] for a in self.db.arms(kind="order", source="telegram", active_only=False)}
        found = []
        for query in self.db.get("chat_queries", [])[:12]:
            try:
                chats = await self.tg.search_chats(query)
            except Exception as exc:
                log.info("chat search failed %s: %s", query, exc)
                continue
            for chat in chats:
                arm_id = f"tg:{chat['username'].lower()}"
                if arm_id in known or not self.db.mark_seen(f"chatsug:{chat['username'].lower()}"):
                    continue
                chat["query"] = query
                found.append(chat)
                if len(found) >= limit:
                    return found
        return found
