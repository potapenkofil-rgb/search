import asyncio
import re
from dataclasses import asdict, dataclass, field

import httpx

from .util import domain_of, domain_matches, log, normalize_url


NICHE_TAGS = {
    "стоматология": ["amenity=dentist", "healthcare=dentist"],
    "медицинский центр": ["amenity=clinic", "healthcare=clinic"],
    "ветклиника": ["amenity=veterinary"],
    "автосервис": ["shop=car_repair"],
    "шиномонтаж": ["shop=tyres"],
    "автомойка": ["amenity=car_wash"],
    "автосалон": ["shop=car"],
    "салон красоты": ["shop=beauty", "shop=hairdresser"],
    "барбершоп": ["shop=hairdresser"],
    "фитнес": ["leisure=fitness_centre", "leisure=sports_centre"],
    "ресторан": ["amenity=restaurant"],
    "кафе": ["amenity=cafe"],
    "гостиница": ["tourism=hotel", "tourism=guest_house", "tourism=hostel"],
    "юридические услуги": ["office=lawyer"],
    "нотариус": ["office=notary"],
    "агентство недвижимости": ["office=estate_agent"],
    "строительная компания": ["office=construction_company", "craft=builder", "office=company"],
    "мебель": ["shop=furniture", "craft=carpenter"],
    "образование и курсы": ["amenity=language_school", "amenity=driving_school", "amenity=training", "amenity=music_school"],
    "типография": ["craft=printer", "shop=copyshop"],
    "турагентство": ["shop=travel_agency", "office=travel_agent"],
    "бухгалтерия": ["office=accountant", "office=tax_advisor"],
    "цветы": ["shop=florist"],
    "оптика": ["shop=optician"],
    "ювелирный": ["shop=jewelry"],
    "окна и двери": ["shop=doors", "shop=windows", "craft=window_construction"],
    "сантехника": ["shop=bathroom_furnishing", "craft=plumber"],
    "стройматериалы": ["shop=doityourself", "shop=hardware", "shop=trade"],
    "детские центры": ["amenity=childcare", "leisure=playground"],
    "психолог": ["healthcare=psychotherapist", "healthcare=counselling"],
    "ремонт техники": ["craft=electronics_repair", "shop=mobile_phone", "shop=computer"],
    "химчистка": ["shop=dry_cleaning", "shop=laundry"],
    "ритуальные услуги": ["shop=funeral_directors"],
    "зоомагазин": ["shop=pet"],
    "страхование": ["office=insurance"],
    "логистика": ["office=logistics", "office=courier"],
}


@dataclass
class Company:
    provider: str
    pid: str
    name: str
    city: str
    niche: str
    category: str = ""
    address: str = ""
    website: str | None = None
    phone: str | None = None
    phones: list[str] = field(default_factory=list)
    email: str | None = None
    socials: list[str] = field(default_factory=list)

    def key(self, social_domains: list[str]) -> str:
        dom = domain_of(self.website)
        if dom and not domain_matches(dom, social_domains):
            return f"site:{dom}"
        return f"nosite:{self.provider}:{self.pid}"

    def to_dict(self) -> dict:
        return asdict(self)


