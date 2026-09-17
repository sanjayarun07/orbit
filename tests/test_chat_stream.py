"""POST /chat/stream: the same turn as POST /chat, rendered as it happens.

Status lines while tools run, each card as its tool returns, the synthesis
token by token, then `done` with the complete response -- or `error` with
the status and detail the JSON route would have answered with. Nothing about
a turn changes when nobody is streaming: `emit` is a no-op without a channel.
"""
import asyncio
import json
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
    assert r["statusSeen"][:2] == ["Thinking…", "Routed: general"] and r["statusHidden"] is True


def test_the_client_falls_back_to_the_json_route_without_a_stream():
    from tests.test_ui_swap_flow import run_case
    assert run_case("stream_falls_back_to_json_when_the_response_is_not_a_stream")["fallback"] is True


def test_a_streamed_error_keeps_the_existing_404_handling_reachable():
    from tests.test_ui_swap_flow import run_case
    r = run_case("stream_error_event_is_fetch_shaped_for_the_existing_handling")
    assert r["ok"] is False and r["status"] == 404 and r["data"]["detail"] == "Conversation not found"
