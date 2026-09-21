"""The turn log (app/turn_log.py): every chat turn recorded, including the
ones that never answered; listing, flagging, ratings and the admin routes."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import execution_policy, feedback, turn_log
from app.main import app
from app.models import AgentResponse, ChatRequest
from app.service_errors import ServiceError
from app.settings import settings


def _identity(kind="user", user_id="11111111-1111-1111-1111-111111111111"):
    return SimpleNamespace(kind=kind, account_id="acct-1", user={"id": user_id} if kind == "user" else None, api_key=None, signed_in=kind == "user")


def _response(**over):
    base = dict(answer="BONK trades at $0.000021.", session_id="s1", session_revision=3, intent="research", capabilities=["market_data"],
                trajectory={"tool_name_0": "birdeye_token_overview", "observation_0": "...", "tool_name_1": "semantic_cache"},
                validation=None, risk_assessment=None, trade_plan=None, credits={"charged": 2, "kind": "tool", "balance": 98})
    base.update(over)
    return AgentResponse(**base)


# ---- recording through the chat wrapper ----

def test_an_answered_turn_is_logged_with_its_tools_and_gate(monkeypatch):
    async def admitted(body, identity):
        return _response(answer_gate={"subject": "ok", "coverage": "partial", "resolved_by": "web"})
    monkeypatch.setattr(execution_policy, "_admitted_chat_turn", admitted)
    out = asyncio.run(execution_policy.execute_chat_turn(ChatRequest(message="BONK price"), _identity()))
    assert out.answer.startswith("BONK")
    rows = asyncio.run(turn_log.list_turns(days=1))
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok" and row["http_status"] == 200 and row["latency_ms"] is not None
    assert row["message"] == "BONK price" and row["answer"].startswith("BONK") and row["intent"] == "research"
    assert row["tools"] == ["birdeye_token_overview"]          # the cache marker is not a tool
    assert row["gate"]["resolved_by"] == "web" and row["credits"]["charged"] == 2
    assert row["user_id"] == "11111111-1111-1111-1111-111111111111" and row["session_id"] == "s1" and row["revision"] == 3
    assert row["transport"] == "json"


def test_a_refused_turn_is_logged_with_its_status_and_detail(monkeypatch):
    async def admitted(body, identity):
        raise ServiceError(402, {"error": "insufficient_credits", "balance": 0})
    monkeypatch.setattr(execution_policy, "_admitted_chat_turn", admitted)
    with pytest.raises(ServiceError):
        asyncio.run(execution_policy.execute_chat_turn(ChatRequest(message="deep dive on WIF", session_id="s9"), _identity()))
    row = asyncio.run(turn_log.list_turns(days=1))[0]
    assert row["status"] == "error" and row["http_status"] == 402 and "insufficient_credits" in row["error"]
    assert row["session_id"] == "s9" and row["answer"] is None


def test_a_crash_is_logged_as_a_500_and_still_raised(monkeypatch):
    async def admitted(body, identity):
        raise RuntimeError("boom")
    monkeypatch.setattr(execution_policy, "_admitted_chat_turn", admitted)
    with pytest.raises(RuntimeError):
        asyncio.run(execution_policy.execute_chat_turn(ChatRequest(message="x"), _identity()))
    row = asyncio.run(turn_log.list_turns(days=1))[0]
    assert row["status"] == "error" and row["http_status"] == 500 and row["error"] == "RuntimeError: boom"


def test_logging_failure_never_breaks_the_answer(monkeypatch):
    async def admitted(body, identity):
        return _response()

    async def broken(**fields):
        raise RuntimeError("log store down")
    monkeypatch.setattr(execution_policy, "_admitted_chat_turn", admitted)
    monkeypatch.setattr(turn_log, "record", broken)
    out = asyncio.run(execution_policy.execute_chat_turn(ChatRequest(message="BONK price"), _identity()))
    assert out.answer.startswith("BONK")                      # the answer still went out


def test_a_store_failure_keeps_the_row_in_memory(monkeypatch):
    async def bad_pool():
        raise RuntimeError("no db")
    monkeypatch.setattr(turn_log, "get_pg_pool", bad_pool)
    assert asyncio.run(turn_log.record(message="x", status="ok", latency_ms=1)) is not None
    assert asyncio.run(turn_log.list_turns(days=1))[0]["message"] == "x"


# ---- listing, flags, ratings ----

def _seed():
    for i, (status, http, msg, user) in enumerate((("ok", 200, "price of SOL", "u1"), ("error", 504, "deep dive on BONK", "u1"), ("ok", 200, "top holders of WIF", "u2"))):
        asyncio.run(turn_log.record(message=msg, status=status, latency_ms=100 * (i + 1), http_status=http, error="timed out" if status == "error" else None,
                                    identity=_identity(user_id=user), session_id=f"s{i}", revision=i,
                                    response=_response(answer=f"answer {i}", session_id=f"s{i}", session_revision=i) if status == "ok" else None))


def test_list_filters_by_status_user_and_text():
    _seed()
    assert [r["message"] for r in asyncio.run(turn_log.list_turns(days=1))] == ["top holders of WIF", "deep dive on BONK", "price of SOL"]
    assert [r["message"] for r in asyncio.run(turn_log.list_turns(days=1, status="error"))] == ["deep dive on BONK"]
    assert [r["message"] for r in asyncio.run(turn_log.list_turns(days=1, user_id="u2"))] == ["top holders of WIF"]
    assert [r["message"] for r in asyncio.run(turn_log.list_turns(days=1, q="timed"))] == ["deep dive on BONK"]


def test_flag_resolve_and_summary():
    _seed()
    target = asyncio.run(turn_log.list_turns(days=1, status="error"))[0]
    flagged = asyncio.run(turn_log.flag(target["id"], "wrong subject"))
    assert flagged["flagged"] and flagged["flag_note"] == "wrong subject" and flagged["resolved_at"] is None
    assert [r["id"] for r in asyncio.run(turn_log.list_turns(days=1, flagged=True))] == [target["id"]]
    s = asyncio.run(turn_log.summary(days=1))
    assert s["turns"] == 3 and s["errors"] == 1 and s["open_flags"] == 1 and s["errors_by_status"] == {"504": 1}
    assert s["latency_ms"]["p50"] == 200 and s["users"] == 2 and s["tools"][0][0] == "birdeye_token_overview"
    resolved = asyncio.run(turn_log.flag(target["id"], "fixed in 1234abcd", resolved=True))
    assert resolved["resolved_at"] is not None and asyncio.run(turn_log.summary(days=1))["open_flags"] == 0


def test_a_thumbs_down_flags_the_logged_turn(monkeypatch):
    _seed()

    async def turn(session_id, revision):
        return {"trajectory": {}}
    monkeypatch.setattr(feedback, "_turn", turn)

    async def nothing(*a, **k):
        return None
    monkeypatch.setattr(feedback, "_load", nothing)
    monkeypatch.setattr(feedback, "_save", nothing)
    asyncio.run(feedback.rate("s2", 2, "down", "wrong wallet", "acct-1"))
    row = asyncio.run(turn_log.list_turns(days=1, flagged=True))[0]
    assert row["message"] == "top holders of WIF" and "wrong wallet" in row["flag_note"]


def test_heavy_fields_are_bounded_not_dropped():
    big = {"tool_name_0": "x", "observation_0": "y" * 100_000}
    row = turn_log.build(message="m", status="ok", latency_ms=1, response=_response(trajectory=big))
    assert row["trajectory"]["note"].startswith("omitted") and row["trajectory"]["tools"] == ["x"]
    assert row["tools"] == ["x"]


# ---- admin routes ----

def test_admin_routes_list_open_and_flag(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "admin-secret")
    _seed()
    client = TestClient(app)
    assert client.get("/admin/turns").status_code == 401
    headers = {"Authorization": "Bearer admin-secret"}
    listed = client.get("/admin/turns?days=1&status=error", headers=headers).json()
    assert listed["count"] == 1 and listed["turns"][0]["error"] == "timed out" and "trajectory" not in listed["turns"][0]
    turn_id = listed["turns"][0]["id"]
    detail = client.get(f"/admin/turns/{turn_id}", headers=headers).json()
    assert detail["message"] == "deep dive on BONK"
    flagged = client.post(f"/admin/turns/{turn_id}/flag", json={"note": "fallback answered nothing"}, headers=headers).json()
    assert flagged["flagged"] and flagged["flag_note"] == "fallback answered nothing"
    assert client.get("/admin/turns/summary?days=1", headers=headers).json()["open_flags"] == 1
    assert client.get("/admin/turns/doesnotexist", headers=headers).status_code == 404
