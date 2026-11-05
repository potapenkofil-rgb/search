import copy
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv


DEFAULTS = {
    "market": {
        "cities": ["Москва", "Санкт-Петербург", "Казань", "Екатеринбург", "Новосибирск"],
        "niches": [
            "стоматология",
            "медицинский центр",
            "ветклиника",
            "автосервис",
            "салон красоты",
            "фитнес",
            "гостиница",
            "юридические услуги",
            "агентство недвижимости",
            "строительная компания",
            "мебель",
            "образование и курсы",
            "типография",
            "турагентство",
            "бухгалтерия",
        ],
        "include_no_website": True,
    },
    "schedule": {
        "timezone": "Europe/Moscow",
        "rss_minutes": 10,
        "telegram_minutes": 3,
        "hh_minutes": 30,
        "discovery_minutes": 60,
        "audit_minutes": 5,
        "audit_batch": 15,
        "digest_hour": 19,
        "reflect_hour": 23,
        "chat_discovery_hour": 12,
    },
    "thresholds": {
        "notify_company": 65,
        "notify_order": 60,
        "digest": 40,
        "min_threshold": 40,
        "max_threshold": 90,
        "target_alerts_per_day": 15,
        "max_alerts_per_hour": 4,
    },
    "audit": {
        "pagespeed": True,
        "screenshot": False,
        "respect_robots": True,
        "min_signal_score": 5,
        "timeout_seconds": 20,
        "concurrency": 5,
        "skip_chains_over": 3,
    },
    "discovery": {
        "providers": ["osm", "twogis", "yandex"],
        "arms_per_run": 3,
        "queue_low_watermark": 20,
        "per_arm_limit": 40,
        "overpass_urls": [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
        ],
        "rediscover_days": 30,
    },
    "orders": {
        "max_age_hours": 48,
        "rss": [],
        "telegram_chats": [],
        "chat_search_queries": [
            "фриланс заказы",
            "заказы на сайт",
            "ищу разработчика",
            "заказы telegram бот",
            "заказы для программистов",
            "фриланс веб",
        ],
        "hh_queries": [
            "разработка сайта",
            "создание сайта",
            "telegram бот",
            "автоматизация бизнес-процессов",
            "редизайн сайта",
        ],
        "hh_user_agent": "LeadHunter/1.0",
        "include_keywords": [
            "сайт", "лендинг", "landing", "редизайн", "интернет-магазин", "веб", "web",
            "бот", "bot", "telegram", "телеграм", "mini app", "чат-бот",
            "автоматизац", "парсер", "парсинг", "скрипт", "интеграц", "api",
            "crm", "amocrm", "битрикс", "bitrix", "tilda", "тильда", "wordpress", "вордпресс",
            "верстк", "frontend", "фронтенд", "backend", "бэкенд", "react", "vue", "next.js",
            "python", "node", "django", "fastapi", "php", "laravel",
            "нейросет", "gpt", "openai", "ai-", "ии-", "n8n", "make.com", "zapier",
            "google таблиц", "google sheets", "приложени", "доработ", "программист", "разработчик",
        ],
        "exclude_keywords": [
            "#резюме", "#ищуработу", "#ищу_работу", "ищу заказы", "ищу заказчиков",
            "предлагаю услуги", "сделаю сайт", "сделаю бота", "#помогу", "#услуги",
            "обучение с нуля", "курс по", "вебинар",
        ],
        "classify_batch": 8,
    },
    "skip_domains": [
        "avito.ru", "2gis.ru", "2gis.com", "yandex.ru", "ya.ru", "google.com", "goo.gl",
        "prodoctorov.ru", "zoon.ru", "flamp.ru", "yell.ru", "tripadvisor.ru", "tripadvisor.com",
        "booking.com", "ostrovok.ru", "sutochno.ru", "gosuslugi.ru", "mos.ru", "napopravku.ru",
        "docdoc.ru", "sberhealth.ru", "otzovik.com", "irecommend.ru", "hh.ru", "rabota.ru",
    ],
    "social_domains": [
        "vk.com", "vk.ru", "m.vk.com", "ok.ru", "t.me", "telegram.me", "wa.me", "whatsapp.com",
        "instagram.com", "facebook.com", "youtube.com", "taplink.cc", "taplink.ws", "taplink.ru",
        "linktr.ee", "dikidi.net", "dikidi.ru", "yclients.com", "n1.yclients.com", "hipolink.me",
        "mssg.me", "inst.page",
    ],
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _int(value):
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    openai_api_key: str
    openai_model: str
    openai_base_url: str | None
    reasoning_effort: str | None
    price_in: float
    price_out: float
    daily_budget_usd: float
    vision: bool
    bot_token: str
    owner_id: int | None
    tg_api_id: int | None
    tg_api_hash: str | None
    twogis_key: str | None
    yandex_key: str | None
    pagespeed_key: str | None
    data_dir: Path
    config: dict

    @property
    def db_path(self) -> Path:
        return self.data_dir / "leadhunter.db"

    @property
    def tg_session_path(self) -> Path:
        return self.data_dir / "telegram"

    def section(self, name: str) -> dict:
        return self.config.get(name, {})


def load(config_path: str | None = None, env_file: str | None = None) -> Settings:
    load_dotenv(env_file or os.getenv("LEADHUNTER_ENV", ".env"))
    path = Path(config_path or os.getenv("LEADHUNTER_CONFIG", "config.yaml"))
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = _merge(DEFAULTS, raw)
    data_dir = Path(os.getenv("LEADHUNTER_DATA", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna"),
        openai_base_url=os.getenv("OPENAI_BASE_URL") or None,
        reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT") or None,
        price_in=_float(os.getenv("LLM_PRICE_IN"), 0.10),
        price_out=_float(os.getenv("LLM_PRICE_OUT"), 0.50),
        daily_budget_usd=_float(os.getenv("DAILY_BUDGET_USD"), 1.0),
        vision=os.getenv("LLM_VISION", "0") == "1",
        bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        owner_id=_int(os.getenv("TELEGRAM_OWNER_ID")),
        tg_api_id=_int(os.getenv("TG_API_ID")),
        tg_api_hash=os.getenv("TG_API_HASH") or None,
        twogis_key=os.getenv("TWOGIS_API_KEY") or None,
        yandex_key=os.getenv("YANDEX_PLACES_API_KEY") or None,
        pagespeed_key=os.getenv("PAGESPEED_API_KEY") or None,
        data_dir=data_dir,
        config=config,
    )
