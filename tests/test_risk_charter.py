import asyncio
from types import SimpleNamespace

from app.nodes import runtime
from app.nodes import trading
from app.nodes.general import general_node
from app.experience import advance_session_context
from app.capability_router import route_capabilities, extract_charter


def _fake_plan(plan_id="plan_1", verified=True, usd=5.0, warnings=None):
    return SimpleNamespace(
        plan_id=plan_id,
        input_token=SimpleNamespace(symbol="SOL", decimals=9),
        output_token=SimpleNamespace(symbol="USDC", verified=verified),
        proposal=SimpleNamespace(amount_atomic=10_000_000, slippage_bps=50),
        input_value_usd=usd,
        quote={"priceImpactPct": "0.001"},
        warnings=warnings or [],
    )


def _patch(monkeypatch, verdict, summary="", reason=None, total=1000.0):
    async def fake_snapshot(_w):
        return {"total_usd_value": total}

    async def fake_call_lm(_program, **_kw):
        return SimpleNamespace(verdict=verdict, summary=summary, blocked_reason=reason)

    monkeypatch.setattr(trading, "build_portfolio_snapshot", fake_snapshot)
    monkeypatch.setattr(runtime, "_call_lm", fake_call_lm)


# ---- charter_risk_node ----

def test_charter_risk_passthrough_when_no_plan():
    assert asyncio.run(trading.charter_risk_node({"trade_plan": None})) == {}


def test_advisory_mode_passes_and_never_blocks(monkeypatch):
    _patch(monkeypatch, "ok", summary="0.5% of portfolio")
    plan = _fake_plan()
    out = asyncio.run(trading.charter_risk_node(
        {"trade_plan": plan, "wallet_address": "w", "session_context": {}}
    ))
    # The plan is not suppressed (no trade_plan key returned) and it's advisory.
    assert "trade_plan" not in out
    assert out["risk_assessment"].verdict == "ok"
    assert out["risk_assessment"].charter_applied is False


def test_no_charter_never_blocks_even_if_agent_says_blocked(monkeypatch):
    # Hard rule: the Risk agent can only make things stricter WITH a charter.
    _patch(monkeypatch, "blocked", summary="x", reason="y")
    plan = _fake_plan()
    out = asyncio.run(trading.charter_risk_node(
        {"trade_plan": plan, "wallet_address": "w", "session_context": {}}
    ))
    assert "trade_plan" not in out
    assert out["risk_assessment"].verdict == "ok"


def test_hard_veto_supersedes_plan_and_suppresses_card(monkeypatch):
    _patch(monkeypatch, "blocked", summary="exceeds your cap",
           reason="Trade is $50 but your cap is $10 — trim to $10.")
    superseded = []

    async def fake_supersede(plan_id):
        superseded.append(plan_id)

    monkeypatch.setattr(trading, "mark_plan_superseded", fake_supersede)
    plan = _fake_plan(plan_id="plan_block")
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": plan,
        "wallet_address": "w",
        "session_context": {"risk_charter": "only verified tokens, max $10 per trade"},
    }))
    assert out["trade_plan"] is None                       # card suppressed
    assert superseded == ["plan_block"]                    # CONFIRM token inert
    assert "Blocked by your risk charter" in out["answer"]
    assert out["risk_assessment"].verdict == "blocked"
    assert out["risk_assessment"].charter_applied is True


def test_risk_agent_failure_falls_back_to_advisory_not_a_block(monkeypatch):
    async def fake_snapshot(_w):
        return {"total_usd_value": 100.0}

    async def boom(_program, **_kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(trading, "build_portfolio_snapshot", fake_snapshot)
    monkeypatch.setattr(runtime, "_call_lm", boom)
    out = asyncio.run(trading.charter_risk_node({
        "trade_plan": _fake_plan(),
        "wallet_address": "w",
        "session_context": {"risk_charter": "max $10 per trade"},
    }))
    assert "trade_plan" not in out  # a Risk-agent crash must not block a trade
    assert out["risk_assessment"].verdict == "ok"


# ---- charter commands ----

def test_charter_commands_route_to_control_not_the_trade_path():
    for message in (
        "set my risk charter: only verified tokens, max $10 per trade",
        "show my risk charter",
        "clear my risk charter",
    ):
        route = route_capabilities(message)
        assert route is not None and route.intent == "general" and route.reason == "risk_charter", message


def test_charter_extract_and_persist_and_clear():
    assert extract_charter("set my risk charter: max 1% of portfolio") == "max 1% of portfolio"
    ctx = advance_session_context({}, "set my risk charter: only verified tokens, max $10", None, "general", [], [], None)
    assert "only verified tokens" in ctx["risk_charter"]
    ctx2 = advance_session_context(ctx, "clear my risk charter", None, "general", [], [], None)
    assert ctx2["risk_charter"] is None
    # A plain message leaves an existing charter untouched.
    ctx3 = advance_session_context(ctx, "what is the price of SOL", None, "research", [], [], None)
    assert ctx3["risk_charter"] == ctx["risk_charter"]


def test_general_node_charter_replies():
    out_set = asyncio.run(general_node({"request": "set my risk charter: max $10 per trade", "session_context": {}}))
    assert "Risk charter updated" in out_set["answer"]
    out_show = asyncio.run(general_node({"request": "show my risk charter", "session_context": {"risk_charter": "max $10 per trade"}}))
    assert "max $10 per trade" in out_show["answer"]
    out_empty = asyncio.run(general_node({"request": "show my risk charter", "session_context": {}}))
    assert "No risk charter" in out_empty["answer"]
