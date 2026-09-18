"""Related questions under an answer (app/followups.py).

User rule (2026-09-18, comparing with Perplexity's follow-ups under "OPEN
token unlock schedule"): show follow-ups only when they are as relevant as
those -- about this answer, naming its subject -- otherwise none at all.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app import followups
from app.settings import settings

ANSWER = ("Assuming you mean OpenLedger (OPEN), its unlock schedule has a major event in September 2026: the 12-month "
          "cliff for team and investor allocations ends, after which monthly vesting begins. Investors hold 182.9M OPEN "
          "and the team 150M OPEN, each with a 12-month cliff then 36 months of monthly vesting. Community and ecosystem "
          "get 617.1M OPEN, 145.5M at TGE and the rest over 48 months. Total supply is 1 billion OPEN; 215.5M was liquid at TGE.")
QUESTION = "OPEN token unlock schedule"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "followups_enabled", True)


@pytest.mark.parametrize("line,ok", [
    ("What is OpenLedger's total circulating supply in September 2026?", True),
    ("How does OpenLedger's 48-month linear vesting curve function?", True),
    ("What percentage of OPEN tokens are allocated to investors?", True),
    ("How much OPEN unlocks when the 12-month cliff ends?", True),
    ("What's trending in crypto right now?", False),        # nothing from this answer
    ("Should I buy it before the unlock?", False),           # pronoun subject, no anchor
    ("View portfolio", False),
    ("What is the price of BONK today?", False),            # a different subject
])
def test_a_follow_up_is_kept_only_when_it_names_something_the_answer_said(line, ok):
    assert followups.grounded(line, QUESTION, ANSWER) is ok


def test_the_gate_dedupes_normalises_and_needs_at_least_two_survivors():
    raw = ("1. What is OpenLedger's total circulating supply in September 2026\n"
           "- What is OpenLedger's total circulating supply in September 2026?\n"
           "OPEN token unlock schedule\n"
           "What's trending in crypto right now?\n"
           "How do the OpenLedger community and ecosystem rewards get distributed?\n")
    kept = followups.filter_followups(raw, QUESTION, ANSWER)
    assert kept == ["What is OpenLedger's total circulating supply in September 2026?",
                    "How do the OpenLedger community and ecosystem rewards get distributed?"]
    assert followups.filter_followups("What is OpenLedger's total supply?\nWhat's trending?\n", QUESTION, ANSWER) == [], "one relevant line is not a section"


def test_which_turns_get_related_questions():
    assert followups.eligible("research", ANSWER, None)
    assert followups.eligible("general", ANSWER, None)
    assert not followups.eligible("trade", ANSWER, None)
    assert not followups.eligible("research", ANSWER, SimpleNamespace(plan_id="p1")), "a trade plan's card is the next step"
    assert not followups.eligible("research", "**BONK** exists on several chains; which one do you mean?", None), "a clarifying question is not followed up"
    assert not followups.eligible("research", "Short.", None)
    assert not followups.eligible("research", "Could not reach the provider. " * 20, None)
    settings.followups_enabled = False
    try:
        assert not followups.eligible("research", ANSWER, None)
    finally:
        settings.followups_enabled = True


def test_generate_asks_the_model_once_and_never_raises(monkeypatch):
    from app.nodes import runtime
    calls = []

    async def fake_lm(program, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(followups="What percentage of OPEN tokens are allocated to investors?\nHow does OpenLedger's 48-month linear vesting curve function?\nWhat's trending in crypto right now?")

    monkeypatch.setattr(runtime, "_call_lm", fake_lm)
    out = asyncio.run(followups.generate(QUESTION, ANSWER, "research"))
    assert out == ["What percentage of OPEN tokens are allocated to investors?", "How does OpenLedger's 48-month linear vesting curve function?"]
    assert len(calls) == 1 and calls[0]["question"] == QUESTION

    async def broken(program, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(runtime, "_call_lm", broken)
    assert asyncio.run(followups.generate(QUESTION, ANSWER, "research")) == []
    assert asyncio.run(followups.generate(QUESTION, ANSWER, "trade")) == []


def test_a_turns_related_questions_reach_the_response_and_history(monkeypatch):
    """The chat route carries them as `suggestions`; the browser renders that
    list under the answer and a tap sends the question."""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app import execution_policy, main, sessions
    from app.graph import AgentRun

    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(execution_policy, "allow_chat_request", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy, "allow_chat_request_from_ip", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy.tool_outcomes, "record_turn", AsyncMock())
    monkeypatch.setattr(execution_policy.research_gaps, "record", AsyncMock())
    monkeypatch.setattr(execution_policy.notifications, "maybe_low_credit_alert", AsyncMock())
    monkeypatch.setattr(execution_policy, "run_agent", AsyncMock(return_value=AgentRun(answer=ANSWER, trajectory=None, trade_plan=None, intent="research", capabilities=["listing_events"])))
    monkeypatch.setattr(followups, "generate", AsyncMock(return_value=["What percentage of OPEN tokens are allocated to investors?", "How much OPEN unlocks when the 12-month cliff ends?"]))
    from tests.conftest import sign_in

    client = TestClient(main.app)
    sign_in(client, email="related@example.com")
    response = client.post("/chat", json={"message": QUESTION}, headers={"X-Orbit-Device": "followups-test"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["suggestions"] == ["What percentage of OPEN tokens are allocated to investors?", "How much OPEN unlocks when the 12-month cliff ends?"]
    history = client.get(f"/chat/history/{data['session_id']}", headers={"X-Orbit-Device": "followups-test"}).json()
    assert history["messages"][-1]["suggestions"] == data["suggestions"]


def test_the_browser_renders_related_questions_and_a_tap_sends_one():
    from tests.test_ui_swap_flow import run_case
    r = run_case("related_questions_render_under_the_answer_and_send_on_tap")
    assert r["rendered"] == ["What percentage of OPEN tokens are allocated to investors?", "How much OPEN unlocks when the 12-month cliff ends?"]
    assert r["sent"] == ["How much OPEN unlocks when the 12-month cliff ends?"]
    assert r["none"] is False, "an empty list renders no section"
