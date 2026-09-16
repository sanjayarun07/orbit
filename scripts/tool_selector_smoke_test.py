"""One-off check that tool_selector_model reaches a configured endpoint and
returns a usable pick, before piloting it for real. Does not touch
settings.llm_tool_selection_enabled or any other running process.

Usage (values read from the environment / .env -- never paste a key on the
command line, it lands in shell history):

    TOOL_SELECTOR_MODEL="hosted_vllm/Qwen3-VL-32B-Instruct-FP8" \\
    TOOL_SELECTOR_API_BASE="https://<your-host>/v1" \\
    TOOL_SELECTOR_API_KEY="<key>" \\
    .venv/bin/python scripts/tool_selector_smoke_test.py

Or set the three TOOL_SELECTOR_* keys in .env and just run the script with
no prefix. Prints the raw model reply for one real arbitration call, then
runs the same request through app.routing.tool_selector.select_tool end to
end (with the flag forced on for this process only) so you see the actual
reordering it would apply.
"""
from __future__ import annotations

import os
import sys
import time

# Must happen before importing anything that reads settings at import time.
os.environ.setdefault("TOOL_SELECTOR_MODEL", os.environ.get("TOOL_SELECTOR_MODEL", ""))

from app.settings import settings  # noqa: E402

if not settings.tool_selector_model:
    sys.exit("Set TOOL_SELECTOR_MODEL (and TOOL_SELECTOR_API_BASE / TOOL_SELECTOR_API_KEY if self-hosted) first.")

print(f"model      : {settings.tool_selector_model}")
print(f"api_base   : {settings.tool_selector_api_base or '(provider default)'}")
print(f"api_key    : {'set' if settings.tool_selector_api_key else '(none)'}")
print(f"timeout    : {settings.tool_selector_timeout_seconds}s")
print()

import dspy  # noqa: E402

from app.routing import tool_selector  # noqa: E402

kwargs = {}
if settings.tool_selector_api_base:
    kwargs["api_base"] = settings.tool_selector_api_base
if settings.tool_selector_api_key:
    kwargs["api_key"] = settings.tool_selector_api_key
lm = dspy.LM(settings.tool_selector_model, timeout=settings.tool_selector_timeout_seconds, num_retries=1, **kwargs)

print("--- raw call: does the endpoint answer a plain prompt? ---")
started = time.monotonic()
try:
    with dspy.context(lm=lm):
        reply = dspy.Predict("question -> answer")(question="Reply with exactly one word: ready")
    print(f"  {(time.monotonic() - started) * 1000:.0f}ms -> {reply.answer!r}")
except Exception as exc:
    sys.exit(f"  FAILED: {type(exc).__name__}: {exc}")

print("\n--- real arbitration call: the volume-vs-boosts case from this session ---")
from app.provider_registry import get_provider_router  # noqa: E402

settings.llm_tool_selection_enabled = True
router = get_provider_router()
request = "trending tokens by volume in 24hrs"
ranked = router.candidates(request, "token_discovery", (), allow_semantic_fallback=True)
print(f"  deterministic order: {[t.name for t in ranked][:5]}")
started = time.monotonic()
picked = tool_selector.select_tool(request, ranked, ())
print(f"  {(time.monotonic() - started) * 1000:.0f}ms -> after arbitration: {[t.name for t in picked][:5]}")
if picked and picked[0].name == "coingecko_top_volume":
    print("  OK: picked the volume-ranking tool, not the paid-boosts list.")
else:
    print(f"  UNEXPECTED: top pick was {picked[0].name if picked else None!r} -- check the raw reply above.")

print("\nIf both calls above look right, run the full eval against it:")
print("  .venv/bin/python scripts/routing_eval/harness.py --mode router --semantic --llm-select")
