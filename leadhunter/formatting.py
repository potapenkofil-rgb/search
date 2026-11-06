import re

from .memory import VERDICTS
from .util import clip, esc

CATEGORY_NAMES = {
    "site": "сайт",
    "redesign": "редизайн",
    "landing": "лендинг",
    "shop": "магазин",
    "webapp": "веб-приложение",
    "bot": "бот",
    "automation": "автоматизация",
    "parser": "парсер",
    "integration": "интеграция",
    "ai": "AI",
    "other": "другое",
}

PROVIDER_NAMES = {
    "osm": "OpenStreetMap",
    "twogis": "2ГИС",
    "twogis_parser": "2ГИС (парсер)",
    "yandex": "Яндекс Карты",
    "manual": "ручной аудит",
}

VERDICT_ICONS = {"trash": "🗑", "interesting": "⭐", "taken": "✅", "won": "💰"}

MENU = [
    ["📊 Статистика", "📋 Сводка"],
    ["🎯 Источники", "🧠 Правила"],
    ["🔍 Проверить сайт", "⚙️ Ещё"],
]

MORE = [
    [("Разбор итогов", "m:reflect"), ("Пороги", "m:thresholds")],
    [("Добавить нишу", "m:ask:addniche"), ("Добавить город", "m:ask:addcity")],
    [("Добавить чат", "m:ask:addchat"), ("Парсер 2ГИС", "m:ask:parse")],
    [("Пауза", "m:pause"), ("Помощь", "m:help")],
]


