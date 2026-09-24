"""POST /chat/stream: the same turn as POST /chat, rendered as it happens.

Status lines while tools run, each card as its tool returns, the synthesis
token by token, then `done` with the complete response -- or `error` with
the status and detail the JSON route would have answered with. Nothing about
a turn changes when nobody is streaming: `emit` is a no-op without a channel.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app import execution_policy, main, sessions, streaming
from app.graph import AgentRun
from app.service_errors import ServiceError
from app.settings import settings


def _events(response) -> list[dict]:
    out, event = [], None
    for line in response.iter_lines():
        line = line.decode() if isinstance(line, bytes) else line
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            out.append({"event": event, **json.loads(line[5:].strip())})
    return out


def _quiet_turn(monkeypatch):
    monkeypatch.setattr(sessions, "get_redis", AsyncMock(return_value=None))
    monkeypatch.setattr(execution_policy, "allow_chat_request", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy, "allow_chat_request_from_ip", AsyncMock(return_value=(True, 0)))
    monkeypatch.setattr(execution_policy.tool_outcomes, "record_turn", AsyncMock())
    monkeypatch.setattr(execution_policy.research_gaps, "record", AsyncMock())
    monkeypatch.setattr(execution_policy.notifications, "maybe_low_credit_alert", AsyncMock())


def test_events_arrive_in_order_and_done_carries_the_full_response(monkeypatch):
    _quiet_turn(monkeypatch)

    async def agent(message, wallet, history, context, action):
        # What the research path does while it works: a status line, a card as
        # soon as a tool returns, the synthesis token by token.
        streaming.emit("status", text="Running jupiter shield")
        await asyncio.to_thread(streaming.emit, "card", markdown="# Shield\n| ok |", tool="solana_token_security")   # from a worker thread
        for token in ("Safe ", "by the ", "dossier."):
            streaming.emit("delta", text=token)
        return AgentRun(answer="**Taken together**\n\nSafe by the dossier.\n\n---\n\n# Shield\n| ok |", trajectory={"tool_name_0": "solana_token_security", "observation_0": "ok"},
                        trade_plan=None, intent="research", capabilities=["token_security"])

    monkeypatch.setattr(execution_policy, "run_agent", agent)
    client = TestClient(main.app)
    with client.stream("POST", "/chat/stream", json={"message": "is BONK safe"}, headers={"X-Orbit-Device": "stream-test"}) as response:
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        events = _events(response)
    kinds = [e["event"] for e in events]
    assert kinds == ["status", "card", "delta", "delta", "delta", "done"], kinds
    assert events[1]["markdown"].startswith("# Shield") and events[1]["tool"] == "solana_token_security"
    done = events[-1]["data"]
    assert done["answer"].startswith("**Taken together**") and done["intent"] == "research" and done["session_id"]
    assert done["credits"]["charged"] >= 1, "a streamed turn is charged like a JSON one"


def test_a_service_error_becomes_an_error_event_with_the_json_routes_status(monkeypatch):
    _quiet_turn(monkeypatch)

    async def refuse(message, wallet, history, context, action):
        raise ServiceError(409, "Another request is already updating this chat. Wait for it to finish and retry.")

    monkeypatch.setattr(execution_policy, "run_agent", refuse)
    with TestClient(main.app).stream("POST", "/chat/stream", json={"message": "hi"}, headers={"X-Orbit-Device": "stream-err"}) as response:
        events = _events(response)
    assert events[-1]["event"] == "error" and events[-1]["status"] == 409 and "already updating" in events[-1]["detail"]


def test_emit_is_a_no_op_without_a_channel_and_reaches_the_queue_from_a_thread():
    streaming.emit("card", markdown="nobody listening")   # must not raise

    async def run():
        queue: asyncio.Queue = asyncio.Queue()
        token = streaming.attach(queue)
        try:
            streaming.emit("status", text="on the loop")
            await asyncio.to_thread(streaming.emit, "card", markdown="from a thread")
            await asyncio.sleep(0)
        finally:
            streaming.detach(token)
        streaming.emit("status", text="after detach")   # dropped
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        return items

    items = asyncio.run(run())
    assert [i["event"] for i in items] == ["status", "card"] and items[1]["markdown"] == "from a thread"


def test_the_json_route_is_unchanged(monkeypatch):
    _quiet_turn(monkeypatch)
    monkeypatch.setattr(execution_policy, "run_agent", AsyncMock(return_value=AgentRun(answer="plain", trajectory=None, trade_plan=None, intent="general", capabilities=[])))
    response = TestClient(main.app).post("/chat", json={"message": "hi"}, headers={"X-Orbit-Device": "json-test"})
    assert response.status_code == 200 and response.json()["answer"] == "plain"


# --- the browser side, through the real inline script -------------------------

def test_the_sse_parser_survives_frames_split_across_reads():
    from tests.test_ui_swap_flow import run_case
    r = run_case("sse_parser_handles_frames_split_across_reads")
    assert r["firstEvents"] == [{"event": "status", "data": {"text": "Running x"}}] and r["rest"].startswith("event: card")
    assert r["secondEvents"] == [{"event": "card", "data": {"markdown": "# C"}}] and r["secondRest"] == ""


def test_the_client_renders_cards_and_tokens_as_they_arrive_and_returns_done():
    from tests.test_ui_swap_flow import run_case
    r = run_case("stream_renders_progressively_and_returns_the_done_payload")
    assert r["ok"] and r["status"] == 200 and r["data"]["answer"] == "final"
    assert r["cards"] == ["# Shield\n| ok |"] and r["summaryHtml"] == "<p>Safe by the dossier.</p>"
    assert r["statusHidden"] is True, "the status line gives way to the answer once it starts arriving"


def test_the_client_renders_a_whole_streamed_answer_as_markdown():
    from tests.test_ui_swap_flow import run_case
    r = run_case("stream_renders_a_whole_answer_as_markdown_while_it_arrives")
    assert r["ok"]
    assert r["summaryHtml"] == "<p>I can help with <strong>markets</strong> and wallets:</p><ul><li>prices</li><li>swaps</li></ul>", r["summaryHtml"]
    assert len(r["statusSeen"]) >= 2 and all(seen.endswith("…") for seen in r["statusSeen"][:2]) and r["statusHidden"] is True, r["statusSeen"]


def test_the_status_line_speaks_in_phrases_with_the_raw_text_as_tooltip():
    """User ask (2026-09-18): "Running perplexity web search" should read like
    Claude's cooking / noodling, with an animated mark beside it."""
    from tests.test_ui_swap_flow import run_case
    r = run_case("status_line_speaks_in_phrases_and_keeps_the_raw_text_as_a_tooltip")
    assert r["families"] == {"Running perplexity web search": "search", "Running birdeye token overview": "market", "Running solana token security": "security",
                             "Running solana rpc token top holders": "holders", "Running knowledge base search": "knowledge", "Running tradingview snapshot": "chart",
                             "Running market sentiment snapshot": "sentiment", "Fetching the wallet's balances and positions": "wallet", "Reading the cards together": "synthesis",
                             "Routed: research · finance_data, web_research": "think", "Thinking…": "think"}, r["families"]
    assert r["phraseFor"] and r["rotates"] and r["title"] == "Running perplexity web search"
    css = (Path(__file__).resolve().parents[1] / "app" / "static" / "chat.css").read_text()
    assert ".stream-status::before" in css and "orbit-spin" in css and "prefers-reduced-motion" in css


