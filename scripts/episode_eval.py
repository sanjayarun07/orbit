"""Episode evaluation: whole answers judged on their end state, k times.

    python scripts/episode_eval.py [--k 5] [--only base-gainers-24h] [--out reports/episodes.json]

Each episode in evals/episodes/cases.json replays recorded tool outputs
(fixtures) through the contract pipeline and checks the goal: which tools
ran (and which must not), whether the scope was satisfied, whether the fact
gate passed, which facts exist, and what the answer must or must not say. A
case passes only when all k runs pass (pass^k, after tau-bench). Wrong-answer
rate is reported apart from tool success and latency. No network, no model:
the rules planner and a cards-only synthesis, so the score is the pipeline's,
not a provider's mood.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import evidence_pipeline, tool_catalog  # noqa: E402
from app.nodes import runtime  # noqa: E402

CASES = ROOT / "evals" / "episodes" / "cases.json"
FIXTURES = ROOT / "evals" / "episodes" / "fixtures"


def _fixture(value: str) -> str:
    path = FIXTURES / value
    text = path.read_text() if path.exists() else value
    return text.replace("{STAMP}", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))


class ReplayRouter:
    replay = True

    def __init__(self, fixtures: dict[str, str]):
        self.outputs = {k: _fixture(v) for k, v in fixtures.items()}
        self.calls: list[str] = []
        self._tools = [SimpleNamespace(name=n, priority=10.0, spec=tool_catalog.TOOL_SPECS.get(n), matches=lambda r: True, capabilities=("x",), risk="read_only")
                       for n in tool_catalog.CONTRACT_COVERAGE]

    def tools(self):
        return tuple(self._tools)

    def _enabled(self, tool):
        return True

    def invoke(self, name, request, chains=()):
        self.calls.append(name)
        out = self.outputs.get(name)
        return SimpleNamespace(output=out, tool=name, provider="replay") if out else None


async def _cards_only(request, cards, trajectory, advice=False):
    return f"**Taken together**\n\n(cards only in replay)\n\n---\n\n{cards}"


def run_episode(episode: dict) -> dict:
    router = ReplayRouter(episode.get("fixtures") or {})
    saved = (evidence_pipeline.get_provider_router, evidence_pipeline.composition.synthesize, runtime.planner_available, evidence_pipeline.settings.contract_pipeline_enabled)
    evidence_pipeline.get_provider_router = lambda: router
    evidence_pipeline.composition.synthesize = _cards_only
    runtime.planner_available = lambda: False
    evidence_pipeline.settings.contract_pipeline_enabled = True
    t0 = time.time()
    try:
        out = asyncio.run(evidence_pipeline.answer({}, episode["prompt"], ()))
    finally:
        # Replay must leave the process as it found it (a leaked fake router failed an unrelated test, 2026-09-23).
        evidence_pipeline.get_provider_router, evidence_pipeline.composition.synthesize, runtime.planner_available, evidence_pipeline.settings.contract_pipeline_enabled = saved
    ms = (time.time() - t0) * 1000
    goal = episode.get("goal") or {}
    if goal.get("pipeline") == "legacy" and out is None:
        return {"id": episode["id"], "ok": True, "failures": [], "ms": ms, "tools": router.calls}
    failures = _judge(episode, out, router.calls)
    fa = goal.get("fact_attr")
    if fa:
        from app import facts as facts_mod
        rows = []
        for name in router.calls:
            if router.outputs.get(name):
                rows += facts_mod.facts_from_card(name, router.outputs[name], episode.get("kind"))
        hit = [r for r in rows if r.kind == fa["kind"] and fa["subject_contains"] in r.subject]
        if not hit or hit[0].attrs.get(fa["attr"]) != fa["equals"]:
            failures.append(f"fact {fa} not found or wrong ({[r.attrs.get(fa['attr']) for r in hit]})")
    return {"id": episode["id"], "ok": not failures, "failures": failures, "ms": ms, "tools": router.calls}


def _judge(episode: dict, out: dict | None, calls: list[str], answer_override: str | None = None) -> list[str]:
    """The goal checks shared by replay and live runs."""
    goal = episode.get("goal") or {}
    failures: list[str] = []
    if goal.get("pipeline") == "legacy":
        if out is not None and out.get("pipeline") == "contract":
            failures.append("expected the legacy path, the pipeline answered")
        return failures
    if out is None:
        return ["no answer"]
    answer = answer_override if answer_override is not None else (out.get("answer") or "")
    gate = out.get("gate") or {}
    if goal.get("tools_any") and not any(t in calls for t in goal["tools_any"]):
        failures.append(f"none of {goal['tools_any']} ran (ran {calls})")
    for t in goal.get("tools_none") or []:
        if t in calls:
            failures.append(f"{t} must not run")
    if goal.get("tools_first") and (not calls or calls[0] != goal["tools_first"]):
        failures.append(f"first tool was {calls[:1]}, expected {goal['tools_first']}")
    if "scope_satisfied" in goal and gate.get("scope_satisfied") != goal["scope_satisfied"]:
        failures.append(f"scope_satisfied={gate.get('scope_satisfied')}, expected {goal['scope_satisfied']}")
    if "gate_ok" in goal and bool(gate.get("ok")) != goal["gate_ok"]:
        failures.append(f"gate ok={gate.get('ok')}, expected {goal['gate_ok']} ({gate.get('missing')})")
    for kind, n in (goal.get("facts_min") or {}).items():
        have = sum(1 for f in out.get("facts") or [] if f.startswith(kind + ":"))
        if have < n:
            failures.append(f"{have} {kind} facts, need {n}")
    for text in goal.get("must_contain") or []:
        if text not in answer:
            failures.append(f"missing text: {text!r}")
    for key in ("must_contain_any", "must_contain_any_2"):
        for text in goal.get(key) or []:
            if not any(t in answer for t in (text if isinstance(text, list) else [text])):
                failures.append(f"missing any of: {text!r}")
    for text in goal.get("must_not_contain") or []:
        if text in answer:
            failures.append(f"forbidden text present: {text!r}")
    return failures


def run_live(episode: dict) -> dict:
    """The prompt through the real research node with real providers and the
    model: latency and the metric counters (LLM calls, Perplexity calls,
    estimated Perplexity cost) beside the same goal judgement. A live goal
    must be provider-independent: tools that ran, scope, gate, structural text."""
    from app import call_budget, evidence_pipeline as pipeline_mod, graph, metrics
    from app.settings import settings

    # The same path a chat turn takes (routing included), with the semantic
    # answer cache off so every run is a real run, and a fresh per-turn budget.
    settings.provider_semantic_cache_threshold = 1.01
    captured: dict = {}
    original = pipeline_mod.answer

    async def capturing(state, request, chains, *, context=""):
        out = await original(state, request, chains, context=context)
        if out is not None:
            captured.update(out)
        return out
    pipeline_mod.answer = capturing
    from app.nodes import research as research_mod
    research_mod.evidence_pipeline.answer = capturing
    before = metrics.snapshot()
    t0 = time.time()
    try:
        starter = getattr(call_budget, "start_turn", None) or getattr(call_budget, "begin_turn", None) or getattr(call_budget, "reset", None)
        if starter:
            starter()
        run = asyncio.run(graph.run_agent(episode["prompt"], "", "", {}))
        out = {"answer": getattr(run, "answer", "") or "", "trajectory": getattr(run, "trajectory", None), **captured}
    except Exception as exc:  # noqa: BLE001
        out = {"answer": f"ERROR {exc!r}", "trajectory": None}
    finally:
        pipeline_mod.answer = original
        research_mod.evidence_pipeline.answer = original
    ms = (time.time() - t0) * 1000
    after = metrics.snapshot()
    delta = {k: after.get(k, 0) - before.get(k, 0) for k in set(after) | set(before) if after.get(k, 0) != before.get(k, 0)}
    trajectory = out.get("trajectory") or {}
    calls = [v for k, v in trajectory.items() if k.startswith("tool_name")]
    failures = _judge(episode, out, calls)
    return {"id": episode["id"], "ok": not failures, "failures": failures, "ms": ms, "tools": calls,
            "llm_calls": delta.get("llm_calls", 0), "perplexity_calls": sum(v for k, v in delta.items() if k.startswith("perplexity_") and k.endswith("_calls")),
            "perplexity_cost_usd": delta.get("perplexity_estimated_cost_microusd", 0) / 1e6, "pipeline": out.get("pipeline", "legacy"),
            "answer_head": (out.get("answer") or "")[:240]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--live", action="store_true", help="run each prompt through the real research node (providers, model); records latency and cost")
    ap.add_argument("--set", action="append", default=[], help="settings override for the run, e.g. --set discovery_first_research=true")
    ap.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"), help="print a per-episode comparison of two saved reports")
    ap.add_argument("--kinds", default=None, help="comma-separated contract kinds to include")
    args = ap.parse_args()
    if args.compare:
        a, b = (json.load(open(f)) for f in args.compare)
        ra, rb = {e["id"]: e for e in a["episodes"]}, {e["id"]: e for e in b["episodes"]}
        print(f"{'episode':32} {'A pass^k':8} {'B pass^k':8} {'A ms':>8} {'B ms':>8} {'A llm':>5} {'B llm':>5} {'A $':>7} {'B $':>7}")
        for eid in ra:
            x, y = ra[eid], rb.get(eid, {})
            print(f"{eid:32} {str(x.get('pass_k')):8} {str(y.get('pass_k')):8} {x.get('ms_mean', 0):8.0f} {y.get('ms_mean', 0):8.0f} {x.get('llm_calls', 0):5.1f} {y.get('llm_calls', 0):5.1f} {x.get('cost_usd', 0):7.3f} {y.get('cost_usd', 0):7.3f}")
        return 0
    if args.set:
        from app.settings import settings
        for item in args.set:
            key, _, value = item.partition("=")
            current = getattr(settings, key)
            setattr(settings, key, (value.lower() in ("1", "true", "yes")) if isinstance(current, bool) else type(current)(value) if current is not None else value)
    episodes = json.load(open(CASES))["episodes"]
    if args.only:
        episodes = [e for e in episodes if e["id"] == args.only]
    if args.kinds:
        wanted = set(args.kinds.split(","))
        episodes = [e for e in episodes if e.get("kind") in wanted]
    if not args.live:
        episodes = [e for e in episodes if not e.get("live_only")]
    report, passed_k, wrong = [], 0, 0
    for episode in episodes:
        runs = [(run_live if args.live else run_episode)(episode) for _ in range(args.k)]
        ok_k = all(r["ok"] for r in runs)
        passed_k += ok_k
        wrong += (not ok_k)
        report.append({"id": episode["id"], "pass_k": ok_k, "pass_1": runs[0]["ok"], "passes": sum(r["ok"] for r in runs), "failures": sorted({f for r in runs for f in r["failures"]}),
                       "ms_mean": sum(r["ms"] for r in runs) / len(runs), "tools": runs[0]["tools"],
                       "llm_calls": sum(r.get("llm_calls", 0) for r in runs) / len(runs), "perplexity_calls": sum(r.get("perplexity_calls", 0) for r in runs) / len(runs),
                       "cost_usd": sum(r.get("perplexity_cost_usd", 0) for r in runs) / len(runs), "pipeline": runs[0].get("pipeline"), "answer_head": runs[0].get("answer_head")})
        print(f"{episode['id']:32} {'PASS' if ok_k else 'FAIL':4} {report[-1]['passes']}/{args.k} {report[-1]['ms_mean']:7.0f} ms  {runs[0]['tools'][:3]}" + ("" if ok_k else "  " + " | ".join(report[-1]["failures"])[:220]))
    print(f"\nepisodes {len(episodes)} · pass^{args.k} {passed_k}/{len(episodes)} · wrong-answer rate {wrong / max(1, len(episodes)):.0%}"
          + (f" · mean {sum(r['ms_mean'] for r in report) / max(1, len(report)):.0f} ms · mean LLM calls {sum(r['llm_calls'] for r in report) / max(1, len(report)):.1f} · mean web cost ${sum(r['cost_usd'] for r in report) / max(1, len(report)):.3f}" if args.live else ""))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump({"k": args.k, "live": args.live, "settings": args.set, "episodes": report, "at": datetime.now(timezone.utc).isoformat()}, open(args.out, "w"), indent=1)
    return 0 if passed_k == len(episodes) else 1


if __name__ == "__main__":
    sys.exit(main())
