"""Several tools for one ask, generally (user rule, 2026-09-17): the router
plans the best tool plus the ones that claim the request and add ground; the
research node runs them together and reads them together. One tool when one
is enough -- a second tool covering the same ground is not "needed"."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import composition, provider_router as pr
from app.nodes import research as research_mod


def _tool(name, caps, dims=None, matches=None, priority=5.0, keywords=()):
    spec = SimpleNamespace(dimensions=frozenset(dims or ()), not_for=frozenset(), fit=lambda request: 0.0) if dims is not None else None
    return pr.ProviderTool(name, "p", tuple(caps), lambda request: f"{name} output", matches=matches or pr._always_match,
                           keywords=tuple(keywords), priority=priority, spec=spec)


def _router(*tools):
    router = pr.ProviderRouter()
    for tool in tools:
        router.register(tool)
    return router


def test_the_plan_adds_a_tool_that_claims_the_request_and_covers_new_ground():
    security = _tool("goplus", ["token_security"], dims={"security"}, matches=lambda r: "safe" in r, priority=10)
    overview = _tool("birdeye", ["market_data"], dims={"liquidity", "volume"}, matches=lambda r: "0x" in r, priority=5)
    honeypot = _tool("honeypot", ["token_security"], dims={"security"}, matches=lambda r: "safe" in r, priority=8)
    router = _router(security, overview, honeypot)
    plan = [t.name for t in router.plan_across("is 0xabc safe", ("token_security", "market_data"), ())]
    assert plan[0] == "goplus" and "birdeye" in plan, plan
    assert "honeypot" not in plan, "a second security tool covers the same ground"


def test_a_catch_all_never_rides_along_and_one_tool_stays_one():
    price = _tool("price", ["market_data"], dims={"volume"}, matches=lambda r: "price" in r, priority=9)
    broad = _tool("web", ["web_research"], dims=None, priority=1)      # always-match catch-all
    router = _router(price, broad)
    assert [t.name for t in router.plan_across("price of SOL", ("market_data", "web_research"), ())] == ["price"]


def test_the_plan_is_bounded():
    tools = [_tool(f"t{i}", [f"cap{i}"], dims={f"d{i}"}, matches=lambda r: True, priority=10 - i) for i in range(6)]
    router = _router(*tools)
    assert len(router.plan_across("anything", tuple(f"cap{i}" for i in range(6)), (), limit=3)) == 3


def test_the_research_node_runs_a_multi_tool_plan_together_and_reads_it_together(monkeypatch):
    plan = [SimpleNamespace(name="goplus_token_security"), SimpleNamespace(name="birdeye_token_overview")]
    calls = []

    def invoke(name, request, chains=()):
        calls.append(name)
        return SimpleNamespace(output=f"# {name}\ncard", tool=name, provider="p", cached=False, attempted=(name,), failures=())

    router = SimpleNamespace(plan_across=lambda request, caps, chains: plan, invoke=invoke,
                             try_route_across=lambda *a, **k: (_ for _ in ()).throw(AssertionError("single route must not run when the plan ran")))
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: router)
    monkeypatch.setattr(composition.runtime, "_call_synthesis_lm", AsyncMock(return_value=SimpleNamespace(summary="Safe by the dossier; thin liquidity by the overview.")))
    out = asyncio.run(research_mod._gather_planned("is X safe", ("token_security", "market_data"), ("solana",), {"history": ""}))
    assert sorted(calls) == ["birdeye_token_overview", "goplus_token_security"]
    assert out["answer"].startswith("**Taken together**") and "# goplus_token_security" in out["answer"] and "# birdeye_token_overview" in out["answer"]
    assert out["trajectory"]["tool_name_0"] == "goplus_token_security" and out["trajectory"]["tool_name_1"] == "birdeye_token_overview"


def test_a_single_tool_plan_or_a_single_success_leaves_the_ordinary_route_to_decide(monkeypatch):
    router = SimpleNamespace(plan_across=lambda request, caps, chains: [SimpleNamespace(name="only")], invoke=lambda *a: None)
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: router)
    assert asyncio.run(research_mod._gather_planned("q", ("market_data",), (), {"history": ""})) is None
    two = [SimpleNamespace(name="a"), SimpleNamespace(name="b")]
    router = SimpleNamespace(plan_across=lambda request, caps, chains: two,
                             invoke=lambda name, *a: SimpleNamespace(output="x", tool=name, provider="p", cached=False, attempted=(), failures=()) if name == "a" else None)
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: router)
    assert asyncio.run(research_mod._gather_planned("q", ("market_data",), (), {"history": ""})) is None, "one usable card is a single answer, not a composition"
