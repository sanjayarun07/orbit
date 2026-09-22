"""What Orbit remembers about a user across conversations.

Decision (2026-09-18): built in-house, not mem0. Hosted mem0 would send
chat content to a third party; self-hosted adds a vector store and an
extraction model call for little we do not already have. This is the
agreed shape:

- a `user_memories` table, per user, cascading on account deletion;
- after a signed-in research or general turn, the primary model is asked
  for the few DURABLE facts the turn revealed about the user (what they
  hold, which chains and venues they use, preferences, aversions, goals,
  experience) -- never a secret, never a full address the account does not
  already link -- and each is stored once, deduplicated by embedding;
- before a turn, the facts nearest the message are recalled into the
  conversation history as a short "what Orbit knows about you" block, so
  the general and research paths read them like earlier context;
- never into trade gating: the risk charter stays the only code-enforced
  source of truth for what a trade may do;
- Settings > Data & privacy lists every fact with its source; any can be
  deleted; all can be cleared; the export includes them.

Embeddings are stored as JSON arrays and compared in Python: a user has
tens of facts, not millions, and this keeps pgvector optional.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import uuid
from datetime import datetime, timezone

import dspy

from app.db import get_pg_pool, memory_is_the_store
from app.knowledge.embeddings import get_embedder
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_FACTS_PER_TURN = 5
MAX_FACTS_PER_USER = 200
RECALL_K = 5
RECALL_FLOOR = 0.15          # cosine below this is not "about" the message; recent facts fill instead
DEDUP_SIMILARITY = 0.85
EXTRACT_TIMEOUT_SECONDS = 10.0
KINDS = ("holding", "chain", "venue", "preference", "aversion", "goal", "experience", "other")

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_memories (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fact TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'other',
    confidence REAL NOT NULL DEFAULT 0.7,
    embedding JSONB NOT NULL,
    source_session TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS user_memories_user_idx ON user_memories (user_id) WHERE deleted_at IS NULL;
"""
_schema_ready = False
_facts: dict[str, list[dict]] = {}          # in-memory store: tests and no-Postgres deployments
_lock = threading.Lock()

# Things that must never be stored, whatever the model returns.
_SECRET = re.compile(r"\b(?:seed|mnemonic|private\s*key|password|passphrase|api[\s_-]*key|secret|bearer|token:)\b|\b0x[0-9a-fA-F]{64}\b|\b[1-9A-HJ-NP-Za-km-z]{80,}\b", re.I)
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


class DurableFacts(dspy.Signature):
    """From one chat turn, list the DURABLE facts it reveals about the USER
    (not about the market): what they hold or trade, which chains, wallets
    and venues they use, their preferences and aversions, their goals, their
    experience level. Only what the user stated or clearly implied about
    themselves; never a fact about a token's price or news; never a guess.
    Never include a seed phrase, private key, password, API key, or a full
    wallet or contract address. Each fact is one short sentence in the third
    person ("Holds BONK and WIF on Solana", "Prefers Base for DeFi",
    "Avoids leverage"). Return strict JSON: {"facts":[{"fact":"...",
    "kind":"holding|chain|venue|preference|aversion|goal|experience|other",
    "confidence":0.0-1.0}]}. Return {"facts":[]} when the turn reveals
    nothing durable, which is most turns."""

    message: str = dspy.InputField(desc="What the user wrote")
    answer: str = dspy.InputField(desc="What Orbit answered (may be truncated)")
    facts_json: str = dspy.OutputField(desc="Strict JSON as specified")


facts_extractor = dspy.Predict(DurableFacts)


def enabled() -> bool:
    return bool(settings.user_memory_enabled)


def opted_out(user: dict | None) -> bool:
    """The user's own switch (Settings > Data & privacy > Memory)."""
    return bool(((user or {}).get("preferences") or {}).get("memory_opt_out"))


# ----------------------------------------------------------------------------
# embeddings
# ----------------------------------------------------------------------------

def _embedder():
    return get_embedder()


def _embed(texts: list[str]) -> list[list[float]]:
    return _embedder().embed(texts)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


# ----------------------------------------------------------------------------
# store
# ----------------------------------------------------------------------------

