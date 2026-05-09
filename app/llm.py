"""LLM provider abstraction with Groq → Gemini failover.

Keeps a small interface so the rest of the app doesn't care which provider
served the request. Provider failures, timeouts, and rate limits all trigger
failover. Per-tenant model preferences would slot into pick_provider(); for
the MVP we hardcode the order.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog
from tenacity import RetryError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

log = structlog.get_logger(__name__)


class LLMError(Exception):
    pass


class ProviderUnavailable(LLMError):
    pass


@dataclass
class LLMResult:
    text: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class GroqProvider:
    name = "groq"

    def __init__(self) -> None:
        from groq import AsyncGroq

        s = get_settings()
        self._client = AsyncGroq(api_key=s.GROQ_API_KEY) if s.GROQ_API_KEY else None
        self._model = s.GROQ_MODEL

    def available(self) -> bool:
        return self._client is not None

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        if not self._client:
            raise ProviderUnavailable("groq not configured")
        start = time.perf_counter()
        try:
            resp = await asyncio.wait_for(
                self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=max_tokens,
                    temperature=0.2,
                ),
                timeout=20,
            )
        except asyncio.TimeoutError as e:
            raise ProviderUnavailable("groq timeout") from e
        except Exception as e:
            raise ProviderUnavailable(f"groq error: {e}") from e
        latency_ms = int((time.perf_counter() - start) * 1000)
        usage = resp.usage
        return LLMResult(
            text=resp.choices[0].message.content or "",
            provider=self.name,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
        )


class GeminiProvider:
    name = "gemini"

    def __init__(self) -> None:
        s = get_settings()
        self._key = s.GEMINI_API_KEY
        self._model_name = s.GEMINI_MODEL
        self._model = None
        if self._key:
            import google.generativeai as genai

            genai.configure(api_key=self._key)
            self._model = genai.GenerativeModel(self._model_name)

    def available(self) -> bool:
        return self._model is not None

    async def complete(self, system: str, user: str, max_tokens: int = 1024) -> LLMResult:
        if self._model is None:
            raise ProviderUnavailable("gemini not configured")
        start = time.perf_counter()
        prompt = f"{system}\n\n{user}"
        try:
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    self._model.generate_content,
                    prompt,
                    generation_config={"max_output_tokens": max_tokens, "temperature": 0.2},
                ),
                timeout=25,
            )
        except asyncio.TimeoutError as e:
            raise ProviderUnavailable("gemini timeout") from e
        except Exception as e:
            raise ProviderUnavailable(f"gemini error: {e}") from e
        latency_ms = int((time.perf_counter() - start) * 1000)
        text = ""
        try:
            text = resp.text or ""
        except Exception:
            pass
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) if usage else 0
        out_tok = getattr(usage, "candidates_token_count", 0) if usage else 0
        return LLMResult(
            text=text,
            provider=self.name,
            input_tokens=in_tok or 0,
            output_tokens=out_tok or 0,
            latency_ms=latency_ms,
        )


_providers: list = []


def providers() -> list:
    global _providers
    if not _providers:
        for cls in (GroqProvider, GeminiProvider):
            p = cls()
            if p.available():
                _providers.append(p)
    return _providers


@retry(
    reraise=True,
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, max=2),
    retry=retry_if_exception_type(ProviderUnavailable),
)
async def _try_one(provider, system: str, user: str, max_tokens: int) -> LLMResult:
    return await provider.complete(system, user, max_tokens=max_tokens)


async def complete_with_failover(system: str, user: str, max_tokens: int = 1024) -> LLMResult:
    plist = providers()
    if not plist:
        raise LLMError("no LLM providers configured")
    last_exc: Exception | None = None
    for p in plist:
        try:
            return await _try_one(p, system, user, max_tokens)
        except (ProviderUnavailable, RetryError) as e:
            log.warning("llm.provider_failed", provider=p.name, error=str(e))
            last_exc = e
            continue
    raise LLMError(f"all providers failed: {last_exc}")


def estimate_cost_micro_usd(provider: str, in_tok: int, out_tok: int) -> int:
    s = get_settings()
    if provider == "groq":
        return int((in_tok * s.GROQ_INPUT_USD_PER_MTOK + out_tok * s.GROQ_OUTPUT_USD_PER_MTOK))
    if provider == "gemini":
        return int((in_tok * s.GEMINI_INPUT_USD_PER_MTOK + out_tok * s.GEMINI_OUTPUT_USD_PER_MTOK))
    return 0
