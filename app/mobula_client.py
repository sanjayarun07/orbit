"""One door to Mobula for every module that calls it.

Review of 2026-09-20: the bundle check, the deep dive and the wallet tools
each called Mobula directly, so a "single" router call could be twenty-six
requests, retries repeated the whole sequence, and three concurrent checks
could exceed the configured per-minute limit while the router's quota,
cost and breaker never saw them. The same review found the configured base
URL ignored by hardcoded hosts.

Every Mobula request now goes through `get()`: the base comes from
settings, a process-wide token bucket enforces `mobula_requests_per_minute`
across all callers, at most `MAX_CONCURRENT` requests are in flight, and
each request is counted for the provider metrics. A caller that would
exceed the budget waits (bounded) rather than bursting; a caller that would
wait too long fails fast with a clear error rather than stalling a turn.

2026-09-21, found live: the snapshot ledger's worker (app/holder_snapshots.py)
drew its full per-minute allowance in a four-second burst, Mobula answered
429 to everything for a while, and a user's wallet-portfolio turn fell back
to a worse provider. Three rules follow:

- The bucket holds at most `mobula_burst` tokens, so sixty per minute means
  about one per second sustained, never sixty at once.
- A 429 (or a 5xx) starts a cooldown of `mobula_cooldown_seconds` (or the
  Retry-After header). Background callers are refused during it; a user's
  call waits for it to end, within its usual bound.
- Background work runs in a second lane: it takes a token only while the
  bucket is at least half full, so a user's turn always finds one.
"""
from __future__ import annotations

import logging
import threading
import time
from urllib.parse import urlsplit

import httpx

from app.metrics import increment
from app.settings import settings

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 6
MAX_WAIT_SECONDS = 8.0


class MobulaBudgetExceeded(RuntimeError):
    """The per-minute Mobula budget is spent (or Mobula asked us to slow
    down); the caller should degrade."""


def origin() -> str:
    parts = urlsplit(settings.mobula_base_url)
    return f"{parts.scheme}://{parts.netloc}"


def url(version: int, path: str) -> str:
    return f"{origin()}/api/{version}{path}"


def _workers() -> int:
    return max(1, int(getattr(settings, "uvicorn_workers", 1) or 1))


def _rate() -> float:
    """This process's share of the per-minute allowance."""
    return max(1.0, float(settings.mobula_requests_per_minute) / _workers())


def _capacity() -> float:
    return float(max(1, min(int(settings.mobula_burst) // _workers() or 1, int(_rate()))))


class _Bucket:
    """Token bucket: `rate` tokens per minute refilled continuously, holding
    at most `mobula_burst` at once. A cooldown (set on 429/5xx) blocks every
    take until it passes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens = _capacity()
        self._stamp = time.monotonic()
        self._cooldown_until = 0.0

    def _refill(self) -> None:
        now = time.monotonic()
        rate = _rate()
        self._tokens = min(_capacity(), self._tokens + (now - self._stamp) * rate / 60.0)
        self._stamp = now

    def take(self, timeout: float, *, background: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    if background:
                        return False
                    wait = self._cooldown_until - now
                else:
                    self._refill()
                    floor = _capacity() / 2.0 if background else 1.0
                    if self._tokens >= floor:
                        self._tokens -= 1.0
                        return True
                    if background:
                        return False  # a background caller never waits for a user's tokens
                    wait = (1.0 - self._tokens) * 60.0 / _rate()
            if time.monotonic() + wait > deadline:
                return False
            time.sleep(min(wait, 0.25))

    def cool_down(self, seconds: float) -> None:
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(1.0, seconds))
            self._tokens = 0.0

    def cooling(self) -> bool:
        with self._lock:
            return time.monotonic() < self._cooldown_until

    def background_ok(self) -> bool:
        """Would a background take succeed right now (no token spent)?"""
        with self._lock:
            if time.monotonic() < self._cooldown_until:
                return False
            self._refill()
            return self._tokens >= _capacity() / 2.0

    def reset(self) -> None:
        with self._lock:
            self._tokens = _capacity()
            self._stamp = time.monotonic()
            self._cooldown_until = 0.0


_bucket = _Bucket()
_inflight = threading.BoundedSemaphore(MAX_CONCURRENT)


def background_ok() -> bool:
    """For a background loop to check before starting a unit of work."""
    return bool(settings.mobula_api_key) and _bucket.background_ok()


def cooling() -> bool:
    return _bucket.cooling()


def _retry_after(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    try:
        return float(header) if header else float(settings.mobula_cooldown_seconds)
    except ValueError:
        return float(settings.mobula_cooldown_seconds)


def get(version: int, path: str, params: dict, timeout: float | None = None, *, background: bool = False) -> dict | list:
    """GET one Mobula endpoint under the shared budget; the unwrapped data.

    `background=True` is the second lane: it never waits, takes a token only
    while the bucket is at least half full, and is refused during a cooldown,
    so a scheduled job can never crowd out a user's turn."""
    if not settings.mobula_api_key:
        raise RuntimeError("Mobula is not configured")
    if not _bucket.take(0.0 if background else MAX_WAIT_SECONDS, background=background):
        increment("mobula_budget_exceeded")
        raise MobulaBudgetExceeded(
            "Mobula asked us to slow down; try again in a moment" if _bucket.cooling()
            else f"Mobula budget of {settings.mobula_requests_per_minute}/min is spent; try again in a moment")
    increment("mobula_requests")
    with _inflight:
        with httpx.Client(timeout=timeout or settings.provider_request_timeout_seconds) as client:
            response = client.get(url(version, path), params=params, headers={"Authorization": settings.mobula_api_key})
            status = int(getattr(response, "status_code", 200) or 200)
            if status == 429 or status >= 500:
                pause = _retry_after(response)
                _bucket.cool_down(pause)
                increment("mobula_cooldowns")
                logger.warning("Mobula answered %s; pausing every caller for %.0fs", status, pause)
            response.raise_for_status()
            payload = response.json()
    return payload.get("data", payload) if isinstance(payload, dict) else payload


def reset_for_test() -> None:
    _bucket.reset()