async def _pool():
    global _schema_ready
    pool = await get_pg_pool()
    if pool is not None and not _schema_ready:
        try:
            async with pool.acquire() as conn:
                await conn.execute(_TABLE_SQL)
            _schema_ready = True
        except Exception:
            logger.warning("user_memory: schema not ready; using memory store", exc_info=True)
            return None
    return pool


def _row(record) -> dict:
    return {
        "id": str(record["id"]), "fact": record["fact"], "kind": record["kind"], "confidence": float(record["confidence"]),
        "embedding": json.loads(record["embedding"]) if isinstance(record["embedding"], str) else list(record["embedding"]),
        "source_session": record.get("source_session"),
        "created_at": record["created_at"].isoformat() if hasattr(record["created_at"], "isoformat") else str(record["created_at"]),
        "last_seen": record["last_seen"].isoformat() if hasattr(record["last_seen"], "isoformat") else str(record["last_seen"]),
    }


async def _all(user_id: str) -> list[dict]:
    pool = await _pool()
    if pool is not None:
        rows = await pool.fetch("SELECT id, fact, kind, confidence, embedding::text AS embedding, source_session, created_at, last_seen "
                                "FROM user_memories WHERE user_id = $1 AND deleted_at IS NULL ORDER BY last_seen DESC", user_id)
        return [_row(r) for r in rows]
    with _lock:
        return [dict(f) for f in _facts.get(user_id, []) if not f.get("deleted_at")]


async def _insert(user_id: str, fact: dict) -> None:
    pool = await _pool()
    if pool is not None:
        await pool.execute("INSERT INTO user_memories (id, user_id, fact, kind, confidence, embedding, source_session) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)",
                           fact["id"], user_id, fact["fact"], fact["kind"], fact["confidence"], json.dumps(fact["embedding"]), fact.get("source_session"))
        return
    if not memory_is_the_store():
        # Postgres is configured and unavailable: a fact about the person is
        # not kept in a worker's memory, where a later deletion cannot reach
        # it (review, 2026-09-22). Losing one fact is harmless.
        logger.info("user_memory: store unavailable; fact not kept")
        return
    with _lock:
        _facts.setdefault(user_id, []).append(fact)


async def _touch(user_id: str, fact_id: str) -> None:
    pool = await _pool()
    now = datetime.now(timezone.utc)
    if pool is not None:
        await pool.execute("UPDATE user_memories SET last_seen = $3 WHERE user_id = $1 AND id = $2", user_id, fact_id, now)
        return
    with _lock:
        for fact in _facts.get(user_id, []):
            if fact["id"] == fact_id:
                fact["last_seen"] = now.isoformat()


async def list_facts(user_id: str) -> list[dict]:
    """Every remembered fact, newest first, without embeddings."""
    return [{k: v for k, v in f.items() if k != "embedding"} for f in await _all(user_id)]


async def forget(user_id: str, fact_id: str) -> bool:
    pool = await _pool()
    if pool is not None:
        status = await pool.execute("UPDATE user_memories SET deleted_at = NOW() WHERE user_id = $1 AND id = $2 AND deleted_at IS NULL", user_id, fact_id)
        return status.endswith("1")
    with _lock:
        for fact in _facts.get(user_id, []):
            if fact["id"] == fact_id and not fact.get("deleted_at"):
                fact["deleted_at"] = datetime.now(timezone.utc).isoformat()
                return True
    return False


async def clear(user_id: str) -> int:
    n = 0
    pool = await _pool()
    if pool is not None:
        status = await pool.execute("UPDATE user_memories SET deleted_at = NOW() WHERE user_id = $1 AND deleted_at IS NULL", user_id)
        try:
            n = int(status.rsplit(" ", 1)[-1])
        except ValueError:
            n = 0
    with _lock:      # and this process's copies, whatever the database said
        live = [f for f in _facts.get(user_id, []) if not f.get("deleted_at")]
        for fact in live:
            fact["deleted_at"] = datetime.now(timezone.utc).isoformat()
        return n + len(live)


def reset_for_test() -> None:
    global _schema_ready
    with _lock:
        _facts.clear()
    _schema_ready = False


# ----------------------------------------------------------------------------
# recall
# ----------------------------------------------------------------------------