def test_research_work_shows_progress_and_checked_sources():
    from tests.test_ui_swap_flow import run_case
    result = run_case("research_work_shows_progress_and_verified_source_status")
    assert result["started"]["visible"] is True
    assert "active" in result["started"]["plan"]
    assert result["sourceCount"] == "1 source link found · page checks follow"
    assert "active" in result["review"]
    assert result["reviewStatus"] == "Checking sources…"
    assert result["final"] == "1 page checked"
    assert result["calls"] == "2 evidence calls"
    assert result["legacy"] == "1 source cited · page checks not recorded"
    assert result["uncovered"] == {"heading": "No evidence available",
                                   "status": "No answer evidence was gathered"}


def test_the_client_falls_back_to_the_json_route_without_a_stream():
    from tests.test_ui_swap_flow import run_case
    assert run_case("stream_falls_back_to_json_when_the_response_is_not_a_stream")["fallback"] is True


def test_a_streamed_error_keeps_the_existing_404_handling_reachable():
    from tests.test_ui_swap_flow import run_case
    r = run_case("stream_error_event_is_fetch_shaped_for_the_existing_handling")
    assert r["ok"] is False and r["status"] == 404 and r["data"]["detail"] == "Conversation not found"


# --- a client that goes away mid-turn (installed iPhone app, 2026-09-18) ----------

