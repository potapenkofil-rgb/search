from .memory import VERDICTS
from .util import clip, esc

CATEGORY_NAMES = {
    "site": "сайт",
    "redesign": "редизайн",
    "landing": "лендинг",
    "shop": "интернет-магазин",
    "webapp": "веб-приложение",
    "bot": "бот",
    "automation": "автоматизация",
    "parser": "парсер",
    "integration": "интеграция",
    "ai": "AI",
    "other": "другое",
}

PROVIDER_NAMES = {"osm": "OpenStreetMap", "twogis": "2ГИС", "twogis_parser": "2ГИС (парсер)", "yandex": "Яндекс Карты", "manual": "ручной аудит"}


def keyboard(lead_id: int, chosen: str | None = None) -> dict:
    def button(code: str) -> dict:
        label = VERDICTS[code].capitalize()
        if chosen == code:
            label = "✓ " + label
        return {"text": label, "callback_data": f"fb:{lead_id}:{code}"}

    return {"inline_keyboard": [[button("trash"), button("interesting")], [button("taken"), button("won")]]}


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
    if lead.get("url"):
        lines.append(f"<a href=\"{esc(lead['url'])}\">Открыть оригинал</a>")
    lines.append(f"<i>Как нашлось: {esc(data.get('source_label', lead['source']))}</i>")
    return "\n".join(lines)


def card(lead: dict) -> str:
    return company_card(lead) if lead["kind"] == "company" else order_card(lead)


def digest_lines(leads: list[dict]) -> list[str]:
    out = []
    for lead in leads:
        a = lead["analysis"]
        if lead["kind"] == "company":
            what = clip(a.get("site_verdict") or a.get("opportunity") or "", 90)
        else:
            what = clip(a.get("summary") or lead["title"], 90)
        link = f" <a href=\"{esc(lead['url'])}\">ссылка</a>" if lead.get("url") else ""
        out.append(f"{lead['score']} · <b>{esc(clip(lead['title'], 60))}</b> — {esc(what)}{link} · /lead_{lead['id']}")
    return out
