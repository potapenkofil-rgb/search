import asyncio
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .bot import Bot
from .companies import Companies
from .db import DB
from .discovery import NICHE_TAGS
from .formatting import brief, digest_rows, keyboard, lead_text, menu_keyboard, more_keyboard
from .llm import LLM, BudgetExceeded
from .memory import VERDICTS, Memory
from .orders import Orders
from .reflect import reflect
from .settings import Settings
from .tg import TgReader
from .util import clip, esc, http_client, log, normalize_url


HELP = """<b>LeadHunter</b> — ищу компании с плохими сайтами и заказы на разработку.

Кнопки снизу — всё основное. Под лидом: оценка, «Подробнее», «Открыть».
Любой текст — директива навсегда. Например: «не присылай салоны красоты».

Команды, если удобнее ими: /stats /sources /rules /digest /reflect /audit /parse
/lead N /addniche /addcity /addchat /addrss /addhh /forget N /rollback
/disable id /enable id /threshold company|order|digest N /pause /resume"""

ASK = {
    "audit": "Пришли адрес сайта, проверю. Например: example.ru",
    "addniche": "Какую нишу добавить? Например: стоматология",
    "addcity": "Какой город добавить? Например: Казань",
    "addchat": "Пришли @чат, который читать",
    "parse": "Что искать парсером? Формат: ниша | город 30",
    "addrss": "Пришли ссылку на RSS",
}

