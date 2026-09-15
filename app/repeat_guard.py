"""Per-ReAct-invocation repeat-call guard.

dspy.ReAct's own loop (verified in .venv/.../dspy/predict/react.py) catches
any tool exception and feeds it back to the model as a corrective
observation -- but has no notion of the model repeating the *exact same*
call it already tried. Left alone, a stuck model burns iterations up to
max_iters retrying an identical failing (tool, args) pair. This wraps each
tool so a second identical call raises immediately with a message DSPy's
own exception-handling already turns into an observation, nudging the model
onto a different tool or different arguments well before max_iters.

contextvars-based, mirrors app/call_budget.py's shape exactly: each
_call_lm invocation (app/nodes/runtime.py, the single choke point for every
DSPy program call) gets its own guard scope, correctly isolated per
concurrent request/tool-loop under asyncio -- this propagates through
asyncio.to_thread the same way app/call_budget.py's contextvar already
does across the identical boundary.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from functools import wraps
from typing import Callable

from app.metrics import increment


@dataclass
class _RepeatGuard:
    seen: set[tuple] = field(default_factory=set)


_current: contextvars.ContextVar["_RepeatGuard | None"] = contextvars.ContextVar("repeat_guard", default=None)


def start_guard() -> contextvars.Token:
    """Call once per DSPy program invocation (app.nodes.runtime._call_lm)."""
    return _current.set(_RepeatGuard())


def reset_guard(token: contextvars.Token) -> None:
    _current.reset(token)


def _signature(name: str, kwargs: dict) -> tuple:
    return (name, tuple(sorted((key, repr(value)) for key, value in kwargs.items())))


def guard_tools(tools: list[Callable]) -> list[Callable]:
    """Wrap each tool so an exact-repeat call (same name, same arguments)
    within the active guard scope raises instead of hitting the network
    again. A no-op when no guard is active -- e.g. a test that imports and
    calls the raw tool function directly is unaffected, since this returns
    new wrapped callables and never mutates the originals."""
    return [_guarded(tool) for tool in tools]


def _guarded(tool: Callable) -> Callable:
    @wraps(tool)
    def wrapper(*args, **kwargs):
        guard = _current.get()
        if guard is None:
            return tool(*args, **kwargs)
        sig = _signature(getattr(tool, "__name__", str(tool)), kwargs)
        if sig in guard.seen:
            increment(f"repeat_guard_blocked_{getattr(tool, '__name__', 'unknown')}")
            raise RuntimeError(
                f"You already called {tool.__name__} with these exact arguments and it did not "
                "resolve the request -- try a different tool, or different arguments, rather than "
                "repeating this call."
            )
        guard.seen.add(sig)
        return tool(*args, **kwargs)

    return wrapper
