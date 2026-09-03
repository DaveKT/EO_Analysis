"""OpenRouter transport and cost accounting.

OpenRouter is OpenAI-compatible, so this is a thin async wrapper rather than an
SDK dependency. The model id is configuration and is never hardcoded at a call
site: a run is identified by (model, prompt_version), and results from different
models are meant to be compared, not conflated.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

# Per-attempt ceiling. Deliberately well below the old 300s: with retries, a
# single stuck request could otherwise hold a run for twenty minutes in silence.
REQUEST_TIMEOUT = 120.0
MAX_ATTEMPTS = 4


class ProviderError(RuntimeError):
    """The provider returned something unusable."""


class RetryableProviderError(ProviderError):
    """Transient: rate limit or server-side failure. Worth retrying."""


@dataclass(frozen=True)
class Completion:
    content: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    model: str
    raw: dict[str, Any]

    @property
    def truncated(self) -> bool:
        """A truncated response is invalid, not partial.

        v1 capped max_tokens at 200 and stored 29 summaries that stop
        mid-clause, with nothing recording that they were cut off.
        """
        return self.finish_reason == "length"


def _log_retry(state: Any) -> None:
    """Make retries visible.

    A stalled call is otherwise indistinguishable from a slow one: run 2 sat at
    24/25 for twenty minutes with no output, because a silent retry chain looks
    exactly like a long request.
    """
    exc = state.outcome.exception() if state.outcome else None
    print(
        f"  retry {state.attempt_number}/{MAX_ATTEMPTS} after"
        f" {type(exc).__name__ if exc else 'failure'}: {str(exc)[:120]}",
        file=sys.stderr,
        flush=True,
    )


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = REQUEST_TIMEOUT,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(
            (RetryableProviderError, httpx.TransportError, TimeoutError)
        ),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(MAX_ATTEMPTS),
        before_sleep=_log_retry,
        reraise=True,
    )
    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str = "extraction",
        max_tokens: int,
        temperature: float = 0.0,
    ) -> Completion:
        # An absolute deadline per attempt. httpx's timeout is per socket
        # operation, so a server that trickles keep-alive bytes during a long
        # generation resets it indefinitely -- observed as a run sitting at
        # 24/25 for fifteen minutes with an ESTABLISHED connection, zero CPU,
        # and the 120s read timeout never firing.
        return await asyncio.wait_for(
            self._complete_once(
                model=model, system=system, user=user, schema=schema,
                schema_name=schema_name, max_tokens=max_tokens,
                temperature=temperature,
            ),
            timeout=self._timeout,
        )

    async def _complete_once(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int,
        temperature: float,
    ) -> Completion:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "max_tokens": max_tokens,
            "temperature": temperature,
            "usage": {"include": True},
            # Without this, OpenRouter may route to a provider that ignores
            # response_format and answers in a shape of its own invention --
            # observed on EO 12890, which came back with a top-level
            # source_quote and relationships[].type. The structured-output
            # guarantee is only a guarantee if routing is constrained to
            # providers that actually implement it.
            "provider": {"require_parameters": True},
        }

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableProviderError(
                f"HTTP {response.status_code}: {response.text[:200]}"
            )
        if response.status_code != 200:
            raise ProviderError(f"HTTP {response.status_code}: {response.text[:300]}")

        body = response.json()
        # OpenRouter can report failures inside a 200 response.
        if "error" in body:
            message = json.dumps(body["error"])[:300]
            code = (body["error"] or {}).get("code")
            if code in (429, 500, 502, 503, 520):
                raise RetryableProviderError(message)
            raise ProviderError(message)

        choices = body.get("choices") or []
        if not choices:
            raise RetryableProviderError("response contained no choices")

        choice = choices[0]
        content = choice.get("message", {}).get("content") or ""
        finish_reason = choice.get("finish_reason") or "unknown"
        # An empty body with a normal finish is the provider failing, not the
        # model declining to answer. Retry rather than storing a blank row.
        if not content.strip() and finish_reason not in ("length", "content_filter"):
            raise RetryableProviderError(
                f"empty content with finish_reason={finish_reason}"
            )

        usage = body.get("usage") or {}
        return Completion(
            content=content,
            finish_reason=finish_reason,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(usage.get("cost") or 0.0),
            model=body.get("model") or model,
            raw=body,
        )
