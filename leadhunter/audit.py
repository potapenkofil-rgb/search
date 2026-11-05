import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date
from urllib import robotparser
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .util import USER_AGENT, domain_of, log


MAX_BYTES = 3_000_000

FREE_BUILDERS = [
    "ucoz.ru", "ucoz.net", "ucoz.com", "narod.ru", "narod2.ru", "3dn.ru", "at.ua", "ucoz.ua",
    "nethouse.ru", "ukit.me", "wix.com", "wixsite.com", "tilda.ws", "jimdo.com", "jimdofree.com",
    "mya5.ru", "umi.ru", "a5.ru", "okis.ru", "satu.kz", "diary.ru", "blogspot.com", "tiu.ru",
    "pulscen.ru", "flexbe.ru", "lpgenerator.ru", "turbo.site", "ru.gg",
]

PARKED_PATTERNS = [
    "домен продается", "домен продаётся", "this domain is for sale", "domain is for sale",
    "buy this domain", "домен припаркован", "parked domain", "парковка домена",
    "срок регистрации домена истек", "срок регистрации домена истёк", "domain has expired",
    "reg.ru — регистрация доменов", "этот домен зарегистрирован",
]

CONSTRUCTION_PATTERNS = [
    "сайт в разработке", "сайт находится в разработке", "under construction", "сайт скоро откроется",
    "coming soon", "ведутся технические работы", "сайт временно недоступен",
]


@dataclass
class Signal:
    code: str
    weight: int
    text: str


@dataclass
class Audit:
    input_url: str
    domain: str | None
    final_url: str | None = None
    reachable: bool = False
    https: bool = False
    status: int | None = None
    elapsed: float | None = None
    html_bytes: int = 0
    title: str = ""
    description: str = ""
    headings: list[str] = field(default_factory=list)
    excerpt: str = ""
    emails: list[str] = field(default_factory=list)
    socials: list[str] = field(default_factory=list)
    tech: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    pagespeed: int | None = None
    screenshot: bytes | None = None
    blocked_by_robots: bool = False
    error: str | None = None

    @property
    def score(self) -> int:
        return sum(s.weight for s in self.signals)

    def add(self, code: str, weight: int, text: str) -> None:
        if not any(s.code == code for s in self.signals):
            self.signals.append(Signal(code, weight, text))

    def summary(self) -> dict:
        return {
            "url": self.final_url or self.input_url,
            "reachable": self.reachable,
            "https": self.https,
            "status": self.status,
            "load_seconds": round(self.elapsed, 2) if self.elapsed else None,
            "html_kb": round(self.html_bytes / 1024),
            "title": self.title,
            "description": self.description,
            "headings": self.headings[:8],
            "tech": self.tech,
            "pagespeed_mobile": self.pagespeed,
            "signals": [s.text for s in sorted(self.signals, key=lambda s: -s.weight)],
            "signal_score": self.score,
            "emails": self.emails,
            "socials": self.socials,
            "error": self.error,
        }


def _versions(pattern: str, haystack: str) -> list[tuple[int, int]]:
    out = []
    for major, minor in re.findall(pattern, haystack, re.I):
        try:
            out.append((int(major), int(minor)))
        except ValueError:
            continue
    return out


