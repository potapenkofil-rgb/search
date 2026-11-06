import asyncio

import httpx

from leadhunter import settings as settings_module
from leadhunter.app import App
from leadhunter.discovery import Company
from leadhunter.formatting import card

from test_core import OLD_SITE


class FakeBot:
    def __init__(self):
        self.sent = []
        self.owner_id = 1

    async def send(self, text, markup=None, chat_id=None, preview=False):
        self.sent.append((text, markup))
        return len(self.sent)

    async def send_photo(self, photo, caption, markup=None):
        self.sent.append((caption, markup))
        return len(self.sent)

    async def edit_markup(self, chat_id, message_id, markup):
        self.sent.append(("edit", markup))

    async def edit(self, message, text, markup=None):
        self.sent.append((text, markup))
        return True

    async def close(self):
        pass


def fake_llm(app):
    async def json(system, user, schema, name, image=None, max_tokens=2000):
        if name == "company":
            return {
                "score": 84,
                "is_lead": True,
                "company_summary": "Частная стоматология",
                "site_verdict": "Сайт из 2014 года",
                "problems": ["нет мобильной версии", "Flash-баннер"],
                "opportunity": "редизайн и мобильная версия",
                "reasoning": "живой бизнес, сайт устарел",
            }
        if name == "reflection":
            return {
                "rules": ["[компании] стоматологии со старыми сайтами — хорошие лиды"],
                "add_niches": [{"niche": "пекарни", "osm_tags": ["shop=bakery"]}],
                "disable_arms": [],
                "add_include_keywords": [],
                "add_exclude_keywords": [],
                "remove_include_keywords": [],
                "add_hh_queries": [],
                "chat_search_queries": [],
                "report": "Стоматологии заходят.",
            }
        return None

    app.llm.json = json


def make_app(tmp_path, monkeypatch):
    monkeypatch.setenv("LEADHUNTER_DATA", str(tmp_path / "data"))
    s = settings_module.load(config_path=str(tmp_path / "none.yaml"), env_file=str(tmp_path / "none.env"))
    s.config["market"]["cities"] = ["Казань"]
    s.config["market"]["niches"] = ["стоматология"]
    s.config["audit"]["pagespeed"] = False
    s.owner_id = 1
    app = App(s)
    app.bot = FakeBot()
    fake_llm(app)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=OLD_SITE, headers={"content-type": "text/html; charset=utf-8"})

    app.companies.http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return app


def test_company_flow_feedback_and_reflection(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    async def scenario():
        arm_id = "niche:стоматология|Казань"
        company = Company("osm", "n1", "Улыбка", "Казань", "стоматология", website="http://ulybka.ru", phone="+7 900")
        key = company.key([])
        assert app.db.enqueue_candidate(key, arm_id, "osm", company.name, company.website, company.to_dict())
        made = await app.companies.audit_once()
        assert made == 1
        text, markup = app.bot.sent[-1]
        assert "Улыбка" in text and "84" in text and "нет мобильной версии" in text
        assert len(text.splitlines()) < len(card(app.db.lead(int(markup["inline_keyboard"][0][0]["callback_data"].split(":")[1]))).splitlines())
        assert "Почему оценка такая" not in text and "Как нашлось" not in text
        assert "info@ulybka.ru" in text and "ulybka.ru" in text
        lead_id = int(markup["inline_keyboard"][0][0]["callback_data"].split(":")[1])
        assert "ulybka.ru" in markup["inline_keyboard"][-1][-1]["url"]
        await app.callback(f"x:{lead_id}:1", {"message": {"chat": {"id": 1}, "message_id": 5}})
        full, full_markup = app.bot.sent[-1]
        assert "Почему оценка такая" in full and "Частная стоматология" in full
        assert full_markup["inline_keyboard"][-1][0]["text"] == "Свернуть"
        note = await app.callback(f"fb:{lead_id}:taken", {"message": {"chat": {"id": 1}, "message_id": 5}})
        assert note == "Запомнил: взял"
        assert app.db.lead(lead_id)["verdict"] == "taken"
        await app.text("📊 Статистика", "1")
        assert "За сутки" in app.bot.sent[-1][0]
        await app.text("не присылай сетевые клиники", "1")
        assert app.memory.directives()[0]["text"] == "не присылай сетевые клиники"
        await app.command("/reflect", "1")
        report = app.bot.sent[-1][0]
        assert "Итог дня" in report and "Стоматологии заходят" in report and "пекарни" in report
        assert app.db.arm("niche:пекарни|Казань")
        await app.command("/lead_%d" % lead_id, "1")
        assert "Улыбка" in app.bot.sent[-1][0]
        await app.text("🔍 Проверить сайт", "1")
        assert app.db.get("pending") == "audit"
        await app.text("ulybka.ru", "1")
        assert app.db.get("pending") is None
        assert "ulybka.ru" in app.bot.sent[-1][0] or "Улыбка" in app.bot.sent[-1][0]
        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())


