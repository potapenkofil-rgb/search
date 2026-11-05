import asyncio
import base64
import json
import re

from openai import AsyncOpenAI, BadRequestError

from .db import DB
from .settings import Settings
from .util import log


class BudgetExceeded(Exception):
    pass


def _extract_json(text: str):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None
    return None


class LLM:
    def __init__(self, settings: Settings, db: DB, concurrency: int = 4):
        self.settings = settings
        self.db = db
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key or "missing",
            base_url=settings.openai_base_url,
            max_retries=3,
            timeout=120,
        )
        self.sem = asyncio.Semaphore(concurrency)
        self.strict_ok = True

    def spent_today(self) -> float:
        return float(self.db.usage()["cost"])

    def over_budget(self) -> bool:
        return self.spent_today() >= self.settings.daily_budget_usd

    def _cost(self, usage) -> tuple[int, int, float]:
        if not usage:
            return 0, 0, 0.0
        tin = int(getattr(usage, "prompt_tokens", 0) or 0)
        tout = int(getattr(usage, "completion_tokens", 0) or 0)
        cost = tin / 1_000_000 * self.settings.price_in + tout / 1_000_000 * self.settings.price_out
        return tin, tout, cost

    async def json(self, system: str, user: str, schema: dict, name: str, image: bytes | None = None, max_tokens: int = 2000) -> dict | None:
        if self.over_budget():
            raise BudgetExceeded(f"{self.spent_today():.2f}$")
        content = user
        if image and self.settings.vision:
            data = base64.b64encode(image).decode()
            content = [
                {"type": "text", "text": user},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}", "detail": "low"}},
            ]
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        async with self.sem:
            response = await self._call(messages, schema, name, max_tokens)
        tin, tout, cost = self._cost(response.usage)
        self.db.add_usage(tin, tout, cost)
        choice = response.choices[0] if response.choices else None
        text = choice.message.content if choice and choice.message else ""
        parsed = _extract_json(text or "")
        if parsed is None:
            log.warning("llm returned non-json for %s: %s", name, (text or "")[:200])
        return parsed

    async def _call(self, messages, schema, name, max_tokens):
        extra = {}
        if self.settings.reasoning_effort:
            extra["reasoning_effort"] = self.settings.reasoning_effort
        if self.strict_ok:
            try:
                return await self.client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=messages,
                    response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
                    max_completion_tokens=max_tokens,
                    **extra,
                )
            except BadRequestError as exc:
                if "response_format" not in str(exc) and "json_schema" not in str(exc):
                    raise
                log.warning("json_schema not supported, switching to json_object: %s", exc)
                self.strict_ok = False
        fallback = list(messages)
        fallback[0] = {
            "role": "system",
            "content": messages[0]["content"] + "\n\nОтветь строго одним JSON-объектом по этой JSON Schema:\n" + json.dumps(schema, ensure_ascii=False),
        }
        return await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=fallback,
            response_format={"type": "json_object"},
            max_completion_tokens=max_tokens,
            **extra,
        )
