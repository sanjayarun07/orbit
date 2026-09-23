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
    failures: list[str] = []
    if goal.get("pipeline") == "legacy":
        if out is not None:
            failures.append("expected the legacy path, the pipeline answered")
        return {"id": episode["id"], "ok": not failures, "failures": failures, "ms": ms, "tools": router.calls}
    if out is None:
        return {"id": episode["id"], "ok": False, "failures": ["pipeline returned None"], "ms": ms, "tools": router.calls}
    answer = out.get("answer") or ""
    gate = out.get("gate") or {}
    if goal.get("tools_any") and not any(t in router.calls for t in goal["tools_any"]):
        failures.append(f"none of {goal['tools_any']} ran (ran {router.calls})")
    for t in goal.get("tools_none") or []:
        if t in router.calls:
            failures.append(f"{t} must not run")
    if goal.get("tools_first") and (not router.calls or router.calls[0] != goal["tools_first"]):
        failures.append(f"first tool was {router.calls[:1]}, expected {goal['tools_first']}")
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
    for text in goal.get("must_not_contain") or []:
        if text in answer:
            failures.append(f"forbidden text present: {text!r}")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--only", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    episodes = json.load(open(CASES))["episodes"]
    if args.only:
        episodes = [e for e in episodes if e["id"] == args.only]
    report, passed_k, wrong = [], 0, 0
    for episode in episodes:
        runs = [run_episode(episode) for _ in range(args.k)]
        ok_k = all(r["ok"] for r in runs)
        passed_k += ok_k
        wrong += (not ok_k)
        report.append({"id": episode["id"], "pass_k": ok_k, "pass_1": runs[0]["ok"], "failures": sorted({f for r in runs for f in r["failures"]}),
                       "ms_mean": sum(r["ms"] for r in runs) / len(runs), "tools": runs[0]["tools"]})
        print(f"{episode['id']:32} {'PASS' if ok_k else 'FAIL':4}  {report[-1]['ms_mean']:6.0f} ms  {runs[0]['tools']}" + ("" if ok_k else "  " + " | ".join(report[-1]["failures"])[:200]))
    print(f"\nepisodes {len(episodes)} · pass^{args.k} {passed_k}/{len(episodes)} · wrong-answer rate {wrong / max(1, len(episodes)):.0%}")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        json.dump({"k": args.k, "episodes": report, "at": datetime.now(timezone.utc).isoformat()}, open(args.out, "w"), indent=1)
    return 0 if passed_k == len(episodes) else 1


if __name__ == "__main__":
    sys.exit(main())
