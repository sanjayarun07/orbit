import asyncio

from app import graph as graph_module
from app.call_budget import charge_and_check


async def _fake_ainvoke(_initial_state):
    """Stands in for the compiled LangGraph's real node execution -- charges
    the shared per-turn budget the same way a real node would (via
    app.call_budget.charge_and_check, e.g. through ProviderRouter.route),
    then returns a minimal final AgentState."""
    charge_and_check(0.01)
    charge_and_check(0.01)
    return {"answer": "hi there", "intent": "research", "capabilities": ["web_research"]}


def test_run_agent_emits_one_turn_budget_event_reflecting_real_usage(monkeypatch):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(graph_module.graph, "ainvoke", _fake_ainvoke)
    monkeypatch.setattr(graph_module, "emit_turn_budget_event", capture)

    asyncio.run(graph_module.run_agent("What's the price of SOL?", "wallet123"))

    assert captured["intent"] == "research"
    assert captured["calls_used"] == 2
    assert captured["cost_usd"] == 0.02
    assert captured["capped"] is False


def test_turn_budget_event_still_emitted_when_the_graph_raises(monkeypatch):
    """The finally block must snapshot and emit even on failure -- and must
    never itself raise or mask the original exception."""
    captured = {}

    async def failing_ainvoke(_initial_state):
        charge_and_check(0.01)
        raise RuntimeError("boom")

    def capture(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(graph_module.graph, "ainvoke", failing_ainvoke)
    monkeypatch.setattr(graph_module, "emit_turn_budget_event", capture)

    try:
        asyncio.run(graph_module.run_agent("What's the price of SOL?", "wallet123"))
        raised = False
    except RuntimeError:
        raised = True

    assert raised, "the original exception must still propagate"
    assert captured["intent"] == "unknown"
    assert captured["calls_used"] == 1