BUTTONS = {
    "📊 Статистика": "stats",
    "📋 Сводка": "digest",
    "🎯 Источники": "sources",
    "🧠 Правила": "rules",
    "🔍 Проверить сайт": "ask:audit",
    "⚙️ Ещё": "more",
}


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = DB(settings.db_path)
        self.memory = Memory(self.db, settings)
        self.memory.bootstrap()
        self.llm = LLM(settings, self.db)
        self.http = http_client(float(settings.section("audit").get("timeout_seconds", 20)))
        self.bot = Bot(settings.bot_token, settings.owner_id)
        self.tg = TgReader(settings)
        self.companies = Companies(self.db, self.memory, self.llm, settings, self.http, self.deliver)
        self.orders = Orders(self.db, self.memory, self.llm, settings, self.http, self.tg, self.deliver)
        self.tz = ZoneInfo(settings.section("schedule").get("timezone", "Europe/Moscow"))
        self.bot.on_command = self.command
        self.bot.on_text = self.text
        self.bot.on_callback = self.callback

    def paused(self) -> bool:
        return bool(self.db.get("paused", False))

    async def deliver(self, lead_id: int, force: bool = False) -> None:
        lead = self.db.lead(lead_id)
        if not lead:
            return
        force = force or lead["arm_id"] == "manual"
        name = "notify_company" if lead["kind"] == "company" else "notify_order"
        hot = lead["score"] >= self.memory.threshold(name)
        if hot and not force and self.alerts_last_hour() >= int(self.settings.section("thresholds").get("max_alerts_per_hour", 4)):
            self.db.set_lead_status(lead_id, "digest")
            return
        if force or hot:
            message_id = await self.send_card(lead)
            self.db.set_lead_status(lead_id, "sent", notified_at=time.time(), message_id=message_id)
        elif lead["score"] >= self.memory.threshold("digest"):
            self.db.set_lead_status(lead_id, "digest")
        else:
            self.db.set_lead_status(lead_id, "low")

    def alerts_last_hour(self) -> int:
        row = self.db.one("SELECT COUNT(*) AS n FROM leads WHERE notified_at > ? AND arm_id != 'manual'", time.time() - 3600)
        return int(row["n"])

    async def send_card(self, lead: dict) -> int | None:
        text = brief(lead)
        markup = keyboard(lead["id"], lead.get("verdict"), False, lead.get("url"))
        shot = self.settings.data_dir / "shots" / f"{lead['id']}.jpg"
        if shot.exists():
            return await self.bot.send_photo(shot.read_bytes(), text, markup)
        return await self.bot.send(text, markup)

    async def callback(self, data: str, query: dict) -> str | None:
        message = query.get("message") or {}
        if data.startswith("fb:"):
            _, lead_id, verdict = data.split(":", 2)
            lead = self.memory.feedback(int(lead_id), verdict)
            if not lead:
                return "Лид не найден"
            expanded = bool(self.db.get(f"expanded:{lead['id']}", False))
            if message:
                await self.bot.edit_markup(
                    message["chat"]["id"], message["message_id"], keyboard(lead["id"], verdict, expanded, lead.get("url"))
                )
            return f"Запомнил: {VERDICTS[verdict]}"
        if data.startswith("x:"):
            _, lead_id, flag = data.split(":", 2)
            lead = self.db.lead(int(lead_id))
            if not lead:
                return "Лид не найден"
            expanded = flag == "1"
            self.db.put(f"expanded:{lead['id']}", expanded)
            markup = keyboard(lead["id"], lead.get("verdict"), expanded, lead.get("url"))
            text = lead_text(lead, expanded)
            if not message or not await self.bot.edit(message, text, markup):
                await self.bot.send(text, markup)
            return None
        if data.startswith("t:"):
            _, name, delta = data.split(":", 2)
            self.memory.set_threshold(name, self.memory.threshold(name) + int(delta))
            if message:
                await self.bot.edit(message, self.thresholds_text(), self.thresholds_keyboard())
            return None
        if data.startswith("m:"):
            action = data.split(":", 1)[1]
            await self.run_action(action)
            return None
        if data.startswith("a:"):
            _, flag, arm_id = data.split(":", 2)
            if not self.db.set_arm_active(arm_id, flag == "1"):
                return "Не нашёл источник"
            await self.cmd_sources("")
            return "Включил" if flag == "1" else "Выключил"
        if data.startswith("d:"):
            removed = self.memory.remove_directive(int(data.split(":", 1)[1]))
            if removed:
                await self.cmd_rules("")
            return "Удалил" if removed else "Уже нет"
        if data.startswith("addchat:"):
            username = data.split(":", 1)[1]
            added = self.memory.add_chat_arm(username)
            return f"Читаю @{username}" if added else "Уже читаю"
        return None

    async def run_action(self, action: str) -> None:
        if action.startswith("ask:"):
            await self.ask(action.split(":", 1)[1])
            return
        if action == "more":
            await self.bot.send("Что сделать?", more_keyboard(self.paused()))
            return
        if action == "thresholds":
            await self.bot.send(self.thresholds_text(), self.thresholds_keyboard())
            return
        handler = getattr(self, f"cmd_{action}", None)
        if handler:
            await self.guard(handler, "")

    def thresholds_text(self) -> str:
        return (
            "<b>Пороги отправки</b>\n"
            f"Компании {self.memory.threshold('notify_company')} · "
            f"Заказы {self.memory.threshold('notify_order')} · "
            f"Сводка {self.memory.threshold('digest')}\n"
            "Выше порога — присылаю сразу, ниже — в сводке."
        )

    @staticmethod
    def thresholds_keyboard() -> dict:
        rows = []
        for label, name in (("Компании", "notify_company"), ("Заказы", "notify_order"), ("Сводка", "digest")):
            rows.append([
                {"text": f"− {label}", "callback_data": f"t:{name}:-5"},
                {"text": f"+ {label}", "callback_data": f"t:{name}:5"},
            ])
        return {"inline_keyboard": rows}

    async def ask(self, action: str) -> None:
        self.db.put("pending", action)
        await self.bot.send(ASK.get(action, "Пришли значение"), {"force_reply": True, "input_field_placeholder": "Ответь сюда"})

    async def text(self, text: str, chat_id: str) -> None:
        action = BUTTONS.get(text.strip())
        if action:
            self.db.put("pending", None)
            await self.run_action(action)
            return
        pending = self.db.get("pending")
        if pending:
            self.db.put("pending", None)
            handler = getattr(self, f"cmd_{pending}", None)
            if handler:
                await self.guard(handler, text.strip())
                return
        directive_id = self.memory.add_directive(text)
        await self.bot.send(f"Запомнил: {esc(clip(text, 200))}\nУдалить: /forget {directive_id}")

    async def command(self, text: str, chat_id: str) -> None:
        match = re.match(r"^/(\w+)(?:@\w+)?\s*(.*)$", text, re.S)
        if not match:
            return
        cmd, arg = match.group(1).lower(), match.group(2).strip()
        lead_match = re.match(r"^lead_(\d+)$", cmd)
        if lead_match:
            cmd, arg = "lead", lead_match.group(1)
        handler = getattr(self, f"cmd_{cmd}", None)
        if not handler:
            await self.bot.send("Не знаю такой команды. /help")
            return
        self.db.put("pending", None)
        await self.guard(handler, arg)

    async def guard(self, handler, arg: str) -> None:
        try:
            await handler(arg)
        except BudgetExceeded:
            await self.bot.send("Дневной лимит расходов на ИИ исчерпан. Подними DAILY_BUDGET_USD или подожди до завтра.")
        except Exception as exc:
            log.exception("handler failed")
            await self.bot.send(f"Ошибка: {esc(clip(str(exc), 300))}")

    async def cmd_start(self, arg: str) -> None:
        await self.bot.send(HELP, menu_keyboard())

    async def cmd_menu(self, arg: str) -> None:
        await self.bot.send("Меню снизу.", menu_keyboard())

    async def cmd_more(self, arg: str) -> None:
        await self.bot.send("Что сделать?", more_keyboard(self.paused()))

    async def cmd_help(self, arg: str) -> None:
        await self.bot.send(HELP)

    async def cmd_stats(self, arg: str) -> None:
        day = time.time() - 86400
        week = time.time() - 7 * 86400
        lines = []
        for kind, label in (("company", "Компании"), ("order", "Заказы")):
            d = self.db.one("SELECT COUNT(*) AS n, SUM(CASE WHEN notified_at IS NOT NULL THEN 1 ELSE 0 END) AS s FROM leads WHERE kind = ? AND created_at > ?", kind, day)
            lines.append(f"{label}: {d['n']} найдено, {d['s'] or 0} прислал")
        fb = self.db.all("SELECT verdict, COUNT(*) AS n FROM feedback WHERE updated_at > ? GROUP BY verdict", week)
        counts = {r["verdict"]: r["n"] for r in fb}
        queue = self.db.one("SELECT COUNT(*) AS n FROM candidates WHERE status = 'queued'")
        usage = self.db.usage()
        lines.append("Оценки за неделю: " + ", ".join(f"{VERDICTS[v]} {counts.get(v, 0)}" for v in VERDICTS))
        lines.append(f"В очереди {queue['n']} · ${usage['cost']:.2f} сегодня, ${self.db.usage_month():.2f} за месяц")
        if self.paused():
            lines.append("⏸ Поиск на паузе")
        await self.bot.send("<b>За сутки</b>\n" + "\n".join(lines), more_keyboard(self.paused()))

    async def cmd_sources(self, arg: str) -> None:
        stats = [s for s in self.memory.arm_stats(include_inactive=True) if s["leads"] or not s["active"]]
        stats.sort(key=lambda s: (-s["active"], -s["mean"], -s["leads"]))
        if not stats:
            await self.bot.send("Пока ничего не нашёл — источники только раскручиваются.")
            return
        rows = []
        for s in stats[:12]:
            good = s["interesting"] + s["taken"] + s["won"]
            mark = "" if s["active"] else "⏸ "
            rows.append([
                {"text": f"{mark}{clip(s['label'], 26)} · {s['leads']}/{good}", "callback_data": "noop"},
                {"text": "Вкл" if not s["active"] else "Выкл", "callback_data": f"a:{int(not s['active'])}:{s['id']}"[:64]},
            ])
        more = f"\nПоказал {min(12, len(stats))} из {len(stats)}" if len(stats) > 12 else ""
        await self.bot.send(f"<b>Источники</b> · найдено/понравилось{more}", {"inline_keyboard": rows})

    async def cmd_rules(self, arg: str) -> None:
        rules = self.memory.rules()
        directives = self.memory.directives()
        lines = []
        if directives:
            lines.append("<b>Твои директивы</b>")
            lines += [f"{d['id']}. {esc(clip(d['text'], 90))}" for d in directives]
        if rules:
            lines.append("")
            lines.append("<b>Что я понял сам</b>")
            lines += [f"• {esc(clip(r, 110))}" for r in rules[:12]]
        if not lines:
            lines = ["Правил пока нет. Оценивай лиды кнопками — они появятся.", "Любой текст сюда станет директивой."]
        rows = [[{"text": f"Удалить {d['id']}", "callback_data": f"d:{d['id']}"}] for d in directives[:6]]
        await self.bot.send("\n".join(lines), {"inline_keyboard": rows} if rows else None)

    async def cmd_forget(self, arg: str) -> None:
        ok = arg.isdigit() and self.memory.remove_directive(int(arg))
        await self.bot.send("Удалил" if ok else "Не нашёл такую директиву. /rules")

    async def cmd_rollback(self, arg: str) -> None:
        ok = self.memory.rollback_rules()
        await self.bot.send("Откатил правила на предыдущую версию" if ok else "Откатывать нечего")

    async def cmd_pause(self, arg: str) -> None:
        self.db.put("paused", True)
        await self.bot.send("⏸ Пауза", more_keyboard(True))

    async def cmd_resume(self, arg: str) -> None:
        self.db.put("paused", False)
        await self.bot.send("▶️ Ищу дальше", more_keyboard(False))

    async def cmd_digest(self, arg: str) -> None:
        await self.digest(force=True)

    async def cmd_reflect(self, arg: str) -> None:
        await self.bot.send("Разбираю…")
        await reflect(self)

    async def cmd_audit(self, arg: str) -> None:
        url = normalize_url(arg)
        if not url:
            await self.bot.send("Формат: /audit example.ru")
            return
        await self.bot.send(f"Смотрю {esc(url)}…")
        lead_id = await self.companies.manual(url)
        if not lead_id:
            await self.bot.send("Не получилось оценить сайт")

    async def cmd_parse(self, arg: str) -> None:
        if not self.companies.parser.enabled:
            await self.bot.send("Парсер 2ГИС не подключён: секция parser в config.yaml")
            return
        match = re.match(r"^(.+?)\s*\|\s*(.+?)(?:\s+(\d+))?$", arg)
        if not match:
            await self.bot.send("Формат: /parse стоматология | Казань 30")
            return
        niche, city, limit = match.group(1), match.group(2), int(match.group(3) or 20)
        await self.bot.send(f"Ищу: {esc(niche)} · {esc(city)}…")
        added = await self.companies.parser_request(niche, city, limit, origin="user")
        await self.bot.send(f"+{added} компаний в очередь. Подходящие пришлю.")

    async def cmd_lead(self, arg: str) -> None:
        lead = self.db.lead(int(arg)) if arg.isdigit() else None
        if not lead:
            await self.bot.send("Нет такого лида")
            return
        await self.bot.send(lead_text(lead, True), keyboard(lead["id"], lead.get("verdict"), True, lead.get("url")))

    async def cmd_addniche(self, arg: str) -> None:
        niche = arg.strip().lower()
        if not niche:
            await self.bot.send("Формат: /addniche стоматология")
            return
        cities = self.memory.cities()
        added = sum(1 for city in cities if self.memory.add_niche_arm(niche, city))
        note = "" if niche in NICHE_TAGS else "\nТегов OSM для неё нет — поищу через 2ГИС или предложу теги на разборе."
        await self.bot.send(f"«{esc(niche)}» · {added} городов{note}")

    async def cmd_addcity(self, arg: str) -> None:
        city = arg.strip()
        if not city:
            await self.bot.send("Формат: /addcity Казань")
            return
        niches = self.memory.niches()
        added = sum(1 for niche in niches if self.memory.add_niche_arm(niche, city))
        await self.bot.send(f"{esc(city)} · {added} ниш")

    async def cmd_addchat(self, arg: str) -> None:
        if not arg:
            await self.bot.send("Формат: /addchat @chat_username")
            return
        ok = self.memory.add_chat_arm(arg)
        extra = "" if self.tg.client else "\nTelegram-аккаунт не подключён, читать не смогу."
        await self.bot.send(("Читаю " if ok else "Уже читаю ") + esc(arg) + extra)

    async def cmd_addrss(self, arg: str) -> None:
        parts = arg.split(maxsplit=1)
        url = normalize_url(parts[0]) if parts else None
        if not url:
            await self.bot.send("Формат: /addrss https://site/rss.xml Название")
            return
        name = parts[1] if len(parts) > 1 else url
        ok = self.db.ensure_arm(f"rss:{url}", "order", "rss", name, {"url": url}, origin="user")
        await self.bot.send("Добавил" if ok else "Уже есть")

    async def cmd_addhh(self, arg: str) -> None:
        if not arg:
            await self.bot.send("Формат: /addhh разработка сайта")
            return
        ok = self.db.ensure_arm(f"hh:{arg}", "order", "hh", f"hh.ru: {arg}", {"query": arg}, origin="user")
        await self.bot.send("Добавил" if ok else "Уже есть")

    async def cmd_disable(self, arg: str) -> None:
        await self.bot.send("Выключил" if self.db.set_arm_active(arg, False) else "Нет такого id. /sources")

    async def cmd_enable(self, arg: str) -> None:
        await self.bot.send("Включил" if self.db.set_arm_active(arg, True) else "Нет такого id. /sources")

    async def cmd_threshold(self, arg: str) -> None:
        parts = arg.split()
        names = {"company": "notify_company", "order": "notify_order", "digest": "digest"}
        if len(parts) != 2 or parts[0] not in names or not parts[1].isdigit():
            await self.bot.send("Формат: /threshold company 70")
            return
        value = self.memory.set_threshold(names[parts[0]], int(parts[1]))
        await self.bot.send(f"Порог {parts[0]} = {value}")

    async def digest(self, force: bool = False) -> None:
        leads = self.db.leads("l.status = 'digest' AND l.digest_at IS NULL", order="l.score DESC", limit=30)
        if not leads:
            if force:
                await self.bot.send("Пусто — всё важное прислал сразу.")
            return
        await self.bot.send(f"<b>Сводка · {len(leads)}</b>", {"inline_keyboard": digest_rows(leads)})
        now = time.time()
        for lead in leads:
            self.db.set_lead_status(lead["id"], "digest", digest_at=now)

    async def chat_discovery(self) -> None:
        chats = await self.orders.discover_chats()
        if not chats:
            return
        lines = ["<b>Нашёл Telegram-чаты, где могут быть заказы</b>"]
        rows = []
        for chat in chats:
            members = f", {chat['members']} уч." if chat.get("members") else ""
            kind = "канал" if chat["broadcast"] else "чат"
            lines.append(f"• @{esc(chat['username'])} — {esc(chat['title'])} ({kind}{members}), запрос «{esc(chat['query'])}»")
            rows.append([{"text": f"Читать @{chat['username']}", "callback_data": f"addchat:{chat['username']}"[:64]}])
        await self.bot.send("\n".join(lines), {"inline_keyboard": rows})

    async def budget_alert(self) -> None:
        key = "budget_alert"
        today = datetime.now(self.tz).date().isoformat()
        if self.db.get(key) == today:
            return
        self.db.put(key, today)
        await self.bot.send(f"Дневной лимит на ИИ (${self.settings.daily_budget_usd}) исчерпан. Продолжу завтра, или подними DAILY_BUDGET_USD.")

    async def every(self, name: str, minutes: float, fn, delay: float = 0) -> None:
        await asyncio.sleep(delay)
        while True:
            if not self.paused():
                try:
                    result = await fn()
                    if result:
                        log.info("%s: %s", name, result)
                except BudgetExceeded:
                    await self.budget_alert()
                except Exception:
                    log.exception("%s failed", name)
            await asyncio.sleep(minutes * 60)

    async def daily(self, name: str, hour: int, fn) -> None:
        key = f"daily:{name}"
        while True:
            now = datetime.now(self.tz)
            today = now.date().isoformat()
            if now.hour >= hour and self.db.get(key) != today:
                self.db.put(key, today)
                try:
                    await fn()
                except BudgetExceeded:
                    await self.budget_alert()
                except Exception:
                    log.exception("%s failed", name)
            await asyncio.sleep(60)

    async def run(self) -> None:
        if not self.settings.openai_api_key:
            raise SystemExit("Нет OPENAI_API_KEY в .env")
        if not self.settings.bot_token:
            raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env")
        tg_ok = await self.tg.start()
        log.info("telegram reader: %s", "on" if tg_ok else "off")
        if not self.settings.owner_id:
            log.warning("TELEGRAM_OWNER_ID не задан: напиши боту /start, он пришлёт твой id")
        else:
            await self.bot.send("Агент запущен.", menu_keyboard())
        s = self.settings.section("schedule")
        tasks = [
            self.bot.poll_forever(),
            self.every("rss", s.get("rss_minutes", 10), self.orders.poll_rss, 5),
            self.every("hh", s.get("hh_minutes", 30), self.orders.poll_hh, 20),
            self.every("discovery", s.get("discovery_minutes", 60), self.companies.discover_once, 30),
            self.every("audit", s.get("audit_minutes", 5), self.companies.audit_once, 90),
            self.daily("digest", int(s.get("digest_hour", 19)), self.digest),
            self.daily("reflect", int(s.get("reflect_hour", 23)), lambda: reflect(self)),
        ]
        if tg_ok:
            tasks.append(self.every("telegram", s.get("telegram_minutes", 3), self.orders.poll_telegram, 10))
            tasks.append(self.daily("chat_discovery", int(s.get("chat_discovery_hour", 12)), self.chat_discovery))
        try:
            await asyncio.gather(*tasks)
        finally:
            await self.close()

    async def close(self) -> None:
        await self.companies.close()
        await self.tg.stop()
        await self.bot.close()
        await self.http.aclose()
