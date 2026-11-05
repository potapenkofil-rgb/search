import asyncio

import httpx

from leadhunter import settings as settings_module
from leadhunter.app import App
from leadhunter.discovery import Company

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
        assert "Плохой сайт · 84/100" in text and "Улыбка" in text and "нет мобильной версии" in text
        lead_id = int(markup["inline_keyboard"][0][0]["callback_data"].split(":")[1])
        note = await app.callback(f"fb:{lead_id}:taken", {"message": {"chat": {"id": 1}, "message_id": 5}})
        assert note == "Запомнил: взял"
        assert app.db.lead(lead_id)["verdict"] == "taken"
        await app.command("/stats", "1")
        assert "Статистика" in app.bot.sent[-1][0]
        await app.text("не присылай сетевые клиники", "1")
        assert app.memory.directives()[0]["text"] == "не присылай сетевые клиники"
        await app.command("/reflect", "1")
        report = app.bot.sent[-1][0]
        assert "Итог дня" in report and "Стоматологии заходят" in report and "пекарни" in report
        assert app.db.arm("niche:пекарни|Казань")
        await app.command("/lead_%d" % lead_id, "1")
        assert "Улыбка" in app.bot.sent[-1][0]
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
        assert "Сводка" in app.bot.sent[-1][0] and f"/lead_{lead_id}" in app.bot.sent[-1][0]
        await app.digest(force=True)
        assert "пусто" in app.bot.sent[-1][0]
        await app.companies.http.aclose()
        await app.http.aclose()

    asyncio.run(scenario())
