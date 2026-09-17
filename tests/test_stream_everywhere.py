"""Every turn streams (user rule, 2026-09-17: "stream response back to UI").

Before this, only a composed answer streamed: a general reply, a single-tool
answer, a ReAct research turn, a deep dive and a knowledge answer all arrived
whole in `done`, so most turns looked unchanged. Now every path that ends in
model-written text goes through runtime.answer, which streams the field when
a client is listening, and every real provider call reports itself from the
router, so a status line precedes every card.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import provider_router as pr, streaming
from app.nodes import general, research as research_mod, runtime


def _capture(coro):
    """Run `coro` with a streaming channel attached; (result, events)."""
    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        token = streaming.attach(queue)
        try:
            result = await coro
            await asyncio.sleep(0)
        finally:
            streaming.detach(token)
        events = []
        while not queue.empty():
            events.append(queue.get_nowait())
        return result, events
    return asyncio.run(run())


def _fake_stream(tokens, **fields):
    async def stream(program, field, on_delta, **kwargs):
        for token in tokens:
            on_delta(token)
        return SimpleNamespace(**{field: "".join(tokens)}, **fields)
    return stream


# --- the one call ---------------------------------------------------------------

def test_answer_streams_the_field_when_a_client_listens_and_calls_whole_otherwise(monkeypatch):
    monkeypatch.setattr(runtime, "stream_answer", _fake_stream(["Hel", "lo"]))
    monkeypatch.setattr(runtime, "_call_lm", AsyncMock(return_value=SimpleNamespace(answer="whole")))
    result, events = _capture(runtime.answer(runtime.general_agent, request="hi", conversation_history=""))
    assert result.answer == "Hello"
    assert [e["text"] for e in events if e["event"] == "delta"] == ["Hel", "lo"]

    plain = asyncio.run(runtime.answer(runtime.general_agent, request="hi", conversation_history=""))
    assert plain.answer == "whole", "no channel: the ordinary bounded call"


def test_answer_on_the_synthesis_tier_uses_the_synthesis_stream_and_call(monkeypatch):
    monkeypatch.setattr(runtime, "stream_synthesis", _fake_stream(["a", "b"]))
    monkeypatch.setattr(runtime, "stream_answer", AsyncMock(side_effect=AssertionError("wrong tier")))
    monkeypatch.setattr(runtime, "_call_synthesis_lm", AsyncMock(return_value=SimpleNamespace(answer="tiered")))
    result, events = _capture(runtime.answer(runtime.knowledge_synthesizer, tier="synthesis", request="q", conversation_history="", passages="p"))
    assert result.answer == "ab" and len([e for e in events if e["event"] == "delta"]) == 2
    assert asyncio.run(runtime.answer(runtime.knowledge_synthesizer, tier="synthesis", request="q", conversation_history="", passages="p")).answer == "tiered"


def test_a_stream_that_fails_falls_back_to_the_whole_answer(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("stream broke")

    monkeypatch.setattr(runtime, "_stream", broken)
    monkeypatch.setattr(runtime, "_call_lm", AsyncMock(return_value=SimpleNamespace(answer="whole")))
    result, _ = _capture(runtime.stream_answer(runtime.general_agent, "answer", lambda t: None, request="hi", conversation_history=""))
    assert result.answer == "whole"


# --- the paths that used to arrive whole ----------------------------------------

def test_a_general_reply_streams(monkeypatch):
    monkeypatch.setattr(runtime, "stream_answer", _fake_stream(["Hi! ", "Markets, wallets, swaps."]))
    monkeypatch.setattr(runtime, "_call_lm", AsyncMock(side_effect=AssertionError("must stream while a client listens")))
    out, events = _capture(general.general_node({"request": "hi", "history": "", "session_context": {}}))
    assert out["answer"] == "Hi! Markets, wallets, swaps."
    assert "".join(e["text"] for e in events if e["event"] == "delta") == out["answer"]


def test_every_real_provider_call_reports_itself_from_the_router():
    tool = pr.ProviderTool("fake_price_lookup", "fake", ("market_data",), lambda request: "# Price\n| SOL | 1 |")
    router = pr.ProviderRouter()
    router.register(tool)
    result, events = _capture(asyncio.to_thread(router.invoke, "fake_price_lookup", "price of SOL"))
    assert result is not None and result.output.startswith("# Price")
    assert {"event": "status", "text": "Running fake price lookup"} in events


def test_a_knowledge_answer_streams_when_it_is_the_answer_and_not_as_one_card_of_many(monkeypatch):
    monkeypatch.setattr(runtime, "stream_synthesis", _fake_stream(["Uniswap v3 ", "uses ticks [1]."]))
    monkeypatch.setattr(runtime, "_call_synthesis_lm", AsyncMock(return_value=SimpleNamespace(answer="whole card")))
    answer, events = _capture(research_mod._synthesize_knowledge("how do ticks work", "[1] ticks...", ""))
    assert answer.startswith("Uniswap v3 uses ticks") and any(e["event"] == "delta" for e in events)
    card, events = _capture(research_mod._synthesize_knowledge("how do ticks work", "[1] ticks...", "", stream=False))
    assert card == "whole card" and not any(e["event"] == "delta" for e in events), "a card of a composition does not stream; the composite summary does"


def test_the_react_research_turn_streams_its_answer(monkeypatch):
    seen = {}

    async def answer(program, field="answer", *, tier="primary", **kwargs):
        seen["program"], seen["tier"], seen["request"] = program, tier, kwargs.get("request")
        streaming.emit("delta", text="Paris is sunny.")
        return SimpleNamespace(answer="Paris is sunny.", trajectory={"tool_name_0": "web_search", "observation_0": "sunny"})

    monkeypatch.setattr(runtime, "answer", answer)
    monkeypatch.setattr(runtime, "_call_lm", AsyncMock(side_effect=AssertionError("the ReAct turn must go through runtime.answer")))
    monkeypatch.setattr(research_mod, "_gather_planned", AsyncMock(return_value=None))
    monkeypatch.setattr(research_mod, "_resolve_named_token", AsyncMock(return_value=SimpleNamespace(clarification=None, pending=None, request="weather in paris today", chain=None)))
    monkeypatch.setattr(research_mod, "_resolved_token_record", lambda request, resolution: None)
    monkeypatch.setattr(research_mod, "direct_mcp_request", lambda request, token_subject=False: None)
    monkeypatch.setattr(research_mod, "is_crypto_trends_query", lambda request: False)
    monkeypatch.setattr(research_mod, "get_provider_router", lambda: SimpleNamespace(
        matched_capabilities=lambda request, chains: (), try_route_across=lambda *a, **k: None, plan_across=lambda *a, **k: []))
    out, events = _capture(research_mod.research_node({"request": "weather in paris today", "capabilities": ["web_research"], "chains": [], "history": "", "session_context": {}}))
    assert out["answer"].startswith("Paris is sunny") and seen["tier"] == "primary" and seen["request"] == "weather in paris today"
    assert isinstance(seen["program"], research_mod.dspy.ReAct)
    assert {"event": "status", "text": "Researching with the live tools"} in events
    assert [e["text"] for e in events if e["event"] == "delta"] == ["Paris is sunny."]


def test_the_streaming_runner_reports_tool_calls_and_scopes_the_repeat_guard(monkeypatch):
    """_stream: DSPy's status messages become status events, tokens become
    deltas, the final prediction is returned, and the repeat guard is scoped
    to the call (a tool the program repeats with the same arguments is
    refused inside, and free again outside)."""
    from dspy.streaming import StatusMessage, StreamResponse
    from app import repeat_guard
    import dspy

    calls = []

    def fake_tool(query: str) -> str:
        calls.append(query)
        return "ok"

    guarded = repeat_guard.guard_tools([fake_tool])[0]

    class FakeStreamer:
        def __init__(self, program, **kwargs):
            self.kwargs = kwargs

        def __call__(self, **kwargs):
            async def gen():
                yield StatusMessage("Running fake tool")
                guarded(query="x")
                with pytest.raises(RuntimeError):
                    guarded(query="x")          # the guard is active inside the stream
                yield StreamResponse(predict_name="p", signature_field_name="answer", chunk="tok", is_last_chunk=False)
                yield dspy.Prediction(answer="tok")
            return gen()

    monkeypatch.setattr(runtime.dspy, "streamify", lambda program, **kwargs: FakeStreamer(program, **kwargs))
    deltas = []
    final, events = _capture(runtime._stream(runtime.general_agent, "answer", deltas.append, runtime._primary_lm, {"request": "hi", "conversation_history": ""}))
    assert final.answer == "tok" and deltas == ["tok"]
    assert {"event": "status", "text": "Running fake tool"} in events
    guarded(query="x")                          # outside the call: no guard, no refusal
    assert calls == ["x", "x"]
