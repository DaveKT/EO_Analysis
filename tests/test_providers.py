"""Tests for the OpenRouter transport.

Every case here is a failure that actually happened. The temperature/routing
one is the sharpest: it failed all 36 documents of a run at zero cost, with no
model ever seeing a request, and surfaced as a *quality gate* failure rather
than an error -- exactly the "silent success on garbage" shape this project
exists to eliminate.

These drive the async client with `asyncio.run` rather than pytest-asyncio, to
avoid adding a test dependency for four coroutines.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from eo.providers import OpenRouterClient, ProviderError

SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def client_with(handler) -> OpenRouterClient:
    return OpenRouterClient(
        "test-key",
        "https://openrouter.test/api/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def completion_body() -> dict:
    return {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        "model": "test/model",
    }


def recording_handler(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=completion_body())

    return handler


def run(coro):
    return asyncio.run(coro)


async def _complete(api: OpenRouterClient, **kwargs):
    try:
        return await api.complete(
            model="test/model", system="s", user="u", schema=SCHEMA,
            max_tokens=100, **kwargs,
        )
    finally:
        await api.aclose()


def test_temperature_is_omitted_when_none():
    """The exact combination that 404'd: `require_parameters` plus a parameter
    the model does not advertise leaves no eligible endpoint."""
    seen: dict = {}
    run(_complete(client_with(recording_handler(seen)), temperature=None))

    assert "temperature" not in seen
    # The guarantee that made the parameter fatal must still be sent.
    assert seen["provider"] == {"require_parameters": True}
    assert seen["response_format"]["json_schema"]["strict"] is True


def test_temperature_is_sent_when_supported():
    seen: dict = {}
    run(_complete(client_with(recording_handler(seen)), temperature=0.0))

    assert seen["temperature"] == 0.0


def test_supported_parameters_unions_endpoints():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/openai/gpt-5.4/endpoints")
        return httpx.Response(200, json={"data": {"endpoints": [
            {"supported_parameters": ["response_format", "seed"]},
            {"supported_parameters": ["response_format", "tools"]},
        ]}})

    async def go():
        api = client_with(handler)
        try:
            return await api.supported_parameters("openai/gpt-5.4")
        finally:
            await api.aclose()

    supported = run(go())
    assert supported == {"response_format", "seed", "tools"}
    assert "temperature" not in supported


def test_supported_parameters_probe_failure_is_not_fatal():
    """An unreachable probe must degrade to the minimal payload, not abort."""
    async def go():
        api = client_with(lambda request: httpx.Response(500, text="down"))
        try:
            return await api.supported_parameters("openai/gpt-5.4")
        finally:
            await api.aclose()

    assert run(go()) == set()


def test_routing_404_is_reported_as_an_error_not_an_empty_result():
    """A routing rejection must raise, and must not be retried: no amount of
    retrying makes an ineligible parameter eligible."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"error": {
            "message": "No endpoints found that can handle the requested parameters.",
            "code": 404,
        }})

    with pytest.raises(ProviderError, match="404"):
        run(_complete(client_with(handler), temperature=0.0))
    assert len(calls) == 1