def test_a_turn_finishes_and_is_saved_when_the_streaming_client_leaves(monkeypatch):
    """Seen on the phone: "Could not reach the assistant: Load failed" -- the
    connection dropped mid-turn and the server cancelled the turn, so there
    was nothing to recover. The turn now completes on its own and lands in
    history like a JSON turn."""
    _quiet_turn(monkeypatch)
    finished = asyncio.Event()

    async def slow_agent(message, wallet, history, context, action):
        streaming.emit("status", text="Running a slow tool")
        await asyncio.sleep(0.6)
        finished.set()
        return AgentRun(answer="NVDA brief", trajectory={"tool_name_0": "perplexity_finance_search", "observation_0": "x"}, trade_plan=None, intent="research", capabilities=["equity_research"])

    monkeypatch.setattr(execution_policy, "run_agent", slow_agent)
    client = TestClient(main.app)
    from tests.conftest import sign_in
    sign_in(client, email="dropped@example.com")
    session_id = None
    with client.stream("POST", "/chat/stream", json={"message": "NVDA fundamentals"}, headers={"X-Orbit-Device": "drop-test"}) as response:
        first = next(response.iter_lines())
        assert "status" in (first.decode() if isinstance(first, bytes) else first)
        # leave without reading the rest: the client is gone
    # the turn keeps running after the client left
    import time as _time
    deadline = _time.time() + 5
    while _time.time() < deadline and not finished.is_set():
        _time.sleep(0.05)
    assert finished.is_set(), "the turn was cancelled when the client left"
    # ...and its answer is in the account's newest conversation
    _time.sleep(0.3)
    convs = client.get("/me/conversations").json()
    items = convs if isinstance(convs, list) else convs.get("conversations") or convs.get("items") or []
    assert items, convs
    session_id = items[0].get("session_id") or items[0].get("id")
    history = client.get(f"/chat/history/{session_id}", headers={"X-Orbit-Device": "drop-test"}).json()
    assert history["messages"][-1]["role"] != "user" and history["messages"][-1]["content"] == "NVDA brief"


def test_the_stream_sends_keepalives_while_a_slow_tool_runs(monkeypatch):
    _quiet_turn(monkeypatch)
    monkeypatch.setattr(settings, "stream_keepalive_seconds", 0.1)

    async def slow_agent(message, wallet, history, context, action):
        await asyncio.sleep(0.35)
        return AgentRun(answer="done", trajectory=None, trade_plan=None, intent="general", capabilities=[])

    monkeypatch.setattr(execution_policy, "run_agent", slow_agent)
    with TestClient(main.app).stream("POST", "/chat/stream", json={"message": "hi"}, headers={"X-Orbit-Device": "keepalive-test"}) as response:
        lines = [(l.decode() if isinstance(l, bytes) else l) for l in response.iter_lines()]
    assert sum(1 for l in lines if l.startswith(": keepalive")) >= 2, lines
    assert any(l.startswith("event: done") for l in lines)


def test_the_browser_recovers_a_broken_stream_from_history():
    from tests.test_ui_swap_flow import run_case
    r = run_case("a_broken_stream_is_recovered_from_history")
    assert r["interrupted"] is True and r["polls"] == 2
    assert r["answer"] == "NVDA brief" and r["chart"] == {"symbol": "NASDAQ:NVDA"} and r["revision"] == 4
    assert "Waiting for the answer" in r["status"]
