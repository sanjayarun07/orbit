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
RETRY_ONCE_SECONDS = 1.0     # one retry after a header-less 429 before the cooldown


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


class _Shared:
    """The deployment-wide minute counter in Redis: INCR on a key per minute,
    expiring after two. A user call is refused past the full allowance, a
    background call past half of it. Redis unreachable -> allowed (the local
    bucket still shapes this process), and not retried for a minute."""

    def __init__(self) -> None:
        self._client = None
        self._retry_at = 0.0
        self._lock = threading.Lock()

    def _redis(self):
        if not settings.mobula_shared_limit or not settings.redis_url:
            return None
        with self._lock:
            if self._client is None and time.monotonic() >= self._retry_at:
                try:
                    import redis as _redis  # the synchronous client: this path runs in worker threads

                    self._client = _redis.Redis.from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True)
                except Exception:
                    self._retry_at = time.monotonic() + 60.0
            return self._client

    # Count only what is admitted: a refused call must not spend the minute
    # for everyone else (review, 2026-09-22). One script, one round trip.
    _TAKE = ("local n = tonumber(redis.call('GET', KEYS[1]) or '0') "
             "if n >= tonumber(ARGV[1]) then return -1 end "
             "n = redis.call('INCR', KEYS[1]) "
             "if n == 1 then redis.call('EXPIRE', KEYS[1], 120) end "
             "return n")

    def take(self, *, background: bool) -> bool:
        client = self._redis()
        if client is None:
            return True
        key = f"mobula:rpm:{int(time.time() // 60)}"
        allowance = float(settings.mobula_requests_per_minute)
        limit = int(allowance / 2.0) if background else int(allowance)
        try:
            result = int(client.eval(self._TAKE, 1, key, limit))
        except Exception:
            with self._lock:
                self._client = None
                self._retry_at = time.monotonic() + 60.0
            return True
        return result != -1

    def reset(self) -> None:
        with self._lock:
            self._client = None
            self._retry_at = 0.0


_shared = _Shared()


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
    if not _shared.take(background=background):
        increment("mobula_shared_limit_exceeded")
        raise MobulaBudgetExceeded(f"Mobula budget of {settings.mobula_requests_per_minute}/min is spent across the deployment; try again in a moment")
    increment("mobula_requests")
    with _inflight:
        with httpx.Client(timeout=timeout or settings.provider_request_timeout_seconds) as client:
            response = client.get(url(version, path), params=params, headers={"Authorization": settings.mobula_api_key})
            status = int(getattr(response, "status_code", 200) or 200)
            if status == 429 and not response.headers.get("Retry-After") and not background \
                    and _bucket.take(0.0) and _shared.take(background=False):
                # Mobula's "Max usage reached" 429 comes without Retry-After
                # and clears within a second (live probe, 2026-09-23: 429,
                # then 200 0.4 s later, at 2 requests a second on a 30 rps
                # plan). One short retry for a user's call before the whole
                # deployment is paused for the cooldown. The retry is a
                # request like any other: it takes its own budget token (no
                # token, no retry) and counts in `mobula_requests`.
                time.sleep(RETRY_ONCE_SECONDS)
                increment("mobula_requests")
                increment("mobula_retries")
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
    _shared.reset()
