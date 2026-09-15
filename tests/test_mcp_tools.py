import asyncio
import time

import pytest

from app import call_budget
from app.mcp_tools import MCPGateway, _AsyncRunner
from app.settings import settings


@pytest.fixture
def gateway(monkeypatch):
    gw = MCPGateway(configs={})

    async def no_redis():
        return None

    # Force pure in-memory caching -- no real Redis in tests.
    monkeypatch.setattr(gw, "_redis_client", no_redis)
    return gw


def test_ttl_for_uses_per_tool_override(monkeypatch):
    monkeypatch.setattr(settings, "mcp_tool_cache_ttl_seconds", {"address_labels": 3600})
    monkeypatch.setattr(settings, "mcp_cache_ttl_seconds", 60)
    assert MCPGateway._ttl_for("address_labels") == 3600
    assert MCPGateway._ttl_for("address_portfolio") == 60  # not overridden -- falls back to the global default


def test_call_caches_result_and_applies_the_tool_specific_ttl(gateway, monkeypatch):
    calls = []

    async def fake_uncached_call(server_name, tool_name, arguments):
        calls.append((server_name, tool_name, arguments))
        return "result-1"

    monkeypatch.setattr(gateway, "_uncached_call", fake_uncached_call)
    monkeypatch.setattr(settings, "mcp_tool_cache_ttl_seconds", {"address_labels": 3600})

    first = asyncio.run(gateway.call("nansen", "address_labels", {"address": "0xabc"}))
    second = asyncio.run(gateway.call("nansen", "address_labels", {"address": "0xabc"}))
    assert first == "result-1"
    assert second == "result-1"
    assert len(calls) == 1  # second call served from cache, not a new external call

    key = gateway._key("nansen", "address_labels", {"address": "0xabc"})
    expires, _value = gateway._cache[key]
    assert expires - time.monotonic() > 3000  # the 3600s override, not the 60s global default


def test_call_uses_the_global_ttl_for_a_tool_with_no_override(gateway, monkeypatch):
    async def fake_uncached_call(server_name, tool_name, arguments):
        return "result-1"

    monkeypatch.setattr(gateway, "_uncached_call", fake_uncached_call)
    monkeypatch.setattr(settings, "mcp_tool_cache_ttl_seconds", {})
    monkeypatch.setattr(settings, "mcp_cache_ttl_seconds", 60)

    asyncio.run(gateway.call("dexscreener", "some_price_tool", {}))
    key = gateway._key("dexscreener", "some_price_tool", {})
    expires, _value = gateway._cache[key]
    assert expires - time.monotonic() <= 65


def test_call_returns_budget_message_without_calling_the_tool_when_budget_exhausted(gateway, monkeypatch):
    calls = []

    async def fake_uncached_call(server_name, tool_name, arguments):
        calls.append((server_name, tool_name, arguments))
        return "should not be reached"

    monkeypatch.setattr(gateway, "_uncached_call", fake_uncached_call)
    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        result = asyncio.run(gateway.call("nansen", "address_portfolio", {"address": "0xabc"}))
    finally:
        call_budget.reset_budget(token)
    assert result == "MCP tool call failed: per-turn data budget reached"
    assert calls == []


def test_async_runner_propagates_contextvars_across_its_background_thread():
    """_AsyncRunner.run() schedules coroutines onto its own persistent
    background thread/event loop via a mechanism that does NOT propagate
    contextvars by default (asyncio.run_coroutine_threadsafe does not carry
    the caller's context) -- verified live against a real MCP tool call
    before this was fixed: a per-turn budget of 0 failed to stop a real
    Nansen API call because charge_and_check() saw no active budget at all
    inside the coroutine. This is the synthetic, no-real-API-call
    regression test for that fix.
    """
    runner = _AsyncRunner()

    async def read_budget():
        budget = call_budget.current_budget()
        return budget.calls if budget is not None else None

    def call_from_a_worker_thread():
        # Mirrors _make_wrapper's sync tool wrapper: runner.run() invoked
        # from a thread other than the one holding the active budget
        # context (asyncio.to_thread in the real call chain).
        return runner.run(read_budget(), timeout=5)

    token = call_budget.start_budget(max_calls=5, max_cost_usd=1.0)
    try:
        call_budget.charge_and_check()  # calls == 1 in this context
        result = asyncio.run(asyncio.to_thread(call_from_a_worker_thread))
    finally:
        call_budget.reset_budget(token)
    assert result == 1  # the coroutine saw the *caller's* budget, not an empty one


def test_call_does_not_charge_budget_on_a_cache_hit(gateway, monkeypatch):
    call_count = 0

    async def fake_uncached_call(server_name, tool_name, arguments):
        nonlocal call_count
        call_count += 1
        return "result-1"

    monkeypatch.setattr(gateway, "_uncached_call", fake_uncached_call)
    asyncio.run(gateway.call("nansen", "address_labels", {"address": "0xabc"}))  # warms the cache, no budget active

    token = call_budget.start_budget(max_calls=0, max_cost_usd=100.0)
    try:
        # Budget is already exhausted (max_calls=0), but this must be a
        # cache hit and never even reach the budget check.
        result = asyncio.run(gateway.call("nansen", "address_labels", {"address": "0xabc"}))
    finally:
        call_budget.reset_budget(token)
    assert result == "result-1"
    assert call_count == 1