def menu_keyboard() -> dict:
    return {
        "keyboard": [[{"text": t} for t in row] for row in MENU],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def more_keyboard(paused: bool) -> dict:
    rows = []
    for row in MORE:
        buttons = []
        for text, data in row:
            if data == "m:pause":
                text, data = ("Продолжить", "m:resume") if paused else ("Пауза", "m:pause")
            buttons.append({"text": text, "callback_data": data})
        rows.append(buttons)
    return {"inline_keyboard": rows}


def keyboard(lead_id: int, chosen: str | None = None, expanded: bool = False, url: str | None = None) -> dict:
    def button(code: str) -> dict:
        label = f"{VERDICT_ICONS[code]} {VERDICTS[code].capitalize()}"
        if chosen == code:
            label = "✓ " + VERDICTS[code].capitalize()
        return {"text": label, "callback_data": f"fb:{lead_id}:{code}"}

    rows = [[button("trash"), button("interesting")], [button("taken"), button("won")]]
    last = [{"text": "Свернуть" if expanded else "Подробнее", "callback_data": f"x:{lead_id}:{0 if expanded else 1}"}]
    if url:
        last.append({"text": "Открыть", "url": url})
    rows.append(last)
    return {"inline_keyboard": rows}


def company_brief(lead: dict) -> str:
    data = lead["data"]
    a = lead["analysis"]
    company = data.get("company") or {}
    audit = data.get("audit") or {}
    icon = "🏚" if audit else "📍"
    where = ", ".join(x for x in (company.get("city"), company.get("niche")) if x)
    head = f"{icon} <b>{esc(clip(company.get('name') or lead['title'], 60))}</b> · {lead['score']}"
    if where:
        head += f" · {esc(where)}"
    lines = [head]
    if audit:
        problems = (a.get("problems") or audit.get("signals") or [])[:3]
        verdict = clip(a.get("site_verdict") or "", 80)
        tail = ", ".join(clip(p, 40) for p in problems)
        lines.append(esc(f"{verdict}: {tail}" if verdict and tail else verdict or tail))
    else:
        socials = [re.sub(r"^https?://(www\.)?", "", x).rstrip("/") for x in (company.get("socials") or [])]
        lines.append("Сайта нет" + (f", только {esc(clip(', '.join(socials[:2]), 50))}" if socials else ""))
    if a.get("opportunity"):
        lines.append(f"🔧 {esc(clip(a['opportunity'], 90))}")
    return "\n".join(x for x in lines if x.strip())


def order_brief(lead: dict) -> str:
    a = lead["analysis"]
    category = CATEGORY_NAMES.get(a.get("category"), a.get("category") or "заказ")
    lines = [f"💼 <b>{esc(clip(a.get('summary') or lead['title'], 90))}</b>"]
    meta = [f"{esc(category)} · {lead['score']}"]
    if a.get("budget"):
        meta.append(esc(clip(a["budget"], 30)))
    if a.get("deadline"):
        meta.append(esc(clip(a["deadline"], 30)))
    if a.get("contact"):
        meta.append(esc(clip(a["contact"], 40)))
    lines.append(" · ".join(meta))
    return "\n".join(lines)


def brief(lead: dict) -> str:
    return company_brief(lead) if lead["kind"] == "company" else order_brief(lead)


def company_card(lead: dict) -> str:
    data = lead["data"]
    a = lead["analysis"]
    company = data.get("company") or {}
    audit = data.get("audit") or {}
    has_site = bool(audit)
    head = "Плохой сайт" if has_site else "Нет сайта"
    lines = [f"<b>{head} · {lead['score']}/100</b> · #{lead['id']}"]
    lines.append(f"<b>{esc(company.get('name') or lead['title'])}</b>")
    meta = ", ".join(x for x in (company.get("city"), company.get("category") or company.get("niche")) if x)
    if meta:
        lines.append(esc(meta))
    if company.get("address"):
        lines.append(esc(company["address"]))
    lines.append("")
    if a.get("company_summary"):
        lines.append(f"<b>Кто:</b> {esc(a['company_summary'])}")
    if has_site:
        lines.append(f"<b>Сайт:</b> {esc(audit.get('url') or company.get('website'))}")
        if a.get("site_verdict"):
            lines.append(f"<b>Вердикт:</b> {esc(a['site_verdict'])}")
        problems = a.get("problems") or audit.get("signals") or []
        if problems:
            lines.append("<b>Почему плохой:</b>")
            lines.extend(f"• {esc(p)}" for p in problems[:7])
        tech = audit.get("tech") or []
        if tech:
            lines.append(f"<b>Технологии:</b> {esc(', '.join(tech[:5]))}")
    else:
        socials = company.get("socials") or []
        lines.append("<b>Сайт:</b> нет" + (f", только {esc(', '.join(socials[:3]))}" if socials else ""))
    if a.get("opportunity"):
        lines.append(f"<b>Что можно сделать:</b> {esc(a['opportunity'])}")
    if a.get("reasoning"):
        lines.append(f"<b>Почему оценка такая:</b> {esc(a['reasoning'])}")
    contacts = []
    if company.get("phone"):
        contacts.append(esc(company["phone"]))
    emails = [company.get("email")] + list(audit.get("emails") or [])
    emails = [e for i, e in enumerate(emails) if e and e not in emails[:i]]
    if emails:
        contacts.append(esc(", ".join(emails[:2])))
    if contacts:
        lines.append(f"<b>Контакты:</b> {' · '.join(contacts)}")
    provider = PROVIDER_NAMES.get(company.get("provider"), company.get("provider") or "")
    lines.append(f"<i>Как нашлось: {esc(provider)}, поиск «{esc(company.get('niche', ''))}» в городе {esc(company.get('city', ''))}</i>")
    return "\n".join(lines)


def order_card(lead: dict) -> str:
    data = lead["data"]
    a = lead["analysis"]
    post = data.get("post") or {}
    category = CATEGORY_NAMES.get(a.get("category"), a.get("category") or "заказ")
    lines = [f"<b>Заказ · {esc(category)} · {lead['score']}/100</b> · #{lead['id']}"]
    if post.get("title"):
        lines.append(f"<b>{esc(clip(post['title'], 200))}</b>")
    lines.append("")
    if a.get("summary"):
        lines.append(f"<b>Нужно:</b> {esc(a['summary'])}")
    if a.get("budget"):
        lines.append(f"<b>Бюджет:</b> {esc(a['budget'])}")
    if a.get("deadline"):
        lines.append(f"<b>Срок:</b> {esc(a['deadline'])}")
    if a.get("contact"):
        lines.append(f"<b>Контакт:</b> {esc(a['contact'])}")
    elif post.get("author"):
        lines.append(f"<b>Автор:</b> {esc(post['author'])}")
    if a.get("reasoning"):
        lines.append(f"<b>Почему подходит:</b> {esc(a['reasoning'])}")
    text = clip(post.get("text"), 700)
    if text:
        lines.append("")
        lines.append(f"<blockquote>{esc(text)}</blockquote>")
    lines.append(f"<i>Как нашлось: {esc(data.get('source_label', lead['source']))}</i>")
    return "\n".join(lines)


def card(lead: dict) -> str:
    return company_card(lead) if lead["kind"] == "company" else order_card(lead)


def lead_text(lead: dict, expanded: bool) -> str:
    return card(lead) if expanded else brief(lead)


def digest_rows(leads: list[dict]) -> list[list[dict]]:
    rows = []
    for lead in leads[:30]:
        icon = "🏚" if lead["kind"] == "company" else "💼"
        if lead["kind"] == "company":
            name = (lead["data"].get("company") or {}).get("name") or lead["title"]
        else:
            name = lead["analysis"].get("summary") or lead["title"]
        rows.append([{"text": f"{icon} {lead['score']} · {clip(name, 40)}", "callback_data": f"x:{lead['id']}:0"}])
    return rows
