import argparse
import asyncio
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from . import settings as settings_module
from .audit import audit_site
from .util import http_client


def setup_logging(data_dir) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)
    file = RotatingFileHandler(data_dir / "leadhunter.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file.setFormatter(fmt)
    root.addHandler(file)
    for noisy in ("httpx", "httpcore", "telethon", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def cmd_run(settings) -> None:
    from .app import App

    await App(settings).run()


async def cmd_tg_login(settings) -> None:
    from .tg import TgReader

    reader = TgReader(settings)
    if not reader.configured:
        raise SystemExit("Нужны TG_API_ID и TG_API_HASH в .env (получить на https://my.telegram.org)")
    await reader.login()


async def cmd_audit(settings, url: str, with_llm: bool) -> None:
    async with http_client(float(settings.section("audit").get("timeout_seconds", 20))) as client:
        audit = await audit_site(client, url, {**settings.section("audit"), "respect_robots": False}, settings.pagespeed_key)
    print(json.dumps(audit.summary(), ensure_ascii=False, indent=2))
    if with_llm and settings.openai_api_key:
        from .companies import Companies
        from .db import DB
        from .llm import LLM
        from .memory import Memory

        db = DB(settings.db_path)
        memory = Memory(db, settings)
        companies = Companies(db, memory, LLM(settings, db), settings, None, None)
        company = {"name": audit.domain, "niche": "ручная проверка", "website": url}
        result = await companies.judge(company, audit)
        print(json.dumps(result, ensure_ascii=False, indent=2))


async def cmd_reflect(settings) -> None:
    from .app import App
    from .reflect import reflect

    app = App(settings)
    try:
        print(await reflect(app))
    finally:
        await app.close()


async def cmd_check(settings) -> None:
    import httpx

    ok = True

    def line(name, good, note=""):
        nonlocal ok
        ok = ok and good
        print(f"[{'OK' if good else '!!'}] {name}{' — ' + note if note else ''}")

    line("OPENAI_API_KEY", bool(settings.openai_api_key))
    line("TELEGRAM_BOT_TOKEN", bool(settings.bot_token))
    line("TELEGRAM_OWNER_ID", bool(settings.owner_id), "" if settings.owner_id else "напиши боту /start после запуска")
    async with http_client(20) as client:
        if settings.openai_api_key:
            from openai import AsyncOpenAI

            try:
                ai = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
                r = await ai.chat.completions.create(
                    model=settings.openai_model,
                    messages=[{"role": "user", "content": "Ответь одним словом: ок"}],
                    max_completion_tokens=50,
                )
                line(f"модель {settings.openai_model}", True, (r.choices[0].message.content or "").strip()[:40])
            except Exception as exc:
                line(f"модель {settings.openai_model}", False, str(exc)[:200])
        if settings.bot_token:
            try:
                r = await client.get(f"https://api.telegram.org/bot{settings.bot_token}/getMe")
                body = r.json()
                line("Telegram-бот", body.get("ok", False), "@" + (body.get("result") or {}).get("username", "?"))
            except (httpx.HTTPError, ValueError) as exc:
                line("Telegram-бот", False, str(exc)[:200])
        line("Telegram-аккаунт (чтение чатов)", settings.tg_session_path.with_suffix(".session").exists(), "" if settings.tg_session_path.with_suffix(".session").exists() else "опционально: python -m leadhunter tg-login")
        for feed in settings.section("orders").get("rss", []):
            try:
                r = await client.get(feed["url"])
                line(f"RSS {feed.get('name')}", r.status_code == 200, f"HTTP {r.status_code}")
            except httpx.HTTPError as exc:
                line(f"RSS {feed.get('name')}", False, str(exc)[:120])
        urls = settings.section("discovery").get("overpass_urls", [])
        reachable = False
        for url in urls:
            try:
                r = await client.post(url, data={"data": "[out:json];node(1);out;"}, timeout=30)
                if r.status_code == 200:
                    reachable = True
                    break
            except httpx.HTTPError:
                continue
        line("OpenStreetMap Overpass", reachable)
        line("2ГИС API", bool(settings.twogis_key), "" if settings.twogis_key else "опционально, больше компаний")
        line("Яндекс Places API", bool(settings.yandex_key), "" if settings.yandex_key else "опционально")
        line("PageSpeed API key", bool(settings.pagespeed_key), "" if settings.pagespeed_key else "без ключа Google часто отвечает 429")
    print("Всё готово" if ok else "Есть проблемы, см. выше")


def main() -> None:
    parser = argparse.ArgumentParser(prog="leadhunter")
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("tg-login")
    sub.add_parser("reflect")
    sub.add_parser("check")
    audit = sub.add_parser("audit")
    audit.add_argument("url")
    audit.add_argument("--llm", action="store_true")
    args = parser.parse_args()
    settings = settings_module.load(args.config)
    setup_logging(settings.data_dir)
    if args.cmd == "run":
        asyncio.run(cmd_run(settings))
    elif args.cmd == "tg-login":
        asyncio.run(cmd_tg_login(settings))
    elif args.cmd == "reflect":
        asyncio.run(cmd_reflect(settings))
    elif args.cmd == "check":
        asyncio.run(cmd_check(settings))
    elif args.cmd == "audit":
        asyncio.run(cmd_audit(settings, args.url, args.llm))


if __name__ == "__main__":
    main()
