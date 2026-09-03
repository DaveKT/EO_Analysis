"""OpenRouter transport and cost accounting.

OpenRouter is OpenAI-compatible, so this is a thin async wrapper rather than an
SDK dependency. The model id is configuration and is never hardcoded at a call
site: a run is identified by (model, prompt_version), and results from different
models are meant to be compared, not conflated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


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


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    @retry(
        retry=retry_if_exception_type(
            (RetryableProviderError, httpx.TransportError)
        ),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(4),
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
        usage = body.get("usage") or {}
        return Completion(
            content=choice.get("message", {}).get("content") or "",
            finish_reason=choice.get("finish_reason") or "unknown",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(usage.get("cost") or 0.0),
            model=body.get("model") or model,
            raw=body,
        )
