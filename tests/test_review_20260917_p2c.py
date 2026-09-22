"""R12 and R13 of the 2026-09-17 consolidated review, as regression tests."""
import asyncio
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import sessions
from app.settings import settings

ROOT = Path(__file__).resolve().parents[1]


# --- R12 · ingestion can recover and reindex ---------------------------------

def _doc(url="https://example.com/a", content="Protocol A is deployed on a chain.", content_hash="hash1"):
    from app.knowledge.models import NormalizedDocument
    return NormalizedDocument("docs", "protocol_docs", url, "protocol:a", "A", content, content_hash)


def _embedder(name, vector):
    return SimpleNamespace(name=name, embed=lambda texts: [list(vector) for _ in texts])


def test_a_failed_graph_write_is_repaired_on_the_next_pass(monkeypatch):
    """The review's reproduction, inverted: the document and chunks committed,
    the derived writes failed, and the retry used to see the same content hash
    and stop. Completion is now recorded separately from content."""
    from app.knowledge import ingest
    from app.knowledge.store import MemoryStore

    async def run():
        store = MemoryStore()
        doc = _doc()
        monkeypatch.setattr(ingest, "get_embedder", lambda: _embedder("test", [1.0, 0.0]))
        monkeypatch.setattr(ingest, "apply_facts", AsyncMock(side_effect=RuntimeError("database interruption")))
        with pytest.raises(RuntimeError):
            await ingest.ingest_document(doc, store)
        repaired = AsyncMock(return_value=1)
        monkeypatch.setattr(ingest, "apply_facts", repaired)
        result = await ingest.ingest_document(_doc(), store)
        live = await store.live_document(doc.url)
        return result, repaired.await_count, live.metadata.get("complete")

    result, repaired_calls, complete = asyncio.run(run())
    assert repaired_calls == 1, "the retry skipped the derived writes that had failed"
    assert result[0] is True and complete is True


def test_a_completed_document_is_not_reprocessed(monkeypatch):
    """The control: content unchanged, pipeline unchanged, complete -- no work."""
    from app.knowledge import ingest
    from app.knowledge.store import MemoryStore

    async def run():
        store = MemoryStore()
        monkeypatch.setattr(ingest, "get_embedder", lambda: _embedder("test", [1.0, 0.0]))
        monkeypatch.setattr(ingest, "apply_facts", AsyncMock(return_value=0))
        first = await ingest.ingest_document(_doc(), store)
        second = await ingest.ingest_document(_doc(), store)
        return first[0], second

    first_changed, second = asyncio.run(run())
    assert first_changed is True and second == (False, 0, 0)


def test_changing_the_embedder_reindexes_unchanged_content(monkeypatch):
    """The review's reproduction, inverted: the vectors must be produced by
    the embedder queries now use, not the one that happened to be configured
    when the content last changed."""
    from app.knowledge import ingest
    from app.knowledge.store import MemoryStore

    async def run():
        store = MemoryStore()
        monkeypatch.setattr(ingest, "apply_facts", AsyncMock(return_value=0))
        monkeypatch.setattr(ingest, "get_embedder", lambda: _embedder("model-a", [1.0, 0.0]))
        assert (await ingest.ingest_document(_doc(content_hash="samehash"), store))[0]
        monkeypatch.setattr(ingest, "get_embedder", lambda: _embedder("model-b", [0.0, 1.0]))
        result = await ingest.ingest_document(_doc(content_hash="samehash"), store)
        return result, {c.metadata["embedder"] for c in store.chunks.values()}

    result, embedders = asyncio.run(run())
    assert result[0] is True and result[1] > 0, "unchanged content was not re-embedded after the embedder changed"
    assert embedders == {"model-b"}, f"old vectors survived: {embedders}"


# --- R13 · a full lock store fails loudly instead of evicting a held lock ----

def test_the_shipped_redis_does_not_evict():
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "--maxmemory-policy" in compose and "noeviction" in compose
    assert "allkeys-lru" not in compose.split("redis:")[1].split("volumes:")[0], "the lock store still evicts"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _redis_server(policy: str):
    """A throwaway redis-server with a tiny memory limit and the given policy."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("redis-server not installed; lock behaviour under memory pressure is UNVERIFIED in this run")
    port = _free_port()
    proc = subprocess.Popen([binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no",
                             # 4 MB, not 1: redis-server 7 on Ubuntu idles near 1 MB, so a 1 MB
                             # cap left allkeys-lru nothing to evict and it refused the first write (CI, 2026-09-22).
                             "--maxmemory", "4mb", "--maxmemory-policy", policy],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    return proc, port


async def _fill(client, until_error: bool) -> None:
    """Write until the store is full (noeviction raises; lru keeps going)."""
    payload = "x" * 64_000
    for index in range(200):
        try:
            await client.set(f"filler:{index}", payload)
        except Exception as exc:
            # redis-py's message is "command not allowed when used memory >
            # 'maxmemory'" -- no OOM substring; the class is the reliable signal.
            if exc.__class__.__name__ == "OutOfMemoryError" or "OOM" in str(exc):
                if until_error:
                    return
                raise
            raise


@pytest.mark.parametrize("policy", ["noeviction", "allkeys-lru"])
def test_lock_behaviour_under_memory_pressure(policy):
    """The review evicted a held 300-second lock from an allkeys-lru instance and
    acquired it again from a second caller. With noeviction the store refuses
    the write instead, which surfaces as the typed error the chat path answers
    with 503. Both policies are run so the difference is demonstrated, not
    asserted from documentation."""
    import redis.asyncio as redis_async

    proc, port = _redis_server(policy)
    try:
        async def run():
            client = redis_async.Redis(host="127.0.0.1", port=port, decode_responses=False)
            try:
                monkey = AsyncMock(return_value=client)
                original = sessions.get_redis
                sessions.get_redis = monkey
                try:
                    held = await sessions.acquire_session_turn("pressure-session")
                    await _fill(client, until_error=(policy == "noeviction"))
                    outcome = None
                    try:
                        # A second caller for the SAME conversation, while the
                        # first still holds it.
                        second = await asyncio.wait_for(sessions.acquire_session_turn("pressure-session"), timeout=5)
                        outcome = "second caller acquired the held lock"
                        await second.release()
                    except sessions.CoordinationStoreFull:
                        outcome = "store full"
                    except asyncio.TimeoutError:
                        outcome = "second caller blocked (lock still held)"
                    lock_present = await client.exists("chat_turn_lock:pressure-session")
                    await held.release()
                    return outcome, bool(lock_present)
                finally:
                    sessions.get_redis = original
            finally:
                await client.aclose()

        outcome, lock_present = asyncio.run(run())
    finally:
        proc.terminate()
        proc.wait(timeout=5)

    if policy == "allkeys-lru":
        # The reviewed failure, reproduced for contrast: the held lock is gone
        # and the second caller walks in.
        assert outcome == "second caller acquired the held lock" or not lock_present
    else:
        assert outcome in ("store full", "second caller blocked (lock still held)"), outcome
        assert outcome != "second caller acquired the held lock"
