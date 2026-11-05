import asyncio
import csv
import io
import json
import re
import shlex
from datetime import date

from .db import DB
from .discovery import Company
from .util import log, normalize_url


FIELD_KEYS = {
    "id": ["id", "firm_id", "branch_id", "uid"],
    "name": ["name", "title", "firm", "company", "название", "наименование"],
    "phone": ["phone", "phones", "tel", "телефон", "телефоны"],
    "email": ["email", "emails", "mail", "почта"],
    "address": ["address", "address_name", "addr", "адрес"],
    "category": ["category", "categories", "rubric", "rubrics", "рубрика", "рубрики", "категория"],
    "website": ["website", "site", "url", "сайт"],
    "socials": ["socials", "vk", "instagram", "telegram", "whatsapp", "соцсети"],
    "city": ["city", "город"],
}


def _clean(value: str, limit: int = 80) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return value.strip()[:limit]


def build_args(template: str, values: dict) -> list[str]:
    args = []
    for token in shlex.split(template):
        for key, value in values.items():
            token = token.replace("{" + key + "}", str(value))
        args.append(token)
    return args


def _pick(record: dict, keys: list[str]):
    low = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        value = low.get(key.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def _flat(value) -> str:
    if isinstance(value, list):
        return ", ".join(_flat(v) for v in value if v)
    if isinstance(value, dict):
        return ", ".join(_flat(v) for v in value.values() if v)
    return str(value).strip()


def parse_records(text: str) -> list[dict]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("items", "results", "data", "companies"):
                if isinstance(data.get(key), list):
                    return [r for r in data[key] if isinstance(r, dict)]
            return [data]
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
    except ValueError:
        pass
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    rows.append(item)
            except ValueError:
                continue
    if rows:
        return rows
    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        return []
    return [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]


def to_companies(records: list[dict], niche: str, city: str, fields: dict | None = None) -> list[Company]:
    keys = {k: (fields or {}).get(k) or v for k, v in FIELD_KEYS.items()}
    out = []
    for i, record in enumerate(records):
        name = _pick(record, keys["name"])
        if not name:
            continue
        name = _flat(name)
        ident = _pick(record, keys["id"])
        socials_raw = _pick(record, keys["socials"])
        socials = [s.strip() for s in re.split(r"[,\s]+", _flat(socials_raw)) if s.strip().startswith("http")] if socials_raw else []
        phone = _pick(record, keys["phone"])
        email = _pick(record, keys["email"])
        out.append(
            Company(
                provider="twogis_parser",
                pid=_flat(ident) if ident else f"{name}|{_flat(_pick(record, keys['address']) or i)}",
                name=name,
                city=_flat(_pick(record, keys["city"]) or city),
                niche=niche,
                category=_flat(_pick(record, keys["category"]) or ""),
                address=_flat(_pick(record, keys["address"]) or ""),
                website=normalize_url(_flat(_pick(record, keys["website"]) or "")) if _pick(record, keys["website"]) else None,
                phone=_flat(phone).split(",")[0].strip() if phone else None,
                email=_flat(email).split(",")[0].strip() if email else None,
                socials=socials[:4],
            )
        )
    return out


class ParserTool:
    def __init__(self, cfg: dict, db: DB):
        self.cfg = cfg or {}
        self.db = db
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and self.cfg.get("command"))

    def used_today(self) -> int:
        return int(self.db.get(f"parser:{date.today().isoformat()}", 0))

    def left_today(self) -> int:
        return max(0, int(self.cfg.get("daily_limit", 300)) - self.used_today())

    def describe(self) -> str:
        return (
            f"Доступен инструмент: парсер 2ГИС, находит компании БЕЗ сайта по нише и городу. "
            f"Осталось на сегодня: {self.left_today()} компаний, за один запрос максимум {int(self.cfg.get('per_request_limit', 50))}."
        )

    async def run(self, niche: str, city: str, limit: int) -> list[Company]:
        if not self.enabled:
            return []
        limit = max(1, min(int(limit or 20), int(self.cfg.get("per_request_limit", 50)), self.left_today()))
        if self.left_today() <= 0:
            return []
        niche, city = _clean(niche), _clean(city)
        args = build_args(self.cfg["command"], {"niche": niche, "query": niche, "city": city, "limit": limit})
        async with self.lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cfg.get("workdir") or None,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=float(self.cfg.get("timeout_seconds", 900)))
            except asyncio.TimeoutError:
                proc.kill()
                log.warning("parser timeout for %s / %s", niche, city)
                return []
            except OSError as exc:
                log.warning("parser failed to start: %s", exc)
                return []
        if proc.returncode != 0:
            log.warning("parser exit %s: %s", proc.returncode, err.decode(errors="replace")[:300])
        companies = to_companies(parse_records(out.decode(errors="replace")), niche, city, self.cfg.get("fields"))[:limit]
        self.db.put(f"parser:{date.today().isoformat()}", self.used_today() + len(companies))
        log.info("parser %s / %s: %d companies", niche, city, len(companies))
        return companies
