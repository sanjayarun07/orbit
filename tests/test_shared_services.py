"""Transport boundaries and shared scheduling behavior after API extraction."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import execution_policy, main, mcp_server, task_scheduling, tasks, tasks_nl
from app.service_errors import ServiceError


def test_http_and_mcp_share_execution_and_translate_service_errors(monkeypatch):
    calls = []

    async def deny(body, identity):
        calls.append(body.message)
        raise ServiceError(429, "Try again shortly", headers={"Retry-After": "7"})

    monkeypatch.setattr(execution_policy, "execute_chat_turn", deny)
    response = TestClient(main.app).post("/chat", json={"message": "hello"})
    assert response.status_code == 429
    assert response.json() == {"detail": "Try again shortly"}
    assert response.headers["retry-after"] == "7"
    result = asyncio.run(mcp_server.orbit_chat("hello"))
    assert result == {"error": "Try again shortly", "status": 429}
    assert calls == ["hello", "hello"]


def test_execution_status_uses_shared_service_for_both_transports(monkeypatch):
    calls = []

    async def status(signature):
        calls.append(signature)
        return {"signature": signature, "status": "finalized", "finalized": True}

    monkeypatch.setattr(execution_policy, "solana_execution_status", status)
    signature = "1" * 64
    http = TestClient(main.app).get(f"/executions/solana/{signature}")
    mcp = asyncio.run(mcp_server.orbit_execution_status(signature))
    assert http.status_code == 200 and http.json() == mcp
    assert calls == [signature, signature]


def test_scheduling_denies_other_users_before_running_or_mutating(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("A foreign task reached the execution/mutation layer")

    user = {"id": "owner", "plan_id": "free"}
    task = asyncio.run(task_scheduling.create_task(user, "reminder", {"message": "Review"}, {"every_minutes": 5}))
    monkeypatch.setattr(tasks, "run_task", forbidden)
    monkeypatch.setattr(tasks, "update_task", forbidden)
    for operation in (task_scheduling.run_now(task["id"], "other"), task_scheduling.update_task(task["id"], "other", status="paused")):
        with pytest.raises(ServiceError) as denied:
            asyncio.run(operation)
        assert denied.value.status_code == 404


def test_chat_resume_and_service_resume_use_the_same_schedule_policy(monkeypatch):
    user = {"id": "owner", "plan_id": "free"}
    task = asyncio.run(task_scheduling.create_task(user, "reminder", {"message": "Review"}, {"every_minutes": 5}))
    asyncio.run(task_scheduling.update_task(task["id"], user["id"], status="paused"))
    expected = datetime.now(timezone.utc) + timedelta(hours=1)
    monkeypatch.setattr(tasks, "next_run", lambda *args, **kwargs: expected)
    reply = asyncio.run(tasks_nl.handle("resume task 1", user))
    resumed = asyncio.run(tasks.get_task(task["id"]))
    assert "active" in reply and resumed["next_run_at"] == expected.isoformat()
    # Repeating resume must not shift an already active task's next occurrence.
    monkeypatch.setattr(tasks, "next_run", lambda *args, **kwargs: pytest.fail("Active task was rescheduled"))
    asyncio.run(tasks_nl.handle("resume task 1", user))
    assert asyncio.run(tasks.get_task(task["id"]))["next_run_at"] == expected.isoformat()
