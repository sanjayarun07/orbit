"""An empty knowledge snapshot is visible in readiness, not silent.

The router's knowledge-base anchor reads an in-memory snapshot; when it is
empty every protocol question falls to the speech model. The routing eval
measured exactly that state for a day and called it three routing misses.
"""
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app import main
from app.knowledge import tool as kb_tool


def _check(client, name):
    body = client.get("/readyz").json()
    return body, next(c for c in body["checks"] if c["name"] == name)


def test_an_empty_snapshot_with_a_knowledge_base_configured_is_degraded_not_ready_failing(monkeypatch):
    monkeypatch.setattr(main, "get_pg_pool", AsyncMock(return_value=object()))
    monkeypatch.setattr(main, "_check_postgres", AsyncMock(return_value="query ok"))
    monkeypatch.setattr(kb_tool, "snapshot", lambda: None)
    body, check = _check(TestClient(main.app), "knowledge_snapshot")
    assert check["ok"] is False and check["required"] is False and "absent" in check["detail"]
    assert "knowledge_snapshot" in body["degraded"] and "knowledge_snapshot" not in body["failed"]


def test_a_warm_snapshot_reports_its_size(monkeypatch):
    monkeypatch.setattr(main, "get_pg_pool", AsyncMock(return_value=object()))
    monkeypatch.setattr(main, "_check_postgres", AsyncMock(return_value="query ok"))

    class Snapshot:
        def __len__(self):
            return 2652

    monkeypatch.setattr(kb_tool, "snapshot", lambda: Snapshot())
    _, check = _check(TestClient(main.app), "knowledge_snapshot")
    assert check["ok"] is True and check["detail"] == "2652 entities"


def test_without_postgres_there_is_no_knowledge_base_to_report_on(monkeypatch):
    monkeypatch.setattr(main, "get_pg_pool", AsyncMock(return_value=None))
    _, check = _check(TestClient(main.app), "knowledge_snapshot")
    assert check["ok"] is True and "not configured" in check["detail"]
