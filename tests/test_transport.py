from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from transport import (
    CallBudget,
    ProxyCallError,
    ProxyTransport,
    QuotaError,
    RetryPolicy,
    merge_catalogues,
    provider_family,
)


def test_catalogue_merges_all_advertised_metadata_shapes():
    raw = {
        "data": [
            {"id": "gpt-5.6-sol", "owned_by": "openai"},
            {"id": "claude-x", "owned_by": "antigravity"},
            {"id": "image-route", "owned_by": "vendor"},
            {"id": "missing-meta", "owned_by": "vendor"},
        ]
    }
    metadata = {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "context_window": 100_000,
                "supported_reasoning_levels": [
                    {"effort": "low"},
                    {"effort": "medium"},
                ],
                "supported_in_api": True,
                "supports_parallel_tool_calls": True,
                "input_modalities": ["text", "image"],
                "priority": 1,
            },
            {
                "id": "claude-x",
                "max_context_window": {"tokens": 200_000},
                "supported_reasoning_efforts": ["low", "high"],
                "api_support": ["responses"],
                "supports_tools": True,
                "modalities": [{"type": "text"}],
            },
            {
                "model": "image-route",
                "context_length": 10_000,
                "efforts": {"low": True},
                "supports_api": True,
                "inputs": "text",
                "outputs": ["image"],
            },
            {
                "slug": "metadata-only",
                "context_window": 10_000,
                "supported_reasoning_levels": ["low"],
                "supported_in_api": True,
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        ]
    }
    catalogue = {item.id: item for item in merge_catalogues(raw, metadata)}
    assert catalogue["gpt-5.6-sol"].efforts == ["low", "medium"]
    assert "worker" in catalogue["gpt-5.6-sol"].roles
    assert catalogue["claude-x"].context_window == 200_000
    assert catalogue["claude-x"].family == "anthropic"
    assert not catalogue["image-route"].eligible
    assert not catalogue["missing-meta"].eligible
    assert catalogue["missing-meta"].listed_only
    assert "metadata-only" not in catalogue


def test_family_normalization_prefers_model_family():
    assert provider_family("gpt-oss-120b", "antigravity") == "openai"
    assert provider_family("claude-opus", "antigravity") == "anthropic"
    assert provider_family("gemini-pro", "antigravity") == "google"
    assert provider_family("qwen3.8", "qoder") == "qwen"


def client_with(handler):
    return httpx.AsyncClient(
        base_url="http://proxy.invalid",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_context_400_gets_one_coverage_repack_retry():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, text="maximum context tokens exceeded")
        return httpx.Response(200, json={"output_text": "OK", "usage": {}})

    transport = ProxyTransport(
        SimpleNamespace(base_url="http://x", api_key="k"),
        budget=CallBudget(2),
        client=client_with(handler),
    )
    text, _ = await transport.ask(
        run_id="r",
        participant="p",
        model="m",
        effort="low",
        prompt="too long",
        stage="opinion",
        repack=lambda: "repacked",
    )
    assert text == "OK"
    assert calls == 2
    await transport.client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error", "expected_calls"),
    [
        (429, "quota", QuotaError, 1),
        (500, "broken", ProxyCallError, 3),
        (400, "invalid effort", ProxyCallError, 1),
    ],
)
async def test_proxy_failure_classification(status, body, error, expected_calls):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, text=body)

    transport = ProxyTransport(
        SimpleNamespace(base_url="http://x", api_key="k"),
        budget=CallBudget(2),
        client=client_with(handler),
        retry_policy=RetryPolicy(base_delay=0, jitter=False),
    )
    with pytest.raises(error):
        await transport.ask(
            run_id="r",
            participant="p",
            model="m",
            effort="low",
            prompt="x",
            stage="opinion",
        )
    assert calls == expected_calls
    assert transport.budget.used == 0
    await transport.client.aclose()


@pytest.mark.asyncio
async def test_retry_success_charges_one_logical_call():
    calls = 0
    budget = CallBudget(1)

    def handler(request):
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"output_text": "OK", "usage": {}})

    transport = ProxyTransport(
        SimpleNamespace(base_url="http://x", api_key="k"),
        budget=budget,
        client=client_with(handler),
        retry_policy=RetryPolicy(base_delay=0, jitter=False),
    )
    text, _ = await transport.ask(
        run_id="r",
        participant="p",
        model="m",
        effort="low",
        prompt="x",
        stage="opinion",
    )
    assert text == "OK"
    assert calls == 3
    assert budget.used == 1
    assert len(budget.events) == 1
    await transport.client.aclose()


@pytest.mark.asyncio
async def test_timeout_is_retried_without_budget_charge():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    transport = ProxyTransport(
        SimpleNamespace(base_url="http://x", api_key="k"),
        budget=CallBudget(2),
        client=client_with(handler),
        retry_policy=RetryPolicy(base_delay=0, jitter=False),
    )
    with pytest.raises(ProxyCallError, match="timeout"):
        await transport.ask(
            run_id="r",
            participant="p",
            model="m",
            effort="low",
            prompt="x",
            stage="opinion",
        )
    assert calls == 3
    assert transport.budget.used == 0
    await transport.client.aclose()
