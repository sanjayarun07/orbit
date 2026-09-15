"""Role-memory reflection loop — the self-improving analyst.

Reverse-engineered from Minara's `role_memory_store` / `role_memory_recall` +
its reflectionPolicy, redesigned for Orbit:

  store  -> when a deep-dive produces a verdict, record the DECISION: asset,
            thesis, the single invalidation variable, price-at-decision, time.
  reflect -> once the outcome is known (min-age elapsed, current price fetched),
            produce ONE bias-free process correction (a lesson), using the
            realized price delta as post-hoc evidence -- the mechanism that
            avoids outcome-bias hallucination.
  recall -> on the next analysis of that asset, surface prior lessons so the
            analyst does not repeat the mistake.

SAFETY: a lesson is an ADVISORY process correction only. It never authorizes a
trade, never loosens a risk gate, and is always overridden by the static safety
rules and the human CONFIRM gate (mirrors Minara's "static safety rules and user
constraints override this lesson"). Reflection runs opportunistically when we next
fetch the asset's price (the "on_price_fetch" trigger); no unattended scheduler.

Persistence: a JSON file (settings.role_memory_path, default data/role_memory.json)
with an in-process fallback, single-instance. Multi-instance shared storage is a
follow-up.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.settings import settings

# Don't reflect before this age (avoid premature outcome reads) or after this age
# (zombie prevention) -- the Minara analysis.reasoning policy's 24h / 720h window.
_MIN_AGE_HOURS = 24.0
_MAX_AGE_HOURS = 720.0
_RECALL_LIMIT = 3

_lock = threading.Lock()
_cases: list["RoleCase"] | None = None  # lazily loaded


@dataclass
class RoleCase:
    id: str
    role: str                 # e.g. "analysis.reasoning"
    chain: str
    address: str
    symbol: str
    thesis: str               # the one-line verdict / bottom line
    flip_variable: str        # the single invalidation variable
    price_at_decision: float | None
    decided_at: float         # unix seconds
    status: str = "pending"   # "pending" | "reflected"
    reflected_at: float | None = None
    outcome_pct: float | None = None   # realized % move at reflection
    lesson: str | None = None

    def asset_key(self) -> str:
        return f"{self.chain}:{self.address.lower()}"


def _store_path() -> Path | None:
    raw = getattr(settings, "role_memory_path", None)
    return Path(raw) if raw else None


def _load() -> list[RoleCase]:
    global _cases
    if _cases is not None:
        return _cases
    _cases = []
    path = _store_path()
    if path and path.exists():
        try:
            for row in json.loads(path.read_text()):
                _cases.append(RoleCase(**row))
        except Exception:
            _cases = []
    return _cases


def _save() -> None:
    path = _store_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([asdict(c) for c in (_cases or [])], indent=1))
    except Exception:
        pass  # persistence is best-effort; the in-process copy still works this run


def reset_for_test() -> None:
    """Drop the in-process cache so the next access reloads from the configured
    path (or starts empty when no path is set) -- also used to simulate a restart."""
    global _cases
    with _lock:
        _cases = None


# Don't record a second decision for the same asset within this window -- a user
# re-running a deep-dive minutes apart is one decision, not many pending cases.
_DEDUP_HOURS = 6.0


def store_case(*, role: str, chain: str, address: str, symbol: str, thesis: str,
               flip_variable: str, price_at_decision: float | None) -> str:
    key = f"{chain}:{address.lower()}"
    now = time.time()
    with _lock:
        for c in _load():
            if c.status == "pending" and c.asset_key() == key and (now - c.decided_at) / 3600.0 < _DEDUP_HOURS:
                return c.id  # a recent pending decision already stands for this asset
    case = RoleCase(
        id=uuid.uuid4().hex[:12], role=role, chain=chain, address=address,
        symbol=symbol, thesis=thesis[:500], flip_variable=flip_variable[:300],
        price_at_decision=price_at_decision, decided_at=time.time(),
    )
    with _lock:
        _load().append(case)
        _save()
    return case.id


def recall_lessons(chain: str, address: str, limit: int = _RECALL_LIMIT) -> list[str]:
    """Reflected lessons for this asset, most recent first."""
    key = f"{chain}:{address.lower()}"
    with _lock:
        cases = [c for c in _load() if c.status == "reflected" and c.lesson and c.asset_key() == key]
    cases.sort(key=lambda c: c.reflected_at or 0, reverse=True)
    return [c.lesson for c in cases[:limit] if c.lesson]


def due_for_reflection(chain: str, address: str) -> list[RoleCase]:
    """Pending cases for this asset old enough to reflect on and not yet stale."""
    key = f"{chain}:{address.lower()}"
    now = time.time()
    with _lock:
        out = []
        for c in _load():
            if c.status != "pending" or c.asset_key() != key:
                continue
            age_h = (now - c.decided_at) / 3600.0
            if _MIN_AGE_HOURS <= age_h <= _MAX_AGE_HOURS:
                out.append(c)
        return out


def mark_stale() -> int:
    """Retire pending cases older than the max age (zombie prevention)."""
    now = time.time()
    n = 0
    with _lock:
        for c in _load():
            if c.status == "pending" and (now - c.decided_at) / 3600.0 > _MAX_AGE_HOURS:
                c.status = "reflected"
                c.lesson = None
                c.reflected_at = now
                n += 1
        if n:
            _save()
    return n


def record_reflection(case_id: str, outcome_pct: float | None, lesson: str) -> None:
    with _lock:
        for c in _load():
            if c.id == case_id:
                c.status = "reflected"
                c.reflected_at = time.time()
                c.outcome_pct = outcome_pct
                c.lesson = lesson[:400]
                break
        _save()
