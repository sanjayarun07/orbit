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
# Separate queue/worker/table from the per-call events above -- one row per
# chat turn (calls_used/cost_usd/whether the ceiling was hit), not per
# provider call. Kept as an independent pair rather than folded into the
# same worker so the two row shapes never have to share one insert call.
_turn_events: Queue[tuple] = Queue(maxsize=10_000)
_turn_started = False
_turn_lock = Lock()


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


def _turn_worker_session() -> None:
    try:
        import clickhouse_connect
        client = clickhouse_connect.get_client(
            host=settings.clickhouse_host, port=settings.clickhouse_port,
            username=settings.clickhouse_username, password=settings.clickhouse_password or "",
            database=settings.clickhouse_database, secure=settings.clickhouse_secure,
        )
        client.command("""CREATE TABLE IF NOT EXISTS orbit_turn_budget (
            occurred_at DateTime64(3, 'UTC'), intent LowCardinality(String),
            calls_used UInt16, cost_usd Float64, calls_ceiling UInt16,
            cost_ceiling Float64, capped Bool
        ) ENGINE = MergeTree ORDER BY occurred_at""")
        while True:
            first = _turn_events.get()
            batch = [first]
            try:
                while len(batch) < 100:
                    batch.append(_turn_events.get_nowait())
            except Empty:
                pass
            client.insert(
                "orbit_turn_budget", batch,
                column_names=["occurred_at", "intent", "calls_used", "cost_usd", "calls_ceiling", "cost_ceiling", "capped"],
            )
    except Exception:
        logger.warning("ClickHouse turn-budget telemetry interrupted; reconnecting")
    finally:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass


def _turn_worker() -> None:
    while True:
        _turn_worker_session()
        time.sleep(5)


def emit_turn_budget_event(
    intent: str, calls_used: int, cost_usd: float, calls_ceiling: int, cost_ceiling: float, capped: bool
) -> None:
    """Best-effort, non-blocking -- a dropped or unrecorded row never affects
    the chat turn itself, matching emit_provider_event's contract. There is
    deliberately no dashboard/audit built on this yet (see
    data/optimized_prompts/README.md-adjacent note in the loop-harness-engineering
    memory): this only starts collecting the data a real tuning decision on
    max_external_calls_per_turn/max_paid_data_cost_usd_per_turn would need."""
    global _turn_started
    if not settings.clickhouse_host:
        return
    with _turn_lock:
        if not _turn_started:
            Thread(target=_turn_worker, name="turn-budget-clickhouse", daemon=True).start()
            _turn_started = True
    try:
        _turn_events.put_nowait(
            (datetime.now(timezone.utc), intent, calls_used, cost_usd, calls_ceiling, cost_ceiling, capped)
        )
    except Full:
        logger.warning("ClickHouse turn-budget telemetry queue is full; dropping event")
