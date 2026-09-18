"""The answer gate (app/answer_gate.py): a research answer ships only when a
judge agrees it is about the subject the user named and gives what they
asked for. User rule (2026-09-18): correct over fast; never a wrong answer.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import answer_gate
from app.nodes import research
from app.settings import settings


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "answer_gate_enabled", True)


def _judge(monkeypatch, *verdicts):
    """A fake judge answering the given verdicts in order."""
    from app.nodes import runtime
    queue = list(verdicts)
    seen = []

    async def lm(program, **kwargs):
        seen.append(kwargs)
        v = queue.pop(0)
        return SimpleNamespace(verdict=v[0], subject=v[1], missing=v[2])

    monkeypatch.setattr(runtime, "_call_lm", lm)
    return seen


CALENDAR = "# Market events · 2026-09-18 → 2026-09-25\n- State Employment and Unemployment · 10:00 ET · bls.gov\n- U.S. International Transactions · bea.gov\n" * 3


def test_which_answers_are_checked():
    assert answer_gate.eligible("A" * 100, {})
    assert not answer_gate.eligible("Which token's unlock? Name it and I'll pull its schedule.", {})
    assert not answer_gate.eligible("A" * 100, {"pending_token": {"symbol": "OPEN"}})
    assert not answer_gate.eligible("A" * 100, {"trade_plan": object()})
    assert not answer_gate.eligible("short", {})
    settings.answer_gate_enabled = False
    try:
        assert not answer_gate.eligible("A" * 100, {})
    finally:
        settings.answer_gate_enabled = True


@pytest.mark.parametrize("raw,expected", [("answers", "answers"), ("Wrong_subject", "wrong_subject"), ("missing: unlock dates", "missing"), ("asks", "asks"), ("", "answers"), ("banana", "answers")])
def test_a_verdict_is_read_strictly_and_never_blocks_by_accident(raw, expected):
    assert answer_gate.parse_verdict(raw) == expected


def test_an_answer_about_the_wrong_subject_is_replaced_by_the_webs_answer(monkeypatch):
    """FARTCOIN got the macro calendar: the judge says wrong_subject, the web
    answers about FARTCOIN, the calendar is dropped."""
    seen = _judge(monkeypatch, ("wrong_subject", "the US macro calendar", "anything about FARTCOIN"), ("answers", "FARTCOIN", ""))

    async def web(question):
        return "FARTCOIN is a Solana memecoin trading at about $0.15, up 8% in 24h; market cap $150M; the September 9 Jupiter routing discussion was the catalyst."

    monkeypatch.setattr(answer_gate, "_web_answer", web)
    out = asyncio.run(answer_gate.gate("What's happening with FARTCOIN?", {"answer": CALENDAR, "trajectory": {"tool_name_0": "market_event_calendar"}}))
    assert out["answer"].startswith("FARTCOIN is a Solana memecoin") and "comes from the web" in out["answer"]
    assert "Market events" not in out["answer"], "a wrong-subject answer is not kept underneath"
    assert out["trajectory"]["tool_name_0"] == "perplexity_context_search" and out["answer_gate"]["resolved_by"] == "web"
    assert len(seen) == 2 and seen[0]["question"] == "What's happening with FARTCOIN?"


def test_a_right_subject_missing_answer_keeps_the_tools_cards_under_the_webs(monkeypatch):
    _judge(monkeypatch, ("missing", "EigenLayer", "who its investors are"), ("answers", "EigenLayer", ""))

    async def web(question):
        return "EigenLayer raised $100M from a16z in February 2024; earlier rounds were led by Blockchain Capital and Polychain."

    monkeypatch.setattr(answer_gate, "_web_answer", web)
    tools = "# EigenLayer · TVL\n| TVL | $12.4B |\n" * 4
    out = asyncio.run(answer_gate.gate("Who are the investors backing EigenLayer?", {"answer": tools, "trajectory": {}}))
    assert out["answer"].startswith("EigenLayer raised $100M") and "did not cover this" in out["answer"] and "# EigenLayer · TVL" in out["answer"]


def test_when_the_web_cannot_answer_either_the_user_gets_a_question(monkeypatch):
    _judge(monkeypatch, ("wrong_subject", "Mercury Systems, a defense company", "Mercury the token"))

    async def web(question):
        return None

    monkeypatch.setattr(answer_gate, "_web_answer", web)
    out = asyncio.run(answer_gate.gate("Mercury token outlook", {"answer": "Mercury Systems (NASDAQ: MRCY) reported record bookings " * 5, "trajectory": {}}))
    assert out["answer"].startswith("I couldn't find Mercury the token") and "Mercury Systems" in out["answer"] and out["answer_gate"]["resolved_by"] == "ask"
    assert out["trajectory"] is None


def test_a_correct_answer_passes_untouched_and_a_broken_judge_never_blocks(monkeypatch):
    _judge(monkeypatch, ("answers", "ARB", ""))
    result = {"answer": "# Unlock schedule — ARB\n| 2026-10-15 | 56,125,000 tokens | cliff | insiders |\n" * 3, "trajectory": {"tool_name_0": "defillama_token_unlocks"}}
    assert asyncio.run(answer_gate.gate("when is the next ARB unlock", result)) is result

    from app.nodes import runtime

    async def broken(program, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(runtime, "_call_lm", broken)
    assert asyncio.run(answer_gate.gate("when is the next ARB unlock", result)) is result


def test_the_research_node_runs_the_gate_last(monkeypatch):
    async def inner(state, sink):
        return {"answer": CALENDAR, "trajectory": {"tool_name_0": "market_event_calendar"}}

    async def gate(question, result):
        return {**result, "answer": f"gated for {question}"}

    monkeypatch.setattr(research, "_research_node", inner)
    monkeypatch.setattr(answer_gate, "gate", gate)
    out = asyncio.run(research.research_node({"request": "What's happening with FARTCOIN?", "capabilities": ["web_research"], "chains": [], "session_context": {}}))
    assert out["answer"] == "gated for What's happening with FARTCOIN?"
