import random
import time

from .db import DB, dumps, loads
from .settings import Settings


VERDICTS = {
    "trash": "мусор",
    "interesting": "интересно",
    "taken": "взял",
    "won": "сделка",
}

GOOD = ("interesting", "taken", "won")


class Memory:
    def __init__(self, db: DB, settings: Settings, rng: random.Random | None = None):
        self.db = db
        self.settings = settings
        self.rng = rng or random.Random()

    def bootstrap(self) -> None:
        market = self.settings.section("market")
        orders = self.settings.section("orders")
        for city in market.get("cities", []):
            for niche in market.get("niches", []):
                self.add_niche_arm(niche, city, origin="config")
        feeds = {f"rss:{feed['url']}" for feed in orders.get("rss", [])}
        for feed in orders.get("rss", []):
            self.db.ensure_arm(f"rss:{feed['url']}", "order", "rss", feed.get("name") or feed["url"], {"url": feed["url"]})
        for arm in self.db.arms(kind="order", source="rss"):
            if arm["origin"] == "config" and arm["id"] not in feeds:
                self.db.set_arm_active(arm["id"], False)
        for chat in orders.get("telegram_chats", []):
            self.add_chat_arm(chat, origin="config")
        queries = {f"hh:{q}" for q in orders.get("hh_queries", [])}
        for query in orders.get("hh_queries", []):
            self.db.ensure_arm(f"hh:{query}", "order", "hh", f"hh.ru: {query}", {"query": query})
        for arm in self.db.arms(kind="order", source="hh"):
            if arm["origin"] == "config" and arm["id"] not in queries:
                self.db.set_arm_active(arm["id"], False)
        self.db.ensure_arm("manual", "company", "manual", "ручной аудит", {}, origin="user", active=0)
        if self.db.get("keywords:include") is None:
            self.db.put("keywords:include", orders.get("include_keywords", []))
        if self.db.get("keywords:exclude") is None:
            self.db.put("keywords:exclude", orders.get("exclude_keywords", []))
        if self.db.get("chat_queries") is None:
            self.db.put("chat_queries", orders.get("chat_search_queries", []))
        thresholds = self.settings.section("thresholds")
        for name in ("notify_company", "notify_order", "digest"):
            if self.db.get(f"threshold:{name}") is None:
                self.db.put(f"threshold:{name}", int(thresholds.get(name, 60)))

    def add_niche_arm(self, niche: str, city: str, origin: str = "user", osm_tags: list[str] | None = None) -> bool:
        params = {"niche": niche, "city": city}
        if osm_tags:
            params["osm_tags"] = osm_tags
        return self.db.ensure_arm(f"niche:{niche}|{city}", "company", "niche", f"{niche} · {city}", params, origin=origin)

    def add_chat_arm(self, chat: str, origin: str = "user") -> bool:
        username = chat.strip().lstrip("@").split("/")[-1]
        if not username:
            return False
        return self.db.ensure_arm(f"tg:{username.lower()}", "order", "telegram", f"Telegram @{username}", {"username": username}, origin=origin)

    def cities(self) -> list[str]:
        rows = self.db.all("SELECT DISTINCT json_extract(params, '$.city') AS c FROM arms WHERE source = 'niche'")
        return [r["c"] for r in rows if r["c"]]

    def niches(self) -> list[str]:
        rows = self.db.all("SELECT DISTINCT json_extract(params, '$.niche') AS n FROM arms WHERE source = 'niche' AND active = 1")
        return [r["n"] for r in rows if r["n"]]

    def arm_stats(self, kind: str | None = None, include_inactive: bool = False) -> list[dict]:
        stale = time.time() - 2 * 86400
        sql = """
            SELECT a.id, a.kind, a.source, a.label, a.active,
                COUNT(l.id) AS leads,
                SUM(CASE WHEN l.notified_at IS NOT NULL OR l.digest_at IS NOT NULL THEN 1 ELSE 0 END) AS shown,
                SUM(CASE WHEN f.verdict = 'trash' THEN 1 ELSE 0 END) AS trash,
                SUM(CASE WHEN f.verdict = 'interesting' THEN 1 ELSE 0 END) AS interesting,
                SUM(CASE WHEN f.verdict = 'taken' THEN 1 ELSE 0 END) AS taken,
                SUM(CASE WHEN f.verdict = 'won' THEN 1 ELSE 0 END) AS won,
                SUM(CASE WHEN f.verdict IS NULL AND l.notified_at IS NOT NULL AND l.notified_at < ? THEN 1 ELSE 0 END) AS ignored
            FROM arms a
            LEFT JOIN leads l ON l.arm_id = a.id
            LEFT JOIN feedback f ON f.lead_id = l.id
            WHERE a.source != 'manual'
        """
        args: list = [stale]
        if kind:
            sql += " AND a.kind = ?"
            args.append(kind)
        if not include_inactive:
            sql += " AND a.active = 1"
        sql += " GROUP BY a.id"
        out = []
        for row in self.db.all(sql, *args):
            item = {k: (row[k] or 0) if k not in ("id", "kind", "source", "label") else row[k] for k in row.keys()}
            alpha = 1 + item["interesting"] + 2 * item["taken"] + 4 * item["won"] + 0.05 * item["shown"]
            beta = 1 + item["trash"] + 0.3 * item["ignored"] + 0.05 * max(0, item["leads"] - item["shown"])
            item["alpha"] = alpha
            item["beta"] = beta
            item["mean"] = alpha / (alpha + beta)
            item["rated"] = item["trash"] + item["interesting"] + item["taken"] + item["won"]
            out.append(item)
        return out

    def pick_arms(self, arm_ids: list[str], k: int) -> list[str]:
        if not arm_ids:
            return []
        stats = {s["id"]: s for s in self.arm_stats()}
        scored = []
        for arm_id in arm_ids:
            s = stats.get(arm_id)
            alpha, beta = (s["alpha"], s["beta"]) if s else (1.0, 1.0)
            scored.append((self.rng.betavariate(alpha, beta), arm_id))
        scored.sort(reverse=True)
        return [arm_id for _, arm_id in scored[:k]]

    def rules(self) -> list[str]:
        row = self.db.one("SELECT items FROM rules WHERE active = 1 ORDER BY version DESC LIMIT 1")
        return loads(row["items"], []) if row else []

    def set_rules(self, items: list[str], reason: str) -> int:
        clean = [i.strip() for i in items if i and i.strip()][:25]
        return self.db.run("INSERT INTO rules(items, reason, active, created_at) VALUES(?, ?, 1, ?)", dumps(clean), reason, time.time())

    def rollback_rules(self) -> bool:
        rows = self.db.all("SELECT version FROM rules WHERE active = 1 ORDER BY version DESC LIMIT 2")
        if not rows:
            return False
        self.db.run("UPDATE rules SET active = 0 WHERE version = ?", rows[0]["version"])
        return True

    def directives(self) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT id, text FROM directives WHERE active = 1 ORDER BY id")]

    def add_directive(self, text: str) -> int:
        return self.db.run("INSERT INTO directives(text, active, created_at) VALUES(?, 1, ?)", text.strip()[:500], time.time())

    def remove_directive(self, directive_id: int) -> bool:
        cur = self.db.conn.execute("UPDATE directives SET active = 0 WHERE id = ?", (directive_id,))
        return cur.rowcount > 0

    def threshold(self, name: str) -> int:
        return int(self.db.get(f"threshold:{name}", self.settings.section("thresholds").get(name, 60)))

    def set_threshold(self, name: str, value: int) -> int:
        cfg = self.settings.section("thresholds")
        value = max(int(cfg.get("min_threshold", 40)), min(int(cfg.get("max_threshold", 90)), int(value)))
        self.db.put(f"threshold:{name}", value)
        return value

    def keywords(self, name: str) -> list[str]:
        return list(self.db.get(f"keywords:{name}", []))

    def update_keywords(self, name: str, add: list[str] | None = None, remove: list[str] | None = None) -> list[str]:
        current = self.keywords(name)
        low = {w.lower() for w in current}
        for word in add or []:
            word = word.strip().lower()
            if word and len(word) >= 3 and word not in low:
                current.append(word)
                low.add(word)
        drop = {w.strip().lower() for w in remove or []}
        current = [w for w in current if w.lower() not in drop]
        self.db.put(f"keywords:{name}", current)
        return current

    def feedback(self, lead_id: int, verdict: str) -> dict | None:
        if verdict not in VERDICTS:
            return None
        lead = self.db.lead(lead_id)
        if not lead:
            return None
        self.db.set_feedback(lead_id, verdict)
        return self.db.lead(lead_id)

    def examples(self, kind: str, n: int = 4) -> tuple[list[str], list[str]]:
        good = self.db.leads("l.kind = ? AND f.verdict IN ('interesting', 'taken', 'won')", kind, order="f.updated_at DESC", limit=n)
        bad = self.db.leads("l.kind = ? AND f.verdict = 'trash'", kind, order="f.updated_at DESC", limit=n)
        return [self._example(l) for l in good], [self._example(l) for l in bad]

    @staticmethod
    def _example(lead: dict) -> str:
        a = lead["analysis"]
        if lead["kind"] == "company":
            problems = "; ".join((a.get("problems") or [])[:3])
            niche = (lead["data"].get("company") or {}).get("niche", "")
            return f"[{VERDICTS.get(lead.get('verdict'), '?')}] {lead['score']} — {lead['title']} ({niche}): {problems}"
        return f"[{VERDICTS.get(lead.get('verdict'), '?')}] {lead['score']} — {a.get('category', '')}: {a.get('summary', '')[:160]}"

    def context(self, kind: str) -> str:
        parts = []
        directives = self.directives()
        if directives:
            parts.append("Директивы разработчика (важнее всего):\n" + "\n".join(f"- {d['text']}" for d in directives))
        tag = "[компании]" if kind == "company" else "[заказы]"
        rules = [r for r in self.rules() if r.startswith(tag) or not r.startswith("[")]
        if rules:
            parts.append("Выученные правила оценки:\n" + "\n".join(f"- {r}" for r in rules))
        good, bad = self.examples(kind)
        if good:
            parts.append("Лиды, которые разработчику понравились:\n" + "\n".join(f"- {g}" for g in good))
        if bad:
            parts.append("Лиды, которые он отметил как мусор:\n" + "\n".join(f"- {b}" for b in bad))
        return "\n\n".join(parts)

    def adapt_thresholds(self) -> dict:
        cfg = self.settings.section("thresholds")
        target = int(cfg.get("target_alerts_per_day", 15))
        since = time.time() - 7 * 86400
        changes = {}
        for kind, name in (("company", "notify_company"), ("order", "notify_order")):
            rows = self.db.leads("l.kind = ? AND l.notified_at > ?", kind, since, limit=1000)
            rated = [r for r in rows if r.get("verdict")]
            if len(rated) < 5:
                continue
            trash_rate = sum(1 for r in rated if r["verdict"] == "trash") / len(rated)
            per_day = len(rows) / 7
            current = self.threshold(name)
            new = current
            if trash_rate > 0.5:
                new = current + 5
            elif trash_rate < 0.2 and per_day < target:
                new = current - 3
            if new != current:
                changes[name] = (current, self.set_threshold(name, new), round(trash_rate, 2))
        return changes
