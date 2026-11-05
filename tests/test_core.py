import asyncio
import random
import time
from datetime import date
from pathlib import Path

import httpx

from leadhunter import settings as settings_module
from leadhunter.audit import Audit, analyze_html, audit_site
from leadhunter.db import DB
from leadhunter.discovery import Company, overpass_query, parse_overpass, parse_twogis, parse_yandex
from leadhunter.formatting import company_card, keyboard, order_card
from leadhunter.memory import Memory
from leadhunter.orders import Orders, parse_feed, parse_hh
from leadhunter.reflect import apply
from leadhunter.util import domain_of, keyword_hit, normalize_url


OLD_SITE = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html><head><title>Стоматология Улыбка</title>
<meta name="generator" content="Joomla! 1.5 - Open Source Content Management">
<script src="/js/jquery-1.7.2.min.js"></script>
</head><body><table><tr><td><font color=red>Акция!</font></td></tr></table>
<table><tr><td>Лечение</td></tr></table><table><tr><td>Цены</td></tr></table><table><tr><td>Контакты</td></tr></table>
<center>Добро пожаловать</center>
<embed src="banner.swf">
<p>Мы лечим зубы с 1998 года. Звоните: +7 900 000-00-00. Пишите <a href="mailto:info@ulybka.ru">info@ulybka.ru</a></p>
<p>""" + "Текст о клинике. " * 40 + """</p>
<p>© 2014 Стоматология Улыбка</p></body></html>"""

MODERN_SITE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Студия</title><meta name="description" content="Описание">
</head><body><div><div><h1>Привет</h1><p>""" + "Нормальный современный текст. " * 30 + """</p><footer>© 2026</footer></div></div></body></html>"""


def make_settings(tmp_path: Path):
    s = settings_module.load(config_path=str(tmp_path / "missing.yaml"), env_file=str(tmp_path / "none.env"))
    s.data_dir = tmp_path
    s.config["market"]["cities"] = ["Казань"]
    s.config["market"]["niches"] = ["стоматология", "автосервис"]
    s.config["orders"]["hh_queries"] = ["telegram бот"]
    return s


def test_url_helpers():
    assert normalize_url("example.ru") == "http://example.ru"
    assert normalize_url("  https://www.Example.ru/path ") == "https://www.Example.ru/path"
    assert normalize_url("nonsense") is None
    assert domain_of("https://www.Example.ru/a") == "example.ru"
    assert keyword_hit("Нужен Telegram бот", ["бот"], []) is True
    assert keyword_hit("Сделаю сайт недорого", ["сайт"], ["сделаю сайт"]) is False


def test_analyze_old_site():
    audit = Audit(input_url="http://ulybka.ru", domain="ulybka.ru")
    analyze_html(audit, OLD_SITE, today=date(2026, 11, 5))
    codes = {s.code for s in audit.signals}
    assert {"no_viewport", "old_copyright", "old_jquery", "flash", "deprecated_tags", "table_layout", "old_doctype", "old_cms"} <= codes
    assert "info@ulybka.ru" in audit.emails
    assert audit.score >= 80


def test_analyze_modern_site():
    audit = Audit(input_url="https://studio.ru", domain="studio.ru")
    analyze_html(audit, MODERN_SITE, today=date(2026, 11, 5))
    assert audit.score < 5, [s.code for s in audit.signals]


