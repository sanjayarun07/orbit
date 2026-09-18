"""Cross-conversation memory (app/user_memory.py), built in-house on
2026-09-18 instead of mem0: durable facts about a signed-in user, extracted
after a turn, recalled before the next, listed and deletable in Settings,
included in the export, gone with the account. Never used for trade gating.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import accounts, main, user_memory
from app.knowledge.embeddings import HashingEmbedder
from app.settings import settings

USER = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(settings, "user_memory_enabled", True)
    monkeypatch.setattr(user_memory, "_embedder", lambda: HashingEmbedder(256))
    user_memory.reset_for_test()
    yield
    user_memory.reset_for_test()


# --- what gets stored ---------------------------------------------------------

def test_the_parser_keeps_durable_third_person_facts_and_drops_secrets_and_addresses():
    raw = ('Here you go: {"facts":[{"fact":"Holds BONK and WIF on Solana","kind":"holding","confidence":0.9},'
           '{"fact":"Prefers Base for DeFi","kind":"chain","confidence":0.8},'
           '{"fact":"Seed phrase is apple banana cherry","kind":"other","confidence":0.9},'
           '{"fact":"Main wallet is 0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460","kind":"holding","confidence":0.9},'
           '{"fact":"Maybe likes memecoins","kind":"preference","confidence":0.3},'
           '{"fact":"x","kind":"other","confidence":0.9},'
           '{"fact":"Avoids leverage","kind":"nonsense","confidence":0.95}]}')
    facts = user_memory.parse_facts(raw)
    assert [f["fact"] for f in facts] == ["Holds BONK and WIF on Solana", "Prefers Base for DeFi", "Avoids leverage"]
    assert facts[2]["kind"] == "other", "an unknown kind becomes other"
    assert user_memory.parse_facts("no json here") == [] and user_memory.parse_facts('{"facts":[]}') == []


def test_a_fact_is_stored_once_and_a_near_duplicate_refreshes_it():
    stored = asyncio.run(user_memory.remember(USER, [{"fact": "Holds BONK and WIF on Solana", "kind": "holding", "confidence": 0.9}], "s1"))
    assert len(stored) == 1
    again = asyncio.run(user_memory.remember(USER, [{"fact": "Holds BONK and WIF on Solana", "kind": "holding", "confidence": 0.9},
                                                    {"fact": "Avoids leverage", "kind": "aversion", "confidence": 0.9}], "s2"))
    assert [f["fact"] for f in again] == ["Avoids leverage"], "the twin refreshed the existing fact instead of duplicating it"
    assert [f["fact"] for f in asyncio.run(user_memory.list_facts(USER))].count("Holds BONK and WIF on Solana") == 1


def test_extraction_asks_the_model_once_and_never_raises(monkeypatch):
    from app.nodes import runtime
    calls = []

    async def lm(program, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(facts_json='{"facts":[{"fact":"Trades memecoins on pump.fun","kind":"venue","confidence":0.85}]}')

    monkeypatch.setattr(runtime, "_call_lm", lm)
    stored = asyncio.run(user_memory.extract(USER, "s1", "I mostly ape pump.fun launches", "Noted. Here is what's bonding…", "general"))
    assert [f["fact"] for f in stored] == ["Trades memecoins on pump.fun"] and len(calls) == 1

    async def broken(program, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(runtime, "_call_lm", broken)
    assert asyncio.run(user_memory.extract(USER, "s1", "another turn about BONK", "answer", "research")) == []
    assert asyncio.run(user_memory.extract(USER, "s1", "swap 1 SOL to USDC", "card", "trade")) == [], "trade turns are not mined"


# --- recall -------------------------------------------------------------------

def test_recall_returns_the_nearest_facts_then_the_most_recent_and_renders_a_block():
    asyncio.run(user_memory.remember(USER, [
        {"fact": "Holds BONK and WIF on Solana", "kind": "holding", "confidence": 0.9},
        {"fact": "Prefers Base for DeFi yield", "kind": "chain", "confidence": 0.8},
        {"fact": "Avoids leverage above 3x", "kind": "aversion", "confidence": 0.9},
    ], "s1"))
    facts = asyncio.run(user_memory.recall(USER, "should I add to my BONK bag on solana", k=2))
    assert facts[0]["fact"] == "Holds BONK and WIF on Solana"
    text = user_memory.with_block("user: hi\nassistant: hello", facts)
    assert text.startswith("What Orbit knows about this user from earlier conversations") and "- Holds BONK and WIF on Solana" in text
    assert text.endswith("user: hi\nassistant: hello") and "never for whether a trade is allowed" in text
    assert user_memory.with_block("", []) == "" and user_memory.block([]) == ""


def test_recall_is_empty_when_off_or_for_nobody(monkeypatch):
    asyncio.run(user_memory.remember(USER, [{"fact": "Holds BONK", "kind": "holding", "confidence": 0.9}], "s1"))
    assert asyncio.run(user_memory.recall("", "BONK")) == []
    monkeypatch.setattr(settings, "user_memory_enabled", False)
    assert asyncio.run(user_memory.recall(USER, "BONK")) == []


# --- the account: list, forget, clear, export, delete -----------------------------

def _client_signed_in(monkeypatch):
    from tests.conftest import sign_in

    client = TestClient(main.app)
    sign_in(client, email="memory@example.com")
    me = client.get("/me/credits")
    return client


def test_settings_can_list_forget_and_clear_and_the_export_includes_memory(monkeypatch):
    from tests.conftest import sign_in

    client = TestClient(main.app)
    sign_in(client, email="memory@example.com")
    user_id = client.get("/me/export").json()["user"]["id"]
    asyncio.run(user_memory.remember(user_id, [
        {"fact": "Holds BONK and WIF on Solana", "kind": "holding", "confidence": 0.9},
        {"fact": "Avoids leverage", "kind": "aversion", "confidence": 0.9},
    ], "s1"))
    listed = client.get("/me/memory").json()
    assert listed["enabled"] is True and [f["fact"] for f in listed["facts"]] == ["Holds BONK and WIF on Solana", "Avoids leverage"] or \
        sorted(f["fact"] for f in listed["facts"]) == ["Avoids leverage", "Holds BONK and WIF on Solana"]
    assert "embedding" not in listed["facts"][0]
    exported = client.get("/me/export").json()
    assert sorted(f["fact"] for f in exported["memory"]) == ["Avoids leverage", "Holds BONK and WIF on Solana"]
    first = listed["facts"][0]["id"]
    assert client.delete(f"/me/memory/{first}").status_code == 200
    assert client.delete(f"/me/memory/{first}").status_code == 404, "already forgotten"
    assert len(client.get("/me/memory").json()["facts"]) == 1
    assert client.delete("/me/memory").json()["count"] == 1
    assert client.get("/me/memory").json()["facts"] == []


def test_deleting_the_account_deletes_its_memory():
    asyncio.run(user_memory.remember(USER, [{"fact": "Holds BONK", "kind": "holding", "confidence": 0.9}], "s1"))
    asyncio.run(accounts.delete_user(USER))
    assert asyncio.run(user_memory.list_facts(USER)) == []


def test_memory_routes_need_a_signed_in_user():
    client = TestClient(main.app)
    assert client.get("/me/memory").status_code == 401


# --- the turn: recalled before, extracted after, never on a trade -----------------

def test_a_turn_reads_the_block_and_schedules_extraction(monkeypatch):
    from unittest.mock import AsyncMock

    from app import execution_policy, sessions
    from app.graph import AgentRun
    from tests.conftest import sign_in

    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(execution_policy, "allow_chat_request", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy, "allow_chat_request_from_ip", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy.tool_outcomes, "record_turn", AsyncMock())
    monkeypatch.setattr(execution_policy.research_gaps, "record", AsyncMock())
    monkeypatch.setattr(execution_policy.notifications, "maybe_low_credit_alert", AsyncMock())
    seen = {}

    async def agent(message, wallet, history, context, action):
        seen["history"] = history
        return AgentRun(answer="BONK is up 12% today on strong volume. " * 6, trajectory=None, trade_plan=None, intent="research", capabilities=["market_data"])

    monkeypatch.setattr(execution_policy, "run_agent", agent)
    extracted = []

    async def fake_extract(user_id, session_id, message, answer, intent):
        extracted.append((message, intent))
        return []

    monkeypatch.setattr(user_memory, "extract", fake_extract)
    client = TestClient(main.app)
    sign_in(client, email="memory@example.com")
    user_id = client.get("/me/export").json()["user"]["id"]
    asyncio.run(user_memory.remember(user_id, [{"fact": "Holds BONK and WIF on Solana", "kind": "holding", "confidence": 0.9}], "s0"))
    response = client.post("/chat", json={"message": "how is BONK doing"}, headers={"X-Orbit-Device": "memory-turn"})
    assert response.status_code == 200, response.text
    assert seen["history"].startswith("What Orbit knows about this user") and "- Holds BONK and WIF on Solana" in seen["history"]
    assert extracted == [("how is BONK doing", "research")]


# --- "what should I look at today?" with remembered holdings ----------------------

def test_a_question_about_me_takes_the_remembered_holdings_as_subjects(monkeypatch):
    from app.nodes import research

    seen = {}

    async def inner(state, sink):
        seen.update(state)
        return {"answer": "BONK +12%, WIF +4% today.", "trajectory": {"tool_name_0": "birdeye_token_overview"}}

    monkeypatch.setattr(research, "_research_node", inner)
    monkeypatch.setattr(research.answer_gate, "gate", lambda q, r: _coro(r))
    monkeypatch.setattr(research.composition, "synthesize", lambda request, cards, trajectory, advice=False: _coro(cards))
    state = {"request": "given what you know about me, what should I look at today?", "capabilities": ["web_research"], "chains": [],
             "session_context": {"user_memory": ["Holds BONK and WIF on Solana", "Prefers Base for DeFi", "Avoids leverage above 3x"]}}
    out = asyncio.run(research.research_node(state))
    assert seen["request"] in ("price and 24h change of $BONK", "price and 24h change of $WIF"), "one clause per holding, each resolved on its own"
    assert "market_data" in seen["capabilities"] and "BONK +12%" in out["answer"]
    assert research._remembered_holdings(state) == ["BONK", "WIF"], "only 'Holds …' facts name subjects; a chain preference does not"

    # without remembered holdings the question is untouched
    plain = {"request": "what should I look at today?", "capabilities": ["web_research"], "chains": [], "session_context": {}}
    asyncio.run(research.research_node(plain))
    assert seen["request"] == "what should I look at today?"


def _coro(value):
    async def run():
        return value
    return run()
