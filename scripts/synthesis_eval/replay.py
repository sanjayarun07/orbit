"""Replay captured evidence bundles through one or more models and score them.

Every model writes from the SAME evidence (bundles.json from capture.py), so
what differs is the writing: grounding in the evidence, provenance, freshness
(the app's own answer validator, step 7 of the pipeline), plus latency and
tokens. The primary model's answer captured at the time is scored too, as
"baseline".

  .venv/bin/python scripts/synthesis_eval/replay.py --model openai/gpt-4.1-mini --model hosted_vllm/DMind-3-mini
  (a hosted_vllm model needs HOSTED_VLLM_API_BASE=http://host:8000/v1 in the environment)
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

BUNDLES = Path(__file__).with_name("bundles.json")
RESULTS = Path(__file__).with_name("results.json")


def _score(prompt: str, answer: str, evidence: str, program: str) -> dict:
    """The app's validator, given the evidence as the one observation the answer
    could draw on. Identical for every model, so the numbers compare."""
    from app.answer_validator import validate_answer
    trajectory = {"tool_name_0": program, "observation_0": evidence}
    validation = validate_answer(prompt, answer, trajectory, "research")
    if validation is None:
        return {"status": "none"}
    dumped = validation.model_dump() if hasattr(validation, "model_dump") else dict(vars(validation))
    return dumped


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))] if values else 0.0


def _lm_for(model: str, api_bases: dict, timeout: float):
    """`--api-base MODEL=URL` points an OpenAI-compatible server (vLLM, SGLang,
    a hosted proxy) at a model id without touching the environment the other
    models use; the id is passed through as `openai/<id>` so LiteLLM speaks
    plain chat-completions to it."""
    import dspy
    base = api_bases.get(model)
    if base:
        return dspy.LM(f"openai/{model}", api_base=base, api_key=api_bases.get("__key__", "none"), timeout=timeout, num_retries=0, max_tokens=4000)
    return dspy.LM(model, timeout=timeout, num_retries=0)


def replay(models: list[str], bundles: list[dict], include_baseline: bool, api_bases: dict | None = None) -> dict:
    api_bases = api_bases or {}
    import dspy
    from app.nodes import runtime
    from app.settings import settings

    # The replay kwargs are byte-identical to capture's, so DSPy's cache would
    # answer every call in a millisecond with no usage: nothing measured.
    dspy.configure_cache(enable_disk_cache=False, enable_memory_cache=False)
    report: dict = {}
    if include_baseline:
        rows = []
        for b in bundles:
            if b.get("baseline_answer"):
                rows.append({"id": b["id"], "answer": b["baseline_answer"], "ms": None, "tokens": None,
                             "validation": _score(b["prompt"], b["baseline_answer"], b["evidence"], b["program"])})
        report[f"baseline ({settings.model})"] = rows
    for model in models:
        lm = _lm_for(model, api_bases, settings.llm_request_timeout_seconds)
        rows = []
        for b in bundles:
            program = getattr(runtime, b["program"])
            t0 = time.perf_counter()
            try:
                result = runtime._run_program(program, lm, b["kwargs"])
                answer = (getattr(result, "answer", "") or "").strip()
                error = None
            except Exception as e:
                answer, error = "", f"{type(e).__name__}: {str(e)[:120]}"
            ms = (time.perf_counter() - t0) * 1000
            usage = (lm.history[-1].get("usage") or {}) if lm.history else {}
            rows.append({"id": b["id"], "answer": answer, "ms": ms, "error": error,
                         "tokens": {"in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens")},
                         "validation": _score(b["prompt"], answer, b["evidence"], b["program"]) if answer else {"status": "error"}})
            print(f"  {model:36} {b['id']} {b['program']:30} {ms:6.0f} ms  {rows[-1]['validation'].get('status')}")
        report[model] = rows
    return report


def summarise(report: dict) -> None:
    print("\n=== SYNTHESIS EVAL ===")
    print(f"{'model':40} {'n':>3} {'warn':>6} {'error':>6} {'p50 ms':>7} {'p95 ms':>7} {'out tok':>8} {'chars':>6}")
    for model, rows in report.items():
        n = len(rows)
        statuses = [r["validation"].get("status") for r in rows]
        warn = sum(1 for s in statuses if s == "warn") / max(1, n)
        err = sum(1 for r in rows if r.get("error")) / max(1, n)
        ms = [r["ms"] for r in rows if r["ms"] is not None]
        out = [r["tokens"]["out"] for r in rows if r.get("tokens") and r["tokens"].get("out")]
        chars = statistics.median(len(r["answer"]) for r in rows) if rows else 0
        failing = {}
        for r in rows:
            for check in r["validation"].get("checks", []) or []:
                if check.get("status") == "warn":
                    failing[check["name"]] = failing.get(check["name"], 0) + 1
        breakdown = ", ".join(f"{k} {v}" for k, v in sorted(failing.items())) or "-"
        print(f"{model:40} {n:3d} {warn:6.0%} {err:6.0%} {_pct(ms, .5):7.0f} {_pct(ms, .95):7.0f} {statistics.median(out) if out else 0:8.0f} {chars:6.0f}   warns by check: {breakdown}")
    print("\nwarn = the app's answer validator flagged the answer (grounding: figures not in the evidence; provenance: data with no source; "
          "freshness: no or stale timestamp; consistency: sources disagree). Lower is better, at equal evidence.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=[], help="LiteLLM model id; repeatable")
    ap.add_argument("--no-baseline", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--api-base", action="append", default=[], metavar="MODEL=URL",
                    help="serve MODEL from an OpenAI-compatible URL, e.g. DMindAI/DMind-3-mini=https://host/v1")
    args = ap.parse_args()
    bundles = json.loads(BUNDLES.read_text())
    if args.limit:
        bundles = bundles[: args.limit]
    api_bases = dict(item.split("=", 1) for item in args.api_base)
    report = replay(args.model, bundles, include_baseline=not args.no_baseline, api_bases=api_bases)
    RESULTS.write_text(json.dumps(report, indent=2))
    summarise(report)
    print(f"Saved {RESULTS}")


if __name__ == "__main__":
    main()
