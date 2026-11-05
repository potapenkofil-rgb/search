import json
import re
import time

from .db import dumps
from .memory import VERDICTS
from .prompts import REFLECT_SCHEMA, REFLECT_SYSTEM
from .util import clip, esc


def _sample(lead: dict) -> dict:
    a = lead["analysis"]
    item = {
        "verdict": lead.get("verdict"),
        "score": lead["score"],
        "kind": lead["kind"],
        "arm": lead["arm_id"],
        "title": clip(lead["title"], 80),
    }
    if lead["kind"] == "company":
        company = lead["data"].get("company") or {}
        item["niche"] = company.get("niche")
        item["city"] = company.get("city")
        item["problems"] = (a.get("problems") or [])[:4]
        item["opportunity"] = a.get("opportunity")
    else:
        item["category"] = a.get("category")
        item["summary"] = clip(a.get("summary"), 200)
        item["budget"] = a.get("budget")
    return item


def build_prompt(app, since: float) -> tuple[str, dict]:
    db = app.db
    memory = app.memory
    stats = memory.arm_stats(include_inactive=False)
    active = sorted([s for s in stats if s["leads"]], key=lambda s: -s["leads"])[:40]
    rated = db.leads("f.updated_at > ?", since, order="f.updated_at DESC", limit=60)
    new_leads = db.all("SELECT kind, COUNT(*) AS n, SUM(CASE WHEN notified_at IS NOT NULL THEN 1 ELSE 0 END) AS sent FROM leads WHERE created_at > ? GROUP BY kind", since)
    facts = {
        "period_hours": round((time.time() - since) / 3600),
        "new_leads": {r["kind"]: {"found": r["n"], "sent": r["sent"]} for r in new_leads},
        "rated_count": len(rated),
        "verdicts": {v: sum(1 for l in rated if l.get("verdict") == v) for v in VERDICTS},
    }
    sections = []
    directives = memory.directives()
    if directives:
        sections.append("Директивы пользователя:\n" + "\n".join(f"- {d['text']}" for d in directives))
    sections.append("Текущие правила:\n" + ("\n".join(f"- {r}" for r in memory.rules()) or "(пока нет)"))
    sections.append("Сводка за период:\n" + json.dumps(facts, ensure_ascii=False))
    sections.append(
        "Статистика источников и ниш (id, label, leads найдено, shown показано, оценки, mean — апостериорное качество):\n"
        + "\n".join(
            json.dumps({k: (round(s[k], 3) if isinstance(s[k], float) else s[k]) for k in ("id", "label", "leads", "shown", "trash", "interesting", "taken", "won", "ignored", "mean")}, ensure_ascii=False)
            for s in active
        )
    )
    sections.append("Оценённые лиды за период:\n" + ("\n".join(json.dumps(_sample(l), ensure_ascii=False) for l in rated) or "(оценок нет)"))
    sections.append("Активные ниши: " + ", ".join(memory.niches()))
    sections.append("Города: " + ", ".join(memory.cities()))
    sections.append("Ключевые слова префильтра: " + ", ".join(memory.keywords("include")[:80]))
    sections.append("Стоп-слова: " + ", ".join(memory.keywords("exclude")[:60]))
    sections.append("Запросы hh.ru: " + ", ".join(a["params"]["query"] for a in db.arms(kind="order", source="hh")))
    sections.append("Запросы поиска чатов: " + ", ".join(db.get("chat_queries", [])))
    return "\n\n".join(sections), facts


