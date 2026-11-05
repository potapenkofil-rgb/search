import asyncio
import json
import time
from typing import Awaitable, Callable

import httpx

from .audit import Audit, Screenshotter, audit_site
from .db import DB
from .discovery import Company, discover, exhausted
from .llm import LLM, BudgetExceeded
from .memory import Memory
from .prompts import COMPANY_SCHEMA, COMPANY_SYSTEM
from .settings import Settings
from .util import domain_of, domain_matches, log


class Companies:
    def __init__(self, db: DB, memory: Memory, llm: LLM, settings: Settings, http: httpx.AsyncClient, deliver: Callable[[int], Awaitable[None]]):
        self.db = db
        self.memory = memory
        self.llm = llm
        self.settings = settings
        self.http = http
        self.deliver = deliver
        self.audit_cfg = settings.section("audit")
        self.disc_cfg = settings.section("discovery")
        self.shooter = Screenshotter() if self.audit_cfg.get("screenshot") else None
        self.shots_dir = settings.data_dir / "shots"
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    def _prepare(self, company: Company) -> Company | None:
        skip = self.settings.config.get("skip_domains", [])
        social = self.settings.config.get("social_domains", [])
        dom = domain_of(company.website)
        if dom and domain_matches(dom, social):
            company.socials = [company.website] + company.socials
            company.website = None
        elif dom and domain_matches(dom, skip):
            company.website = None
        if not company.website:
            if not self.settings.section("market").get("include_no_website", True):
                return None
            if company.provider == "osm" and not company.socials:
                return None
        return company

    def _reset_if_stale(self, arm: dict) -> dict:
        state = arm["state"]
        days = float(self.disc_cfg.get("rediscover_days", 30))
        if state.get("exhausted_at") and time.time() - state["exhausted_at"] > days * 86400:
            state = {"twogis_region": state.get("twogis_region")}
            self.db.set_arm_state(arm["id"], state)
        return state

    async def discover_once(self) -> int:
        arms = self.db.arms(kind="company", source="niche")
        queued = self.db.queued_counts()
        low = int(self.disc_cfg.get("queue_low_watermark", 20))
        eligible = []
        for arm in arms:
            arm["state"] = self._reset_if_stale(arm)
            if arm["state"].get("exhausted_at"):
                continue
            if queued.get(arm["id"], 0) >= low:
                continue
            eligible.append(arm["id"])
        picked = self.memory.pick_arms(eligible, int(self.disc_cfg.get("arms_per_run", 3)))
        total = 0
        for arm_id in picked:
            arm = self.db.arm(arm_id)
            companies, state = await discover(self.http, arm, self.settings)
            added = 0
            for company in companies:
                company = self._prepare(company)
                if not company:
                    continue
                key = company.key(self.settings.config.get("social_domains", []))
                if self.db.is_seen(f"lead:{key}"):
                    continue
                if self.db.enqueue_candidate(key, arm_id, company.provider, company.name, company.website, company.to_dict()):
                    added += 1
            state["last_run"] = time.time()
            state["found_total"] = int(state.get("found_total", 0)) + added
            if exhausted(state, self.settings):
                state["exhausted_at"] = time.time()
            self.db.set_arm_state(arm_id, state)
            log.info("discovery %s: +%d", arm["label"], added)
            total += added
        return total

    async def audit_once(self) -> int:
        batch = int(self.settings.section("schedule").get("audit_batch", 15))
        queued = self.db.queued_counts()
        arm_ids = [a for a in queued if self.db.arm(a) and self.db.arm(a)["active"]]
        picked = self.memory.pick_arms(arm_ids, 3)
        if not picked:
            return 0
        per_arm = max(1, batch // len(picked))
        candidates = []
        for arm_id in picked:
            candidates += self.db.take_candidates(arm_id, per_arm)
        sem = asyncio.Semaphore(int(self.audit_cfg.get("concurrency", 5)))
        budget_hit = False

        async def run(candidate):
            nonlocal budget_hit
            async with sem:
                if budget_hit:
                    return None
                try:
                    return await self.process_candidate(candidate)
                except BudgetExceeded:
                    budget_hit = True
                    return None
                except Exception:
                    log.exception("candidate %s failed", candidate["key"])
                    self.db.finish_candidate(candidate["key"], "error")
                    return None

        results = await asyncio.gather(*(run(c) for c in candidates))
        if budget_hit:
            raise BudgetExceeded("company audit")
        return sum(1 for r in results if r)

    async def process_candidate(self, candidate: dict) -> int | None:
        key = candidate["key"]
        company = candidate["payload"]
        if candidate.get("dupes", 1) > int(self.audit_cfg.get("skip_chains_over", 3)):
            self.db.finish_candidate(key, "chain")
            return None
        audit = None
        if company.get("website"):
            audit = await audit_site(self.http, company["website"], self.audit_cfg, self.settings.pagespeed_key, self.shooter)
            if audit.blocked_by_robots:
                self.db.finish_candidate(key, "robots")
                return None
            if audit.reachable and audit.score < int(self.audit_cfg.get("min_signal_score", 5)) and not audit.screenshot:
                self.db.finish_candidate(key, "modern")
                return None
        arm = self.db.arm(candidate["arm_id"]) or {"label": candidate["arm_id"]}
        lead_id = await self.judge_and_save(company, audit, candidate["arm_id"], arm["label"], key, candidate.get("dupes", 1))
        self.db.finish_candidate(key, "done")
        self.db.mark_seen(f"lead:{key}")
        return lead_id

    async def judge(self, company: dict, audit: Audit | None, dupes: int = 1) -> dict | None:
        info = {
            "name": company.get("name"),
            "niche": company.get("niche"),
            "category": company.get("category"),
            "city": company.get("city"),
            "address": company.get("address"),
            "socials": company.get("socials"),
            "branches_found": dupes,
        }
        parts = []
        context = self.memory.context("company")
        if context:
            parts.append(context)
        parts.append("Компания:\n" + json.dumps(info, ensure_ascii=False))
        if audit:
            summary = audit.summary()
            summary.pop("emails", None)
            parts.append("Аудит сайта:\n" + json.dumps(summary, ensure_ascii=False))
            if audit.excerpt:
                parts.append("Текст главной страницы (начало):\n" + audit.excerpt[:1500])
            if audit.screenshot and self.settings.vision:
                parts.append("Приложен скриншот главной страницы. Оцени визуально, насколько устарел дизайн.")
        else:
            parts.append("Сайта у компании нет (в справочнике не указан или указана только соцсеть).")
        result = await self.llm.json(
            COMPANY_SYSTEM,
            "\n\n".join(parts),
            COMPANY_SCHEMA,
            "company",
            image=audit.screenshot if audit else None,
            max_tokens=900,
        )
        if not result:
            return None
        result["score"] = max(0, min(100, int(result.get("score", 0))))
        if not result.get("is_lead"):
            result["score"] = min(result["score"], 30)
        return result

    async def judge_and_save(self, company: dict, audit: Audit | None, arm_id: str, arm_label: str, external_id: str, dupes: int = 1) -> int | None:
        analysis = await self.judge(company, audit, dupes)
        if not analysis:
            return None
        url = (audit.final_url or audit.input_url) if audit else None
        data = {"company": company, "audit": audit.summary() if audit else None, "arm_label": arm_label}
        lead_id = self.db.add_lead("company", arm_id, company.get("provider", "manual"), external_id, company.get("name") or url or "?", url, data, analysis, analysis["score"])
        if lead_id and audit and audit.screenshot:
            (self.shots_dir / f"{lead_id}.jpg").write_bytes(audit.screenshot)
        if lead_id:
            await self.deliver(lead_id)
        return lead_id

    async def manual(self, url: str) -> int | None:
        dom = domain_of(url)
        if not dom:
            return None
        company = {"provider": "manual", "pid": dom, "name": dom, "city": "", "niche": "ручная проверка", "website": url, "socials": []}
        audit = await audit_site(self.http, url, {**self.audit_cfg, "respect_robots": False}, self.settings.pagespeed_key, self.shooter)
        return await self.judge_and_save(company, audit, "manual", "ручной аудит", f"manual:{dom}:{int(time.time())}")

    async def close(self) -> None:
        if self.shooter:
            await self.shooter.close()