def test_low_score_goes_to_digest(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    async def scenario():
        lead_id = app.db.add_lead("order", "rss:https://www.fl.ru/rss/all.xml", "rss", "x1", "Поправить вёрстку", "https://fl.ru/1", {"post": {"text": "Поправить вёрстку"}, "source_label": "FL.ru"}, {"summary": "Поправить вёрстку", "category": "site"}, 45)
        await app.deliver(lead_id)
        assert app.db.lead(lead_id)["status"] == "digest"
        await app.digest()
        text, markup = app.bot.sent[-1]
        assert "Сводка · 1" in text
        assert markup["inline_keyboard"][0][0]["callback_data"] == f"x:{lead_id}:0"
        await app.digest(force=True)
        assert "Пусто" in app.bot.sent[-1][0]
        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())


def test_hourly_alert_cap(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.settings.config["thresholds"]["max_alerts_per_hour"] = 2

    async def scenario():
        ids = []
        for i in range(4):
            ids.append(app.db.add_lead("order", "hh:x", "hh", f"h{i}", f"Заказ {i}", None, {"post": {}, "source_label": "hh"}, {"summary": "бот", "category": "bot"}, 90))
        for lead_id in ids:
            await app.deliver(lead_id)
        statuses = [app.db.lead(i)["status"] for i in ids]
        assert statuses == ["sent", "sent", "digest", "digest"]
        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())


def test_removed_config_feed_is_disabled(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.db.ensure_arm("rss:https://www.fl.ru/rss/all.xml", "order", "rss", "FL.ru", {"url": "https://www.fl.ru/rss/all.xml"})
    app.memory.bootstrap()
    assert app.db.arm("rss:https://www.fl.ru/rss/all.xml")["active"] == 0
    asyncio.run(app.http.aclose())
    asyncio.run(app.companies.http.aclose())


def test_reflection_can_call_parser(tmp_path, monkeypatch):
    import sys
    from leadhunter.reflect import run_parser_requests

    app = make_app(tmp_path, monkeypatch)
    script = tmp_path / "p.py"
    script.write_text("import json\nprint(json.dumps([{'name': 'Шиномонтаж Колесо', 'phone': '+7 5'}], ensure_ascii=False))\n", encoding="utf-8")
    app.companies.parser.cfg = {"enabled": True, "command": f"{sys.executable} {script} {{niche}} {{city}}", "daily_limit": 100, "per_request_limit": 20}

    async def scenario():
        changes = await run_parser_requests(app, [{"niche": "шиномонтаж", "city": "Казань", "limit": 10}])
        assert changes and "1 компаний" in changes[0]
        row = app.db.one("SELECT * FROM candidates WHERE provider = 'twogis_parser'")
        assert row["name"] == "Шиномонтаж Колесо" and row["arm_id"] == "niche:шиномонтаж|Казань"
        await app.command("/parse шиномонтаж | Казань 5", "1")
        assert "в очередь" in app.bot.sent[-1][0]
        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())


def test_menu_buttons_and_thresholds(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    async def scenario():
        await app.text("⚙️ Ещё", "1")
        rows = app.bot.sent[-1][1]["inline_keyboard"]
        flat = {b["text"]: b["callback_data"] for row in rows for b in row}
        assert flat["Пауза"] == "m:pause"
        await app.callback("m:pause", {})
        assert app.paused() is True
        assert "Пауза" in app.bot.sent[-1][0]
        rows = app.bot.sent[-1][1]["inline_keyboard"]
        assert any(b["text"] == "Продолжить" for row in rows for b in row)
        await app.callback("m:resume", {})
        assert app.paused() is False

        await app.callback("m:thresholds", {})
        before = app.memory.threshold("notify_company")
        await app.callback("t:notify_company:5", {"message": {"chat": {"id": 1}, "message_id": 9}})
        assert app.memory.threshold("notify_company") == before + 5

        await app.callback("m:ask:addniche", {})
        assert app.db.get("pending") == "addniche"
        await app.text("пекарни", "1")
        assert app.db.arm("niche:пекарни|Казань")

        await app.text("не присылай сетевые клиники", "1")
        assert app.memory.directives()[0]["text"] == "не присылай сетевые клиники"
        await app.text("🧠 Правила", "1")
        rows = app.bot.sent[-1][1]["inline_keyboard"]
        did = app.memory.directives()[0]["id"]
        assert rows[0][0]["callback_data"] == f"d:{did}"
        await app.callback(f"d:{did}", {})
        assert app.memory.directives() == []

        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())


def test_command_clears_pending_input(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)

    async def scenario():
        await app.callback("m:ask:addcity", {})
        assert app.db.get("pending") == "addcity"
        await app.command("/stats", "1")
        assert app.db.get("pending") is None
        await app.text("просто заметка", "1")
        assert app.memory.directives()[0]["text"] == "просто заметка"
        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())
