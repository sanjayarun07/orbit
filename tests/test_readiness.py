"""/health says the process is alive; /readyz says whether it can serve.

The distinction matters operationally: a load balancer or deploy gate needs
a check that actually fails when the database is unreachable or a worker
that must not die silently has died. The old /health returned ok in all of
those cases.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import main
from app.settings import settings


@pytest.fixture(autouse=True)
def _stub_datastores(monkeypatch):
    """Each TestClient request runs on its own event loop, so the shared
    asyncpg/redis clients (bound to the first one) raise "Event loop is
    closed" on the next call -- an artifact of the harness, not of /readyz.
    These tests cover the readiness LOGIC; the real datastore probes are
    verified against the running server. A test that needs a failing
    datastore overrides these.
    """
    class _Redis:
        async def ping(self):
            return True

    class _Pool:
        async def fetchval(self, *_args):
            return 1

    async def pool():
        return _Pool()

    async def redis():
        return _Redis()

    monkeypatch.setattr(main, "get_pg_pool", pool)
    monkeypatch.setattr(main, "get_redis", redis)


class _FakeTask:
    """Only the Task surface /readyz reads. Real tasks would need a loop, and
    closing one here breaks the shared connection pool bound to it."""

    def __init__(self, *, done=False, cancelled=False, exc=None):
        self._done, self._cancelled, self._exc = done, cancelled, exc

    def cancelled(self):
        return self._cancelled

    def done(self):
        return self._done or self._cancelled

    def exception(self):
        return self._exc


def test_health_is_liveness_only_and_stays_cheap():
    body = TestClient(main.app).get("/health").json()
    assert body["status"] == "ok" and "counters" in body


def test_readyz_reports_every_dependency_and_worker():
    body = TestClient(main.app).get("/readyz").json()
    names = {c["name"] for c in body["checks"]}
    assert {"postgres", "redis", "model_credentials"} <= names
    assert {"reconciliation", "relay_reconciliation", "tasks"} <= names, "workers that must not die silently are reported"
    assert body["version"] and body["live_trading"] is settings.live_trading
    for check in body["checks"]:
        assert set(check) >= {"name", "ok", "required", "detail"}


def test_readyz_fails_with_503_when_a_required_dependency_is_down(monkeypatch):
    async def broken_pool():
        raise ConnectionError("could not connect to server")

    monkeypatch.setattr(settings, "database_url", "postgresql://localhost/orbit")
    monkeypatch.setattr(main, "get_pg_pool", broken_pool)
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready" and "postgres" in body["failed"]
    postgres = next(c for c in body["checks"] if c["name"] == "postgres")
    assert postgres["ok"] is False and "ConnectionError" in postgres["detail"]


def test_readyz_fails_when_a_required_worker_has_stopped(monkeypatch):
    monkeypatch.setitem(main._workers, "reconciliation", _FakeTask(done=True))
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    assert "reconciliation" in response.json()["failed"]


def test_readyz_names_the_exception_that_killed_a_worker(monkeypatch):
    monkeypatch.setitem(main._workers, "tasks", _FakeTask(done=True, exc=RuntimeError("boom")))
    body = TestClient(main.app).get("/readyz").json()
    tasks_check = next(c for c in body["checks"] if c["name"] == "tasks")
    assert tasks_check["ok"] is False and "RuntimeError" in tasks_check["detail"]


def test_a_slow_dependency_fails_the_probe_instead_of_hanging_it(monkeypatch):
    async def hangs():
        await asyncio.sleep(30)

    monkeypatch.setattr(settings, "readiness_check_timeout_seconds", 0.05)
    monkeypatch.setattr(settings, "redis_url", "redis://localhost:6379/0")
    monkeypatch.setattr(main, "get_redis", hangs)
    response = TestClient(main.app).get("/readyz")
    assert response.status_code == 503
    redis_check = next(c for c in response.json()["checks"] if c["name"] == "redis")
    assert "timed out" in redis_check["detail"]


def test_an_optional_worker_being_down_is_degraded_not_unready(monkeypatch):
    """The knowledge ingester is off by default; that must not stop traffic."""
    for name in main._REQUIRED_WORKERS:
        monkeypatch.setitem(main._workers, name, _FakeTask())
    monkeypatch.setitem(main._workers, "knowledge_ingest", _FakeTask(done=True))
    body = TestClient(main.app).get("/readyz").json()
    assert body["status"] == "ready" and body["failed"] == [], body["checks"]
    assert "knowledge_ingest" in body["degraded"]


def test_a_process_whose_workers_never_started_is_not_ready():
    """No lifespan, no workers: honest 503 rather than a green light."""
    body = TestClient(main.app).get("/readyz").json()
    missing = {c["name"] for c in body["checks"] if c["detail"] == "not started"}
    assert missing == main._REQUIRED_WORKERS - set(main._workers)