def analyze_html(audit: Audit, markup: str, today: date | None = None) -> None:
    today = today or date.today()
    low = markup.lower()
    soup = BeautifulSoup(markup, "html.parser")

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    audit.title = title[:200]
    desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    audit.description = (desc_tag.get("content") or "").strip()[:300] if desc_tag else ""
    audit.headings = [h.get_text(" ", strip=True)[:120] for h in soup.find_all(["h1", "h2"]) if h.get_text(strip=True)][:10]

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            email = href[7:].split("?")[0].strip()
            if email and email not in audit.emails:
                audit.emails.append(email)
        else:
            dom = domain_of(href) if href.startswith("http") else None
            if dom and any(dom == s or dom.endswith("." + s) for s in ("vk.com", "vk.ru", "t.me", "instagram.com", "wa.me", "ok.ru")):
                if href not in audit.socials:
                    audit.socials.append(href)
    for email in re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", soup.get_text(" ")):
        if email not in audit.emails and not email.lower().endswith((".png", ".jpg", ".webp", ".svg")):
            audit.emails.append(email)
    audit.emails = audit.emails[:3]
    audit.socials = audit.socials[:5]

    scripts = " ".join(s.get("src", "") for s in soup.find_all("script"))
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    audit.excerpt = text[:1500]

    if any(p in low for p in PARKED_PATTERNS) and len(text) < 3000:
        audit.add("parked", 30, "домен припаркован или продаётся, сайта по факту нет")
    if any(p in low for p in CONSTRUCTION_PATTERNS) and len(text) < 3000:
        audit.add("construction", 25, "вместо сайта заглушка «в разработке»")

    if not re.search(r"<meta[^>]+name=[\"']?viewport", low):
        audit.add("no_viewport", 25, "нет мобильной версии (нет meta viewport)")

    years = []
    for match in re.finditer(r"(?:©|&copy;|copyright|\(c\)|все права защищены)[^0-9]{0,40}((?:19|20)\d{2})(?:\s*[-–—]\s*((?:19|20)\d{2}))?", text, re.I):
        for group in match.groups():
            if group:
                years.append(int(group))
    if years:
        last = max(y for y in years if y <= today.year) if any(y <= today.year for y in years) else None
        if last and last <= today.year - 3:
            weight = 15 if last <= today.year - 6 else 10
            audit.add("old_copyright", weight, f"в подвале © {last} — сайт давно не обновляли")

    jq = _versions(r"jquery[.-]?v?(\d+)\.(\d+)", scripts + " " + markup[:200000])
    if jq:
        major, minor = min(jq)
        audit.tech.append(f"jQuery {major}.{minor}")
        if major < 2:
            audit.add("old_jquery", 6, f"старый jQuery {major}.{minor}")
    bs = _versions(r"bootstrap[/.-]?v?(\d)\.(\d+)", scripts + " " + low[:200000])
    if bs:
        major, minor = min(bs)
        audit.tech.append(f"Bootstrap {major}.{minor}")
        if major <= 3:
            audit.add("old_bootstrap", 5, f"старый Bootstrap {major}.{minor}")

    if ".swf" in low or "shockwave-flash" in low:
        audit.add("flash", 20, "на сайте Flash — давно не работает в браузерах")
    if "<frameset" in low or re.search(r"<frame\s", low):
        audit.add("frames", 20, "вёрстка на фреймах (frameset)")
    deprecated = len(re.findall(r"<(font|center|marquee|blink)[\s>]", low))
    if deprecated:
        audit.add("deprecated_tags", 8, f"устаревшие HTML-теги (font/center/marquee): {deprecated} шт.")
    tables = low.count("<table")
    divs = low.count("<div")
    if tables >= 4 and divs < tables * 3:
        audit.add("table_layout", 10, "вёрстка таблицами, как в 2000-х")
    head = low[:300].lstrip()
    if head.startswith("<!doctype") and not head.startswith("<!doctype html>"):
        if "xhtml" in head or "html 4" in head:
            audit.add("old_doctype", 5, "старый стандарт HTML4/XHTML")
    elif not head.startswith("<!doctype"):
        audit.add("no_doctype", 3, "нет doctype")
    if "<!--[if ie" in low or "<!--[if lt ie" in low:
        audit.add("ie_hacks", 3, "хаки под Internet Explorer")

    generator = soup.find("meta", attrs={"name": re.compile(r"^generator$", re.I)})
    gen = (generator.get("content") or "") if generator else ""
    if gen:
        audit.tech.append(gen[:60])
    gen_low = gen.lower()
    wp = re.search(r"wordpress\s+(\d+)\.(\d+)", gen_low)
    joomla = re.search(r"joomla!?\s*(\d+)\.(\d+)", gen_low)
    drupal = re.search(r"drupal\s+(\d+)", gen_low)
    if wp and int(wp.group(1)) < 5:
        audit.add("old_cms", 12, f"старый WordPress {wp.group(1)}.{wp.group(2)}")
    elif joomla and int(joomla.group(1)) < 3:
        audit.add("old_cms", 12, f"старая Joomla {joomla.group(1)}.{joomla.group(2)}")
    elif drupal and int(drupal.group(1)) < 8:
        audit.add("old_cms", 12, f"старый Drupal {drupal.group(1)}")
    elif any(x in gen_low for x in ("frontpage", "dreamweaver", "netobjects", "ucoz")):
        audit.add("old_cms", 12, f"сайт сделан в {gen[:40]}")
    for marker, name in (("ucoz", "uCoz"), ("bitrix", "1C-Битрикс"), ("tilda", "Tilda"), ("wix.com", "Wix"), ("wp-content", "WordPress"), ("joomla", "Joomla"), ("modx", "MODX"), ("opencart", "OpenCart"), ("insales", "InSales"), ("nethouse", "Nethouse")):
        if marker in low and name not in audit.tech:
            audit.tech.append(name)

    if not audit.title:
        audit.add("no_title", 5, "нет заголовка страницы (title)")
    if not audit.description:
        audit.add("no_description", 3, "нет meta description — плохо для SEO")
    if len(text) < 300 and "parked" not in [s.code for s in audit.signals]:
        audit.add("thin", 6, "на главной почти нет текста (или он рисуется скриптами)")


async def _allowed(client: httpx.AsyncClient, base: str) -> bool:
    try:
        response = await client.get(urljoin(base, "/robots.txt"), timeout=8)
    except httpx.HTTPError:
        return True
    if response.status_code != 200:
        return True
    parser = robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(USER_AGENT, base)


async def _get(client: httpx.AsyncClient, url: str) -> tuple[httpx.Response, bytes, float]:
    start = time.monotonic()
    async with client.stream("GET", url) as response:
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                break
    return response, body, time.monotonic() - start


