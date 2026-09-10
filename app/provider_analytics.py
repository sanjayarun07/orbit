"""Bounded, best-effort ClickHouse telemetry for provider routing decisions."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from queue import Empty, Full, Queue
from threading import Lock, Thread
import time

from app.settings import settings


logger = logging.getLogger(__name__)
_events: Queue[tuple] = Queue(maxsize=10_000)
_started = False
_lock = Lock()


def _worker_session() -> None:
    try:
        import clickhouse_connect
        client = clickhouse_connect.get_client(
            host=settings.clickhouse_host, port=settings.clickhouse_port,
            username=settings.clickhouse_username, password=settings.clickhouse_password or "",
            database=settings.clickhouse_database, secure=settings.clickhouse_secure,
        )
        client.command("""CREATE TABLE IF NOT EXISTS orbit_provider_events (
            occurred_at DateTime64(3, 'UTC'), tool LowCardinality(String),
            provider LowCardinality(String), success Bool, latency_ms Float64
        ) ENGINE = MergeTree ORDER BY (provider, tool, occurred_at)""")
        while True:
            first = _events.get()
            batch = [first]
            try:
                while len(batch) < 100:
                    batch.append(_events.get_nowait())
            except Empty:
                pass
            client.insert(
                "orbit_provider_events", batch,
                column_names=["occurred_at", "tool", "provider", "success", "latency_ms"],
            )
    except Exception:
        logger.warning("ClickHouse provider telemetry interrupted; reconnecting")
    finally:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass


def _worker() -> None:
    while True:
        _worker_session()
        time.sleep(5)


def emit_provider_event(tool: str, provider: str, success: bool, latency_ms: float) -> None:
    global _started
    if not settings.clickhouse_host:
        return
    with _lock:
        if not _started:
            Thread(target=_worker, name="provider-clickhouse", daemon=True).start()
            _started = True
    try:
        _events.put_nowait((datetime.now(timezone.utc), tool, provider, success, latency_ms))
    except Full:
        logger.warning("ClickHouse provider telemetry queue is full; dropping event")