def _oq(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def overpass_query(city: str, tags: list[str], limit: int) -> str:
    parts = []
    for tag in tags:
        if "=" not in tag:
            continue
        key, value = tag.split("=", 1)
        parts.append(f'nwr["{_oq(key.strip())}"="{_oq(value.strip())}"]["name"](area.a);')
    body = "\n".join(parts)
    return (
        f'[out:json][timeout:90];\n'
        f'area["name"="{_oq(city)}"]["boundary"="administrative"]["admin_level"~"^[4-8]$"]->.a;\n'
        f"(\n{body}\n);\nout tags center {limit};"
    )


def parse_overpass(data: dict, city: str, niche: str) -> list[Company]:
    out = []
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            continue
        website = tags.get("website") or tags.get("contact:website") or tags.get("url")
        socials = [tags[k] for k in ("contact:vk", "contact:instagram", "contact:telegram", "contact:facebook") if tags.get(k)]
        address = ", ".join(x for x in (tags.get("addr:street"), tags.get("addr:housenumber")) if x)
        category = next((f"{k}={tags[k]}" for k in ("amenity", "shop", "office", "craft", "healthcare", "leisure", "tourism") if tags.get(k)), "")
        raw_phones = [p.strip() for key in ("phone", "contact:phone", "contact:mobile") for p in (tags.get(key) or "").split(";") if p.strip()]
        out.append(
            Company(
                provider="osm",
                pid=f"{el.get('type')}{el.get('id')}",
                name=name,
                city=city,
                niche=niche,
                category=category,
                address=address,
                website=normalize_url(website),
                phone=raw_phones[0] if raw_phones else None,
                phones=raw_phones[:4],
                email=tags.get("email") or tags.get("contact:email"),
                socials=socials,
            )
        )
    return out


async def osm_search(client: httpx.AsyncClient, urls: list[str], city: str, niche: str, tags: list[str], limit: int = 500) -> list[Company]:
    query = overpass_query(city, tags, limit)
    for url in urls:
        try:
            response = await client.post(url, data={"data": query}, timeout=120)
            if response.status_code == 200:
                return parse_overpass(response.json(), city, niche)
            log.info("overpass %s -> %s", url, response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("overpass %s failed: %s", url, exc)
        await asyncio.sleep(2)
    return []


def parse_twogis(data: dict, city: str, niche: str) -> list[Company]:
    out = []
    for item in (data.get("result") or {}).get("items", []):
        website = None
        phones = []
        email = None
        socials = []
        for group in item.get("contact_groups") or []:
            for contact in group.get("contacts") or []:
                kind = contact.get("type")
                value = contact.get("text") or contact.get("value") or ""
                if kind == "website" and not website:
                    text = contact.get("text") or ""
                    website = normalize_url(text if "." in text and "2gis" not in text else contact.get("value"))
                elif kind == "phone":
                    number = contact.get("value") or value
                    if number and number not in phones:
                        phones.append(number)
                elif kind == "email" and not email:
                    email = value
                elif kind in ("vkontakte", "instagram", "telegram", "whatsapp", "facebook"):
                    socials.append(contact.get("url") or value)
        rubrics = ", ".join(r.get("name", "") for r in item.get("rubrics") or [] if r.get("name"))
        out.append(
            Company(
                provider="twogis",
                pid=str(item.get("id", "")).split("_")[0],
                name=item.get("name") or "",
                city=city,
                niche=niche,
                category=rubrics,
                address=item.get("address_name") or "",
                website=website,
                phone=phones[0] if phones else None,
                phones=phones[:4],
                email=email,
                socials=socials[:4],
            )
        )
    return [c for c in out if c.name]


async def twogis_region(client: httpx.AsyncClient, key: str, city: str) -> int | None:
    try:
        response = await client.get("https://catalog.api.2gis.com/2.0/region/search", params={"q": city, "key": key}, timeout=30)
        items = (response.json().get("result") or {}).get("items") or []
        return int(items[0]["id"]) if items else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.info("2gis region failed %s: %s", city, exc)
        return None


async def twogis_search(client: httpx.AsyncClient, key: str, region_id: int, city: str, niche: str, page: int) -> list[Company]:
    params = {
        "q": niche,
        "region_id": region_id,
        "type": "branch",
        "fields": "items.contact_groups,items.rubrics,items.address",
        "page": page,
        "page_size": 10,
        "key": key,
    }
    try:
        response = await client.get("https://catalog.api.2gis.com/3.0/items", params=params, timeout=30)
        if response.status_code != 200:
            log.info("2gis items -> %s", response.status_code)
            return []
        return parse_twogis(response.json(), city, niche)
    except (httpx.HTTPError, ValueError) as exc:
        log.info("2gis items failed: %s", exc)
        return []


def parse_yandex(data: dict, city: str, niche: str) -> list[Company]:
    out = []
    for feature in data.get("features", []):
        meta = (feature.get("properties") or {}).get("CompanyMetaData") or {}
        if not meta.get("name"):
            continue
        phones = meta.get("Phones") or []
        cats = ", ".join(c.get("name", "") for c in meta.get("Categories") or [])
        out.append(
            Company(
                provider="yandex",
                pid=str(meta.get("id", "")),
                name=meta["name"],
                city=city,
                niche=niche,
                category=cats,
                address=meta.get("address") or "",
                website=normalize_url(meta.get("url")),
                phone=phones[0].get("formatted") if phones else None,
                phones=[p.get("formatted") for p in phones[:4] if p.get("formatted")],
            )
        )
    return out


async def yandex_search(client: httpx.AsyncClient, key: str, city: str, niche: str, skip: int) -> list[Company]:
    params = {"apikey": key, "text": f"{niche} {city}", "type": "biz", "lang": "ru_RU", "results": 50, "skip": skip}
    try:
        response = await client.get("https://search-maps.yandex.ru/v1/", params=params, timeout=30)
        if response.status_code != 200:
            log.info("yandex places -> %s", response.status_code)
            return []
        return parse_yandex(response.json(), city, niche)
    except (httpx.HTTPError, ValueError) as exc:
        log.info("yandex places failed: %s", exc)
        return []


def niche_tags(niche: str, params: dict) -> list[str]:
    tags = params.get("osm_tags") or NICHE_TAGS.get(niche.lower()) or []
    return [t for t in tags if re.match(r"^[a-z_:]+=[\w:-]+$", t)]


async def discover(client: httpx.AsyncClient, arm: dict, settings) -> tuple[list[Company], dict]:
    params = arm["params"]
    state = dict(arm["state"])
    city = params["city"]
    niche = params["niche"]
    cfg = settings.section("discovery")
    providers = cfg.get("providers", ["osm"])
    found: list[Company] = []
    if "osm" in providers and not state.get("osm_done"):
        tags = niche_tags(niche, params)
        if tags:
            found += await osm_search(client, cfg.get("overpass_urls", []), city, niche, tags)
        state["osm_done"] = True
    if "twogis" in providers and settings.twogis_key and not state.get("twogis_done"):
        if not state.get("twogis_region"):
            state["twogis_region"] = await twogis_region(client, settings.twogis_key, city)
        if state.get("twogis_region"):
            page = int(state.get("twogis_page", 1))
            batch = []
            for p in range(page, page + 3):
                chunk = await twogis_search(client, settings.twogis_key, state["twogis_region"], city, niche, p)
                batch += chunk
                if len(chunk) < 10:
                    state["twogis_done"] = True
                    break
            state["twogis_page"] = page + 3
            found += batch
    if "yandex" in providers and settings.yandex_key and not state.get("yandex_done"):
        skip = int(state.get("yandex_skip", 0))
        batch = await yandex_search(client, settings.yandex_key, city, niche, skip)
        state["yandex_skip"] = skip + 50
        if len(batch) < 50 or skip >= 450:
            state["yandex_done"] = True
        found += batch
    return found, state


def exhausted(state: dict, settings) -> bool:
    providers = settings.section("discovery").get("providers", ["osm"])
    checks = []
    if "osm" in providers:
        checks.append(state.get("osm_done"))
    if "twogis" in providers and settings.twogis_key:
        checks.append(state.get("twogis_done"))
    if "yandex" in providers and settings.yandex_key:
        checks.append(state.get("yandex_done"))
    return all(checks) if checks else True