async def pagespeed(client: httpx.AsyncClient, url: str, key: str | None) -> int | None:
    params = {"url": url, "strategy": "mobile", "category": "performance"}
    if key:
        params["key"] = key
    try:
        response = await client.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=90)
        if response.status_code != 200:
            log.info("pagespeed %s -> %s", url, response.status_code)
            return None
        score = response.json()["lighthouseResult"]["categories"]["performance"]["score"]
        return round(float(score) * 100) if score is not None else None
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        log.info("pagespeed failed %s: %s", url, exc)
        return None


class Screenshotter:
    def __init__(self):
        self.pw = None
        self.browser = None
        self.lock = asyncio.Lock()

    async def shot(self, url: str) -> bytes | None:
        async with self.lock:
            try:
                if not self.browser:
                    from playwright.async_api import async_playwright

                    self.pw = await async_playwright().start()
                    self.browser = await self.pw.chromium.launch()
                page = await self.browser.new_page(viewport={"width": 1280, "height": 800}, user_agent=USER_AGENT)
                try:
                    await page.goto(url, timeout=25000, wait_until="load")
                    await page.wait_for_timeout(1500)
                    return await page.screenshot(type="jpeg", quality=60)
                finally:
                    await page.close()
            except Exception as exc:
                log.info("screenshot failed %s: %s", url, exc)
                return None

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.pw:
            await self.pw.stop()
        self.browser = None
        self.pw = None


async def audit_site(client: httpx.AsyncClient, url: str, config: dict, pagespeed_key: str | None = None, shooter: Screenshotter | None = None) -> Audit:
    domain = domain_of(url)
    audit = Audit(input_url=url, domain=domain)
    if not domain:
        audit.error = "bad url"
        return audit
    response = None
    body = b""
    ssl_problem = False
    for scheme in ("https", "http"):
        target = f"{scheme}://{domain}/"
        if config.get("respect_robots", True) and not await _allowed(client, target):
            audit.blocked_by_robots = True
            audit.error = "robots.txt запрещает обход"
            return audit
        try:
            response, body, elapsed = await _get(client, target)
            audit.elapsed = elapsed
            break
        except httpx.ConnectError as exc:
            if "ssl" in str(exc).lower() or "certificate" in str(exc).lower():
                ssl_problem = True
            audit.error = str(exc)[:200]
        except httpx.HTTPError as exc:
            audit.error = str(exc)[:200]
    if response is None:
        if ssl_problem:
            audit.add("ssl_error", 15, "сломан SSL-сертификат")
        audit.add("down", 35, "сайт не открывается")
        return audit
    audit.error = None
    audit.status = response.status_code
    audit.final_url = str(response.url)
    audit.https = audit.final_url.startswith("https://")
    audit.html_bytes = len(body)
    if response.status_code >= 400:
        audit.add("http_error", 30, f"сайт отдаёт ошибку {response.status_code}")
        return audit
    audit.reachable = True
    if ssl_problem:
        audit.add("ssl_error", 15, "сломан SSL-сертификат, браузер показывает предупреждение")
    elif not audit.https:
        audit.add("no_https", 12, "нет HTTPS — браузер пишет «Не защищено»")
    if audit.elapsed and audit.elapsed > 4:
        audit.add("slow", 6, f"главная грузится {audit.elapsed:.1f} с")
    if audit.html_bytes > 1_500_000:
        audit.add("heavy", 4, f"тяжёлый HTML {audit.html_bytes // 1024} КБ")
    if domain_of(audit.final_url) and any(domain_of(audit.final_url).endswith(b) for b in FREE_BUILDERS):
        audit.add("free_builder", 12, f"сайт на бесплатном конструкторе ({domain_of(audit.final_url)})")
    encoding = response.encoding or "utf-8"
    try:
        markup = body.decode(encoding, errors="replace")
    except LookupError:
        markup = body.decode("utf-8", errors="replace")
    if re.search(r"charset=[\"']?(windows-1251|cp1251)", markup[:3000], re.I) and encoding.lower() not in ("windows-1251", "cp1251"):
        markup = body.decode("cp1251", errors="replace")
    analyze_html(audit, markup)
    tasks = []
    if config.get("pagespeed", True):
        tasks.append(pagespeed(client, audit.final_url, pagespeed_key))
    if shooter and config.get("screenshot"):
        tasks.append(shooter.shot(audit.final_url))
    results = await asyncio.gather(*tasks) if tasks else []
    idx = 0
    if config.get("pagespeed", True):
        audit.pagespeed = results[idx]
        idx += 1
        if audit.pagespeed is not None:
            if audit.pagespeed < 30:
                audit.add("pagespeed_bad", 12, f"PageSpeed на телефоне {audit.pagespeed}/100")
            elif audit.pagespeed < 50:
                audit.add("pagespeed_low", 6, f"PageSpeed на телефоне {audit.pagespeed}/100")
    if shooter and config.get("screenshot"):
        audit.screenshot = results[idx]
    return audit