def apply(app, result: dict) -> list[str]:
    db = app.db
    memory = app.memory
    changes = []
    rules = [r for r in result.get("rules") or [] if isinstance(r, str) and r.strip()]
    if rules and rules != memory.rules():
        memory.set_rules(rules, "reflection")
        changes.append(f"правила обновлены ({len(rules)} шт.)")
    cities = memory.cities()
    for item in (result.get("add_niches") or [])[:3]:
        niche = (item.get("niche") or "").strip().lower()
        tags = [t.strip() for t in item.get("osm_tags") or [] if re.match(r"^[a-z_:]+=[\w:-]+$", t.strip())]
        if not niche or not tags:
            continue
        added = sum(1 for city in cities if memory.add_niche_arm(niche, city, origin="reflection", osm_tags=tags))
        if added:
            changes.append(f"новая ниша «{niche}» ({', '.join(tags)})")
    stats = {s["id"]: s for s in memory.arm_stats()}
    for arm_id in result.get("disable_arms") or []:
        s = stats.get(arm_id)
        if not s or s["rated"] < 8 or s["trash"] / max(1, s["rated"]) < 0.75:
            continue
        if db.set_arm_active(arm_id, False):
            changes.append(f"отключил источник {s['label']}")
    add_inc = result.get("add_include_keywords") or []
    rm_inc = result.get("remove_include_keywords") or []
    if add_inc or rm_inc:
        memory.update_keywords("include", add_inc, rm_inc)
        if add_inc:
            changes.append("ключевые слова +" + ", ".join(add_inc[:8]))
        if rm_inc:
            changes.append("ключевые слова −" + ", ".join(rm_inc[:8]))
    add_exc = result.get("add_exclude_keywords") or []
    if add_exc:
        memory.update_keywords("exclude", add_exc)
        changes.append("стоп-слова +" + ", ".join(add_exc[:8]))
    for query in (result.get("add_hh_queries") or [])[:2]:
        query = query.strip()
        if query and db.ensure_arm(f"hh:{query}", "order", "hh", f"hh.ru: {query}", {"query": query}, origin="reflection"):
            changes.append(f"запрос hh.ru «{query}»")
    queries = db.get("chat_queries", [])
    for query in (result.get("chat_search_queries") or [])[:3]:
        query = query.strip()
        if query and query not in queries:
            queries.append(query)
            changes.append(f"поиск чатов «{query}»")
    db.put("chat_queries", queries[-20:])
    return changes


async def reflect(app) -> str:
    db = app.db
    since = float(db.get("last_reflection", time.time() - 86400))
    prompt, facts = build_prompt(app, since)
    changes = []
    report = ""
    worth = facts["rated_count"] > 0 or sum(v.get("found", 0) for v in facts["new_leads"].values()) > 0
    if worth:
        result = await app.llm.json(REFLECT_SYSTEM, prompt, REFLECT_SCHEMA, "reflection", max_tokens=3000)
        if result:
            changes = apply(app, result)
            report = result.get("report") or ""
    for name, (old, new, trash_rate) in app.memory.adapt_thresholds().items():
        label = "компании" if name == "notify_company" else "заказы"
        changes.append(f"порог «{label}» {old} → {new} (мусора {int(trash_rate * 100)}%)")
    db.put("last_reflection", time.time())
    db.run("INSERT INTO reflections(created_at, report) VALUES(?, ?)", time.time(), dumps({"facts": facts, "report": report, "changes": changes}))
    usage = db.usage()
    lines = ["<b>Итог дня</b>"]
    for kind, label in (("company", "Компании"), ("order", "Заказы")):
        item = facts["new_leads"].get(kind) or {}
        lines.append(f"{label}: найдено {item.get('found', 0)}, отправлено {item.get('sent', 0)}")
    v = facts["verdicts"]
    lines.append(f"Твои оценки: мусор {v['trash']}, интересно {v['interesting']}, взял {v['taken']}, сделка {v['won']}")
    lines.append(f"Расход за сегодня: ${usage['cost']:.3f}, за месяц: ${db.usage_month():.2f}")
    if report:
        lines.append("")
        lines.append(f"<b>Что понял:</b> {esc(report)}")
    if changes:
        lines.append("")
        lines.append("<b>Что изменил:</b>")
        lines.extend(f"• {esc(c)}" for c in changes)
    if not worth:
        lines.append("")
        lines.append("Нового мало, учиться пока не на чем. Оценивай лиды кнопками — так я быстрее пойму, что тебе нужно.")
    text = "\n".join(lines)
    await app.bot.send(text)
    return text