def test_audit_site_with_mock_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.scheme == "https":
            raise httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED")
        return httpx.Response(200, text=OLD_SITE, headers={"content-type": "text/html; charset=utf-8"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await audit_site(client, "ulybka.ru", {"pagespeed": False, "respect_robots": True})

    audit = asyncio.run(run())
    codes = {s.code for s in audit.signals}
    assert audit.reachable
    assert "ssl_error" in codes
    assert "no_viewport" in codes


def test_audit_site_down():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Name or service not known")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await audit_site(client, "dead.ru", {"pagespeed": False, "respect_robots": False})

    audit = asyncio.run(run())
    assert not audit.reachable
    assert any(s.code == "down" for s in audit.signals)


def test_overpass_parse_and_query():
    query = overpass_query('Санкт-"Петербург', ["amenity=dentist"], 100)
    assert '\\"' in query and 'nwr["amenity"="dentist"]' in query
    data = {
        "elements": [
            {"type": "node", "id": 1, "tags": {"name": "Улыбка", "amenity": "dentist", "website": "ulybka.ru", "phone": "+7 1"}},
            {"type": "way", "id": 2, "tags": {"name": "Без сайта", "amenity": "dentist", "contact:vk": "https://vk.com/x"}},
            {"type": "node", "id": 3, "tags": {"amenity": "dentist"}},
        ]
    }
    companies = parse_overpass(data, "Казань", "стоматология")
    assert len(companies) == 2
    assert companies[0].website == "http://ulybka.ru"
    assert companies[1].socials == ["https://vk.com/x"]
    assert companies[0].key([]) == "site:ulybka.ru"
    assert companies[1].key([]).startswith("nosite:osm:")


def test_twogis_and_yandex_parse():
    twogis = {
        "result": {
            "items": [
                {
                    "id": "70000001_abc",
                    "name": "Автосервис Мотор",
                    "address_name": "ул. Ленина, 1",
                    "rubrics": [{"name": "Автосервисы"}],
                    "contact_groups": [{"contacts": [{"type": "website", "text": "motor.ru", "value": "http://link.2gis.ru/x"}, {"type": "phone", "value": "+79000000000"}]}],
                }
            ]
        }
    }
    c = parse_twogis(twogis, "Казань", "автосервис")[0]
    assert c.website == "http://motor.ru" and c.phone == "+79000000000" and c.pid == "70000001"
    yandex = {"features": [{"properties": {"CompanyMetaData": {"id": "1", "name": "Ремонт", "url": "remont.ru", "Phones": [{"formatted": "+7 2"}], "Categories": [{"name": "Ремонт"}]}}}]}
    y = parse_yandex(yandex, "Казань", "ремонт")[0]
    assert y.website == "http://remont.ru" and y.phone == "+7 2"


def test_memory_bootstrap_and_bandit(tmp_path):
    s = make_settings(tmp_path)
    db = DB(":memory:")
    memory = Memory(db, s, rng=random.Random(1))
    memory.bootstrap()
    arms = db.arms(kind="company")
    assert {a["id"] for a in arms} == {"niche:стоматология|Казань", "niche:автосервис|Казань"}
    good, bad = "niche:стоматология|Казань", "niche:автосервис|Казань"
    for i in range(10):
        lid = db.add_lead("company", good, "osm", f"g{i}", "g", None, {}, {}, 80)
        db.set_lead_status(lid, "sent", notified_at=time.time())
        db.set_feedback(lid, "taken")
        lid = db.add_lead("company", bad, "osm", f"b{i}", "b", None, {}, {}, 80)
        db.set_lead_status(lid, "sent", notified_at=time.time())
        db.set_feedback(lid, "trash")
    picks = [memory.pick_arms([good, bad], 1)[0] for _ in range(200)]
    assert picks.count(good) > 180


def test_rules_directives_thresholds(tmp_path):
    s = make_settings(tmp_path)
    db = DB(":memory:")
    memory = Memory(db, s)
    memory.bootstrap()
    memory.set_rules(["[компании] правило 1"], "test")
    memory.set_rules(["[компании] правило 2", "[заказы] правило 3"], "test")
    assert memory.rules() == ["[компании] правило 2", "[заказы] правило 3"]
    assert memory.rollback_rules()
    assert memory.rules() == ["[компании] правило 1"]
    did = memory.add_directive("не присылай салоны красоты")
    assert "салоны красоты" in memory.context("company")
    assert memory.remove_directive(did)
    assert memory.set_threshold("notify_company", 200) == 90
    for i in range(10):
        lid = db.add_lead("company", "niche:стоматология|Казань", "osm", f"t{i}", "t", None, {}, {}, 70)
        db.set_lead_status(lid, "sent", notified_at=time.time())
        db.set_feedback(lid, "trash")
    memory.set_threshold("notify_company", 65)
    changes = memory.adapt_thresholds()
    assert changes["notify_company"][1] == 70


def test_feed_and_hh_parsing():
    rss = """<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>t</title>
    <item><title><![CDATA[Нужен сайт для кофейни]]></title><link>https://www.fl.ru/projects/1/x.html</link>
    <description><![CDATA[Сделать лендинг на Tilda, бюджет 30000]]></description><pubDate>Thu, 05 Nov 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    arm = {"id": "rss:x", "label": "FL.ru"}
    posts = parse_feed(rss.encode(), arm)
    assert posts[0].title == "Нужен сайт для кофейни"
    assert "Tilda" in posts[0].text and posts[0].published
    hh = {"items": [{"id": "9", "name": "Разработчик Telegram-бота (проект)", "alternate_url": "https://hh.ru/vacancy/9", "snippet": {"requirement": "Опыт с <highlighttext>ботами</highlighttext>"}, "employer": {"name": "ООО Ромашка"}, "published_at": "2026-11-05T10:00:00+0300"}]}
    posts = parse_hh(hh, {"id": "hh:bot", "label": "hh.ru: bot"})
    assert posts[0].external_id == "hh:9" and "ботами" in posts[0].text and not posts[0].prefilter


class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def json(self, system, user, schema, name, image=None, max_tokens=2000):
        self.calls += 1
        return self.answer(user)


def test_orders_pipeline_dedup_and_prefilter(tmp_path):
    s = make_settings(tmp_path)
    db = DB(":memory:")
    memory = Memory(db, s)
    memory.bootstrap()
    delivered = []

    async def deliver(lead_id):
        delivered.append(lead_id)

    def answer(user):
        return {"items": [{"idx": 0, "is_order": True, "score": 85, "category": "bot", "summary": "Бот для записи", "budget": "20000", "deadline": "", "contact": "@client", "reasoning": "профиль"}]}

    llm = FakeLLM(answer)
    orders = Orders(db, memory, llm, s, None, None, deliver)
    from leadhunter.orders import Post

    now = time.time()
    posts = [
        Post("rss:x", "rss", "FL.ru", "rss:x:1", "Нужен telegram бот", "Бот для записи клиентов", "https://fl.ru/1", None, now),
        Post("rss:x", "rss", "FL.ru", "rss:x:2", "Сделаю сайт", "Сделаю сайт недорого", "https://fl.ru/2", None, now),
        Post("rss:x", "rss", "FL.ru", "rss:x:3", "Продам гараж", "Гараж", "https://fl.ru/3", None, now),
        Post("rss:x", "rss", "FL.ru", "rss:x:4", "Старый бот", "нужен бот", "https://fl.ru/4", None, now - 10 * 86400),
    ]
    created = asyncio.run(orders.process(posts))
    assert created == 1 and llm.calls == 1 and len(delivered) == 1
    assert asyncio.run(orders.process(posts)) == 0
    lead = db.lead(delivered[0])
    text = order_card(lead)
    assert "Бот для записи" in text and "@client" in text and "FL.ru" in text


def test_cards_render():
    company_lead = {
        "id": 7,
        "kind": "company",
        "score": 82,
        "title": "Улыбка",
        "url": "http://ulybka.ru",
        "source": "osm",
        "data": {
            "company": {"name": "Улыбка <клиника>", "city": "Казань", "niche": "стоматология", "provider": "osm", "phone": "+7 1"},
            "audit": {"url": "http://ulybka.ru", "signals": ["нет мобильной версии"], "tech": ["Joomla! 1.5"], "emails": ["info@ulybka.ru"]},
        },
        "analysis": {"company_summary": "Частная клиника", "site_verdict": "Сайт 2014 года", "problems": ["нет мобильной версии"], "opportunity": "редизайн", "reasoning": "живой бизнес"},
    }
    text = company_card(company_lead)
    assert "Плохой сайт · 82/100" in text and "&lt;клиника&gt;" in text and "info@ulybka.ru" in text
    assert "Как нашлось" in text
    kb = keyboard(7, "taken")
    flat = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "✓ Взял" in flat


def test_reflection_apply_guards(tmp_path):
    s = make_settings(tmp_path)
    db = DB(":memory:")
    memory = Memory(db, s)
    memory.bootstrap()

    class AppStub:
        pass

    app = AppStub()
    app.db = db
    app.memory = memory
    result = {
        "rules": ["[компании] сети не присылать"],
        "add_niches": [{"niche": "Пекарни", "osm_tags": ["shop=bakery", "bad tag!"]}],
        "disable_arms": ["niche:автосервис|Казань"],
        "add_include_keywords": ["mini app"],
        "add_exclude_keywords": ["#реклама"],
        "remove_include_keywords": ["php"],
        "add_hh_queries": ["разработка чат-бота"],
        "chat_search_queries": ["заказы n8n"],
        "report": "ok",
    }
    changes = apply(app, result)
    assert memory.rules() == ["[компании] сети не присылать"]
    arm = db.arm("niche:пекарни|Казань")
    assert arm and arm["params"]["osm_tags"] == ["shop=bakery"]
    assert db.arm("niche:автосервис|Казань")["active"] == 1
    assert "php" not in memory.keywords("include")
    assert "#реклама" in memory.keywords("exclude")
    assert db.arm("hh:разработка чат-бота")
    assert "заказы n8n" in db.get("chat_queries")
    assert changes
