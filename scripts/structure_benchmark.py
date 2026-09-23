"""The structure benchmark: does the product flag the tokens that went bad, on
the evidence it had at the time, and stay quiet on the ones that did not?

TradingAgents' evaluation runner, adapted (2026-09-22): a grid of cases,
each scored on what was knowable at its decision time, three arms on the
same saved evidence, and the failures recorded rather than the score alone.

    python scripts/structure_benchmark.py label [--min-snapshots 5] [--horizon-hours 24]
    python scripts/structure_benchmark.py run --arm rules
    python scripts/structure_benchmark.py run --arm analyst --limit 20        # model calls: paid
    python scripts/structure_benchmark.py run --arm critic  --limit 20        # two model calls per case
    python scripts/structure_benchmark.py report

`label` reads the holder ledger: for every subject with enough snapshots,
the FIRST snapshot is the evidence at decision time and the later ones
decide the outcome labels -- concentration up ten points, liquidity down
to under 30%, price down to under 30% -- each present only when the
fields it needs were recorded at both ends, otherwise "unknown". Cases
land in evals/structure/cases.json with the evidence that was knowable,
never anything later. `run` scores an arm over the cases and writes
evals/structure/runs/<arm>.json; `report` prints recall on bad outcomes,
false-alert rate on clean ones, evidence coverage, latency and model
calls per arm, and the misses by name. As-of discipline: an arm sees
only a case's decision-time snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EVALS = ROOT / "evals" / "structure"
CASES = EVALS / "cases.json"
RUNS = EVALS / "runs"

OUTCOMES = ("concentration_up", "liquidity_collapse", "price_collapse")
BAD = frozenset(OUTCOMES)


def _outcome(first: dict, last: dict) -> dict:
    """Labels from the first and last snapshot; None where the fields are missing."""
    def ratio(key):
        a, b = first.get(key), last.get(key)
        if a is None or b is None or float(a) <= 0:
            return None
        return float(b) / float(a)
    out = {}
    if first.get("top10_pct") is not None and last.get("top10_pct") is not None:
        out["concentration_up"] = float(last["top10_pct"]) - float(first["top10_pct"]) >= 10.0
    else:
        out["concentration_up"] = None
    liq, price = ratio("liquidity_usd"), ratio("price_usd")
    out["liquidity_collapse"] = (liq < 0.3) if liq is not None else None
    out["price_collapse"] = (price < 0.3) if price is not None else None
    return out


def build_cases(rows_by_subject: dict[str, list[dict]], min_snapshots: int, horizon_hours: float) -> list[dict]:
    """Pure: from ledger rows (oldest first per subject) to cases."""
    cases = []
    for subject_key, rows in sorted(rows_by_subject.items()):
        rows = sorted(rows, key=lambda r: r["taken_at"])
        if len(rows) < min_snapshots:
            continue
        first = rows[0]
        t0 = datetime.fromisoformat(first["taken_at"]) if isinstance(first["taken_at"], str) else first["taken_at"]
        window = [r for r in rows if (datetime.fromisoformat(r["taken_at"]) if isinstance(r["taken_at"], str) else r["taken_at"]) <= t0 + timedelta(hours=horizon_hours)]
        if len(window) < 2:
            continue
        last = window[-1]
        outcome = _outcome(first, last)
        evidence = {k: first.get(k) for k in ("top10_pct", "top50_pct", "holders_count", "dev_pct", "sniper_pct", "bundler_pct", "insider_pct",
                                             "lp_burned_pct", "lp_locked_pct", "lp_unlocked_pct", "price_usd", "liquidity_usd", "market_cap_usd")}
        evidence["flags"] = first.get("flags") or []
        cases.append({
            "id": subject_key, "decision_at": str(first["taken_at"]), "outcome_at": str(last["taken_at"]), "snapshots_in_window": len(window),
            "evidence": evidence, "outcome": outcome,
            "bad": any(v is True for v in outcome.values()),
            "clean": all(v is False for v in outcome.values() if v is not None) and any(v is not None for v in outcome.values()),
        })
    return cases


async def label(min_snapshots: int, horizon_hours: float) -> int:
    from app.db import get_pg_pool

    pool = await get_pg_pool()
    rows = await pool.fetch("SELECT subject_key, taken_at, top10_pct, top50_pct, holders_count, dev_pct, sniper_pct, bundler_pct, insider_pct, "
                            "lp_burned_pct, lp_locked_pct, lp_unlocked_pct, price_usd, liquidity_usd, market_cap_usd, flags::text AS flags "
                            "FROM holder_snapshots ORDER BY subject_key, taken_at")
    by_subject: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        d["taken_at"] = d["taken_at"].isoformat()
        d["flags"] = json.loads(d["flags"]) if isinstance(d["flags"], str) else d["flags"]
        by_subject.setdefault(d["subject_key"], []).append(d)
    cases = build_cases(by_subject, min_snapshots, horizon_hours)
    EVALS.mkdir(parents=True, exist_ok=True)
    CASES.write_text(json.dumps({"labelled_at": datetime.now().isoformat(), "min_snapshots": min_snapshots, "horizon_hours": horizon_hours, "cases": cases}, indent=1, default=str))
    bad = sum(c["bad"] for c in cases)
    clean = sum(c["clean"] for c in cases)
    print(f"{len(cases)} cases from {len(by_subject)} subjects: {bad} bad, {clean} clean, {len(cases) - bad - clean} unknown -> {CASES.relative_to(ROOT)}")
    return 0


# ---- arms --------------------------------------------------------------------

def arm_rules(case: dict) -> dict:
    from app.structure_rules import assess

    out = assess(case["evidence"])
    return {"flag": out["flag"], "reasons": out["reasons"], "unknown": out["unknown"], "model_calls": 0}


async def arm_analyst(case: dict, critic: bool = False) -> dict:
    """One grounded analyst over the decision-time evidence, optionally
    checked by an independent critic who may overturn an unsupported flag."""
    import dspy
    from app.nodes import runtime

    class StructureCall(dspy.Signature):
        """Judge a meme token's structural risk from the recorded figures ONLY.
        Flag it when the figures show a launch a holder could not safely
        exit: pullable liquidity, concentrated or coordinated supply, a
        deployer or insiders holding a large share. A missing figure is
        unknown, never safe. Answer flag as yes or no and give the reasons,
        each naming the figure it rests on."""
        evidence_json: str = dspy.InputField()
        flag: str = dspy.OutputField(desc="yes | no")
        reasons: str = dspy.OutputField(desc="one line per reason, each naming a figure")

    class StructureCritic(dspy.Signature):
        """Check an analyst's structural-risk verdict against the recorded
        figures ONLY. Uphold it when every reason names a figure that is
        present and supports it; overturn it when a reason rests on a figure
        that is missing or does not say what the reason claims. Answer
        verdict as uphold or overturn, and say why."""
        evidence_json: str = dspy.InputField()
        analyst_flag: str = dspy.InputField()
        analyst_reasons: str = dspy.InputField()
        verdict: str = dspy.OutputField(desc="uphold | overturn")
        why: str = dspy.OutputField()

    evidence = json.dumps(case["evidence"], default=str)
    result = await runtime._call_lm(dspy.Predict(StructureCall), evidence_json=evidence)
    flag = str(getattr(result, "flag", "")).strip().lower().startswith("y")
    reasons = str(getattr(result, "reasons", "") or "")
    calls = 1
    overturned = None
    if critic:
        check = await runtime._call_lm(dspy.Predict(StructureCritic), evidence_json=evidence, analyst_flag="yes" if flag else "no", analyst_reasons=reasons)
        calls += 1
        if str(getattr(check, "verdict", "")).strip().lower().startswith("overturn"):
            overturned = str(getattr(check, "why", "") or "")
            flag = not flag
    return {"flag": flag, "reasons": [r.strip() for r in reasons.splitlines() if r.strip()], "critic_overturned": overturned, "model_calls": calls}


async def run(arm: str, limit: int | None) -> int:
    doc = json.loads(CASES.read_text())
    cases = doc["cases"] if limit is None else sample(doc["cases"], limit)
    results = []
    for case in cases:
        t0 = time.monotonic()
        try:
            out = arm_rules(case) if arm == "rules" else await arm_analyst(case, critic=(arm == "critic"))
        except Exception as exc:  # noqa: BLE001 - a failed case is a recorded failure
            out = {"flag": None, "error": f"{type(exc).__name__}: {exc}"[:200], "model_calls": 0}
        results.append({"id": case["id"], "bad": case["bad"], "clean": case["clean"], "outcome": case["outcome"], **out, "latency_ms": int((time.monotonic() - t0) * 1000)})
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / f"{arm}.json").write_text(json.dumps({"arm": arm, "ran_at": datetime.now().isoformat(), "results": results}, indent=1, default=str))
    print(score(results, arm))
    return 0


def sample(cases: list[dict], limit: int) -> list[dict]:
    """A stratified, deterministic sample: half bad, half clean, in the
    ledger's order, so a limited run compares the arms on both kinds."""
    bad = [c for c in cases if c["bad"]]
    clean = [c for c in cases if c["clean"]]
    half = max(1, limit // 2)
    picked = bad[:half] + clean[:limit - min(half, len(bad))]
    return picked[:limit]


def score(results: list[dict], arm: str) -> str:
    judged = [r for r in results if r.get("flag") is not None]
    bad = [r for r in judged if r["bad"]]
    clean = [r for r in judged if r["clean"]]
    recall = sum(1 for r in bad if r["flag"]) / len(bad) if bad else float("nan")
    false_alerts = sum(1 for r in clean if r["flag"]) / len(clean) if clean else float("nan")
    unknown = sum(len(r.get("unknown") or []) for r in judged) / len(judged) if judged else 0
    latency = sorted(r["latency_ms"] for r in results)
    p50 = latency[len(latency) // 2] if latency else 0
    calls = sum(r.get("model_calls", 0) for r in results)
    errors = [r["id"] for r in results if r.get("error")]
    misses = [r["id"] for r in bad if not r["flag"]]
    lines = [f"arm {arm}: {len(results)} cases, {len(bad)} bad, {len(clean)} clean",
             f"  recall on bad outcomes   {recall:.0%}" if bad else "  recall on bad outcomes   n/a",
             f"  false alerts on clean    {false_alerts:.0%}" if clean else "  false alerts on clean    n/a",
             f"  unknown fields per case  {unknown:.1f}", f"  latency p50              {p50} ms", f"  model calls              {calls}"]
    if misses:
        lines.append("  missed: " + ", ".join(m[:40] for m in misses[:8]) + (f" (+{len(misses) - 8})" if len(misses) > 8 else ""))
    if errors:
        lines.append(f"  errors: {len(errors)}")
    return "\n".join(lines)


def report() -> int:
    if not RUNS.exists():
        print("no runs yet"); return 1
    for path in sorted(RUNS.glob("*.json")):
        doc = json.loads(path.read_text())
        print(score(doc["results"], doc["arm"]))
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    l = sub.add_parser("label"); l.add_argument("--min-snapshots", type=int, default=5); l.add_argument("--horizon-hours", type=float, default=24.0)
    r = sub.add_parser("run"); r.add_argument("--arm", choices=("rules", "analyst", "critic"), required=True); r.add_argument("--limit", type=int)
    sub.add_parser("report")
    args = ap.parse_args()
    if args.mode == "label":
        return asyncio.run(label(args.min_snapshots, args.horizon_hours))
    if args.mode == "run":
        return asyncio.run(run(args.arm, args.limit))
    return report()


if __name__ == "__main__":
    sys.exit(main())
