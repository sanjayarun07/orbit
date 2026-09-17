"""Capture evidence bundles for the synthesis tier.

Runs prompts through the real agent in-process with the synthesis recorder on,
and saves what each synthesis program was GIVEN -- the request and the
evidence the tools gathered -- so replay.py can have different models write
the answer from identical evidence. Model quality, isolated from retrieval.

Costs real tool and model calls (the primary model still writes the answer
during capture; that answer is kept as the baseline for the same bundle).

  .venv/bin/python scripts/synthesis_eval/capture.py --file prompts.txt
  .venv/bin/python scripts/synthesis_eval/capture.py --defaults        # a built-in set
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

OUT = Path(__file__).with_name("bundles.json")

# Prompts that reach the three synthesis programs: equity brief, knowledge
# answer, token deep dive. Kept to a size that captures in minutes.
DEFAULT_PROMPTS = [
    "deep dive on BONK", "deep dive on JUP", "analyze WIF for me", "thoughts on PYTH",
    "what is Morpho Blue and how does it work", "How does Aave V3's E-mode change the liquidation threshold?",
    "what backs USDe and how does it keep its peg", "has Aave ever been hacked?", "who are Kamino's competitors on Solana",
    "what happened in the Ronin bridge hack", "who are the investors backing EigenLayer",
    "why is NVDA stock up today", "TSLA earnings reaction", "equity research on COIN", "is MSTR a good buy",
]


def _evidence_text(program: str, kwargs: dict) -> str:
    if program == "token_deepdive_agent":
        return str(kwargs.get("evidence") or "")
    if program == "knowledge_synthesizer":
        return str(kwargs.get("passages") or "")
    if program == "equity_research_synthesizer":
        return f"{kwargs.get('financial_evidence') or ''}\n\n{kwargs.get('news_evidence') or ''}"
    return "\n".join(str(v) for v in kwargs.values() if isinstance(v, str))


async def capture(prompts: list[str]) -> list[dict]:
    from app.graph import run_agent
    from app.knowledge import tool as kb_tool
    from app.nodes import runtime

    # The knowledge-base anchor reads a resolver snapshot the app warms at
    # startup; a bare process has none, and every protocol question would
    # then be answered elsewhere and never reach the knowledge synthesizer.
    # And the tool's DB work must run on THIS loop, as it does on the app's
    # main loop; unbound, a worker thread opens a private loop and the
    # knowledge tool fails quietly, so the router picks web research instead.
    kb_tool.set_loop(asyncio.get_running_loop())
    try:
        await kb_tool.resolver(force=True)
        res = kb_tool.snapshot()
        print(f"knowledge snapshot: {len(res) if res is not None else 0} entities")
    except Exception as e:
        print(f"knowledge snapshot: EMPTY ({str(e)[:60]}); knowledge prompts will not reach the synthesizer")
    bundles: list[dict] = []
    for prompt in prompts:
        recorded: list[tuple[str, dict]] = []
        runtime.synthesis_recorder = lambda name, kwargs, _r=recorded: _r.append((name, dict(kwargs)))
        t0 = time.perf_counter()
        try:
            run = await asyncio.wait_for(run_agent(prompt, "", "", {}, None), timeout=240)
            baseline = run.answer
        except Exception as e:
            print(f"  ! {prompt!r}: {type(e).__name__}: {str(e)[:80]}")
            baseline = None
        finally:
            runtime.synthesis_recorder = None
        elapsed = (time.perf_counter() - t0) * 1000
        if not recorded:
            print(f"  - {prompt!r}: no synthesis program ran (answered elsewhere) [{elapsed:.0f} ms]")
            continue
        name, kwargs = recorded[-1]
        bundles.append({"id": f"b{len(bundles) + 1:03d}", "prompt": prompt, "program": name, "kwargs": kwargs,
                        "evidence": _evidence_text(name, kwargs), "baseline_answer": baseline, "captured_ms": elapsed})
        print(f"  + {prompt!r} -> {name} ({len(_evidence_text(name, kwargs))} chars of evidence) [{elapsed:.0f} ms]")
    return bundles


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="prompts, one per line")
    ap.add_argument("--defaults", action="store_true")
    args = ap.parse_args()
    prompts = []
    if args.file:
        prompts += [l.strip() for l in Path(args.file).read_text().splitlines() if l.strip() and not l.startswith("#")]
    if args.defaults or not prompts:
        prompts += DEFAULT_PROMPTS
    bundles = asyncio.run(capture(prompts))
    OUT.write_text(json.dumps(bundles, indent=2))
    by = {}
    for b in bundles:
        by[b["program"]] = by.get(b["program"], 0) + 1
    print(f"\n{len(bundles)} bundles -> {OUT}  {by}")


if __name__ == "__main__":
    main()