async def recall(user_id: str, message: str, k: int = RECALL_K) -> list[dict]:
    """The facts nearest the message, then the most recent, up to k."""
    if not enabled() or not user_id:
        return []
    facts = await _all(user_id)
    if not facts:
        return []
    try:
        query = (await asyncio.to_thread(_embed, [message or ""]))[0]
    except Exception:
        logger.info("user_memory: embedding failed; recalling most recent", exc_info=True)
        query = []
    # Only facts about the message. Filling the rest with recent facts put
    # unrelated ones into every turn (review, 2026-09-20).
    scored = sorted(((_cosine(query, f.get("embedding") or []), f) for f in facts), key=lambda pair: -pair[0])
    picked = [f for score, f in scored if score >= RECALL_FLOOR][:k]
    return [{key: value for key, value in f.items() if key != "embedding"} for f in picked]


def block(facts: list[dict]) -> str:
    """The recalled facts as a short block for the conversation history."""
    if not facts:
        return ""
    lines = ["What Orbit knows about this user from earlier conversations (use it for context; never for whether a trade is allowed):"]
    lines += [f"- {f['fact']}" for f in facts]
    return "\n".join(lines)


def with_block(history: str, facts: list[dict]) -> str:
    text = block(facts)
    if not text:
        return history or ""
    return f"{text}\n\n{history}" if history else text


# ----------------------------------------------------------------------------
# extraction
# ----------------------------------------------------------------------------

def parse_facts(raw: str) -> list[dict]:
    """The model's JSON as clean facts: short, third person, no secrets, no
    addresses, known kinds, at most MAX_FACTS_PER_TURN."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    out: list[dict] = []
    for item in (data.get("facts") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        fact = " ".join(str(item.get("fact") or "").split()).strip().rstrip(".")
        if not (8 <= len(fact) <= 160) or _SECRET.search(fact) or _ADDRESS.search(fact):
            continue
        kind = str(item.get("kind") or "other").lower()
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.7))))
        except (TypeError, ValueError):
            confidence = 0.7
        if confidence < 0.5:
            continue
        out.append({"fact": fact, "kind": kind if kind in KINDS else "other", "confidence": confidence})
        if len(out) >= MAX_FACTS_PER_TURN:
            break
    return out


async def remember(user_id: str, facts: list[dict], source_session: str | None = None) -> list[dict]:
    """Store facts, once each: a fact whose embedding is within
    DEDUP_SIMILARITY of an existing one refreshes that one instead."""
    if not facts:
        return []
    existing = await _all(user_id)
    if len(existing) >= MAX_FACTS_PER_USER:
        logger.info("user_memory: user %s at the fact cap; not adding", user_id[:8])
        return []
    vectors = await asyncio.to_thread(_embed, [f["fact"] for f in facts])
    stored: list[dict] = []
    for fact, vector in zip(facts, vectors):
        twin = next((e for e in existing if _cosine(vector, e.get("embedding") or []) >= DEDUP_SIMILARITY), None)
        if twin:
            await _touch(user_id, twin["id"])
            continue
        record = {"id": str(uuid.uuid4()), "fact": fact["fact"], "kind": fact["kind"], "confidence": fact["confidence"], "embedding": vector,
                  "source_session": source_session, "created_at": datetime.now(timezone.utc).isoformat(), "last_seen": datetime.now(timezone.utc).isoformat()}
        await _insert(user_id, record)
        existing.append(record)
        stored.append({k: v for k, v in record.items() if k != "embedding"})
    return stored


async def extract(user_id: str, session_id: str | None, message: str, answer: str, intent: str | None) -> list[dict]:
    """After a turn: ask the model for durable facts and store them. Never
    raises, never blocks the turn (callers schedule it)."""
    if not enabled() or not user_id or intent not in (None, "research", "general", "portfolio"):
        return []
    if len((message or "").strip()) < 4:
        return []
    from app.nodes import runtime

    try:
        result = await asyncio.wait_for(runtime._call_lm(facts_extractor, message=message[:1200], answer=(answer or "")[:2500]), timeout=EXTRACT_TIMEOUT_SECONDS)
    except Exception:
        logger.info("user_memory: extraction skipped", exc_info=True)
        return []
    facts = parse_facts(getattr(result, "facts_json", "") or "")
    if not facts:
        return []
    try:
        return await remember(user_id, facts, source_session=session_id)
    except Exception:
        logger.warning("user_memory: could not store facts", exc_info=True)
        return []
