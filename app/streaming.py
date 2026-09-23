"""A per-turn event channel, so the browser can render a turn as it happens.

Nothing about a turn changes when a client is not streaming: `emit` is a
no-op unless the turn was started with `attach`. When it was, the research
path reports what it is doing (`status`), hands over each card the moment
its tool returns (`card`), streams the synthesis text token by token
(`delta`), and the endpoint finishes with the complete response (`done`) --
the same AgentResponse the JSON route returns, so persistence, credits and
every gate are untouched. Errors travel as an `error` event carrying the
same status and detail the JSON route would have answered with.

The channel is a ContextVar, inherited by the tasks and threads a turn
spawns; a thread reports through the loop it was started from.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

_channel: ContextVar[tuple[asyncio.AbstractEventLoop, asyncio.Queue] | None] = ContextVar("turn_stream", default=None)


def attach(queue: asyncio.Queue) -> Token:
    return _channel.set((asyncio.get_running_loop(), queue))


def detach(token: Token) -> None:
    _channel.reset(token)


def active() -> bool:
    return _channel.get() is not None


_muted: ContextVar[frozenset] = ContextVar("turn_stream_muted", default=frozenset())


@contextmanager
def muted(*events: str):
    """Drop these events for the code inside: concurrent clauses of a
    compound ask each streamed their own sentences into one transcript
    ("SeptemberCould you please 22 provide", UI run 2026-09-23)."""
    token = _muted.set(_muted.get() | frozenset(events))
    try:
        yield
    finally:
        _muted.reset(token)


def emit(event: str, **data: Any) -> None:
    """Report to the streaming client, if any. Safe from any thread."""
    channel = _channel.get()
    if channel is None or event in _muted.get():
        return
    loop, queue = channel
    item = {"event": event, **data}
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        queue.put_nowait(item)
    else:
        loop.call_soon_threadsafe(queue.put_nowait, item)
