"""Durable provider-request tracking; never accept a client-reported success."""
import asyncio
import logging
from datetime import datetime, timezone
import httpx

from app.db import get_pg_pool

_memory: dict[str, dict] = {}
TERMINAL = {"success", "failure", "refund", "refunded"}


async def track(request_id: str, session_id: str | None = None, revision: int | None = None) -> dict:
    pool = await get_pg_pool()
    if pool is None:
        if session_id:
            previous = next((r for r in _memory.values() if r.get("session_id") == session_id and r.get("revision") == revision), None)
            if previous:
                return {**previous, "execution_claimed": False}
        if request_id not in _memory and len(_memory) >= 1000:
            raise ValueError("Execution tracking capacity reached")
        claimed = request_id not in _memory
        record = _memory.setdefault(request_id, {"request_id": request_id, "status": "submission_unknown", "session_id": session_id, "revision": revision})
        return {**record, "execution_claimed": claimed}
    async with pool.acquire() as conn:
        claimed = await conn.fetchval("INSERT INTO relay_executions (request_id, status, session_id, revision) VALUES ($1,'submission_unknown',$2,$3) ON CONFLICT DO NOTHING RETURNING request_id", request_id, session_id, revision)
        if not claimed and session_id:
            row = await conn.fetchrow("SELECT request_id, status, checked_at FROM relay_executions WHERE session_id=$1 AND revision=$2", session_id, revision)
            if row:
                return {**dict(row), "execution_claimed": False}
        record = dict(await conn.fetchrow("SELECT request_id, status, checked_at FROM relay_executions WHERE request_id=$1", request_id))
        return {**record, "execution_claimed": claimed is not None}


async def for_turn(session_id: str, revision: int) -> dict | None:
    pool = await get_pg_pool()
    if pool is None:
        return next((r for r in _memory.values() if r.get("session_id") == session_id and r.get("revision") == revision), None)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT request_id,status,checked_at FROM relay_executions WHERE session_id=$1 AND revision=$2", session_id, revision)
    return dict(row) if row else None


async def get_status(request_id: str) -> dict | None:
    pool = await get_pg_pool()
    if pool is None:
        return _memory.get(request_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT request_id, status, checked_at FROM relay_executions WHERE request_id=$1", request_id)
    return dict(row) if row else None


async def reconcile() -> None:
    pool = await get_pg_pool()
    if pool is None:
        records = sorted((r for r in _memory.values() if r["status"] not in TERMINAL), key=lambda r: r.get("checked_at", ""))[:20]
    else:
        async with pool.acquire() as conn:
            records = await conn.fetch("SELECT request_id FROM relay_executions WHERE status NOT IN ('success','failure','refund','refunded') ORDER BY checked_at NULLS FIRST LIMIT 20")
    async with httpx.AsyncClient(timeout=10) as client:
        for record in records:
            request_id = record["request_id"]
            status = "submission_unknown"
            try:
                response = await client.get("https://api.relay.link/intents/status/v3", params={"requestId": request_id})
                response.raise_for_status()
                candidate = response.json().get("status")
                if candidate in TERMINAL | {"waiting", "depositing", "pending", "submitted", "delayed"}:
                    status = candidate
            except (httpx.HTTPError, ValueError, AttributeError):
                pass  # No provider evidence means unknown, never retry execution.
            now = datetime.now(timezone.utc)
            if pool is None:
                _memory[request_id].update(status=status, checked_at=now.isoformat())
            else:
                async with pool.acquire() as conn:
                    await conn.execute("UPDATE relay_executions SET status=$1, checked_at=$2 WHERE request_id=$3 AND status NOT IN ('success','failure','refund','refunded')", status, now, request_id)


async def worker() -> None:
    while True:
        try:
            await reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning("Relay reconciliation unavailable; retrying without execution")
        await asyncio.sleep(15)
