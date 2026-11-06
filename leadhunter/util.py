import hashlib
import html
import logging
import re
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup


log = logging.getLogger("leadhunter")

USER_AGENT = "LeadHunterBot/1.0 (site quality audit)"


def http_client(timeout: float = 20.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


def normalize_url(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip().split()[0].strip("<>\"'")
    if not value:
        return None
    if value.startswith("//"):
        value = "http:" + value
    if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        value = "http://" + value
    parsed = urlparse(value)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return value


def domain_of(raw: str | None) -> str | None:
    url = normalize_url(raw)
    if not url:
        return None
    host = urlparse(url).hostname or ""
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host or None


def domain_matches(domain: str | None, patterns: list[str]) -> bool:
    if not domain:
        return False
    for pattern in patterns:
        pattern = pattern.lower()
        if domain == pattern or domain.endswith("." + pattern):
            return True
    return False


def html_to_text(markup: str | None, limit: int | None = None) -> str:
    if not markup:
        return ""
    text = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if limit else text


def esc(value) -> str:
    return html.escape(str(value or ""), quote=False)


def clip(value: str | None, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


PHONE_RE = re.compile(r"(?<!\d)(?:\+?7|8)[\s\-().]{0,4}(?:\d[\s\-().]{0,4}){9}\d(?!\d)")


def phone_key(raw: str | None) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in "78":
        digits = "7" + digits[1:]
    else:
        return None
    return digits if digits[1] not in "01" else None


def phone_text(raw: str | None) -> str:
    key = phone_key(raw)
    if not key:
        return (raw or "").strip()
    return f"+7 {key[1:4]} {key[4:7]}-{key[7:9]}-{key[9:]}"


def normalize_phones(values, limit: int = 4) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        key = phone_key(value)
        shown = phone_text(value) if key else re.sub(r"[^\d+()\-\s]", "", str(value)).strip()
        ident = key or re.sub(r"\D", "", shown)
        if len(ident) < 6 or ident in seen:
            continue
        seen.add(ident)
        out.append(shown)
        if len(out) >= limit:
            break
    return out


def text_hash(value: str) -> str:
    norm = re.sub(r"\W+", " ", (value or "").lower()).strip()[:400]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def keyword_hit(text: str, include: list[str], exclude: list[str]) -> bool:
    low = (text or "").lower()
    if any(word.lower() in low for word in exclude if word):
        return False
    return any(word.lower() in low for word in include if word)
