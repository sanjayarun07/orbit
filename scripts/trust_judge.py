"""Judge a trust re-run (scripts/trust_rerun.py) turn by turn: the model reads
the prompt, the reviewer's expectation and the answer, and returns
pass / partial / fail with the one thing that decided it, plus any claim in
the answer that the answer's own cards do not support. A first pass for the
human read, not a verdict: the report lists every fail and partial with the
judge's reason so the answers can be read in order.

    .venv/bin/python scripts/trust_judge.py reports/trust-rerun-<sha>
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import dspy  # noqa: E402


class TurnJudge(dspy.Signature):
    """Judge one chat answer against what a careful reviewer expected.

    pass: the answer does what the prompt asked and the expectation describes, with no wrong or unsupported claim.
    partial: useful and not wrong, but a required part is missing, hedged away, or unverified.
    fail: a wrong fact, a wrong subject, a claim the answer's own evidence does not support, a promise not kept,
    a clarifying question where the prompt was clear, or an error.
    Judge only what is on the page: an answer that says exactly what it could not verify is honest, not a fail.
    A research-mode deployment cannot quote or execute swaps; a plain refusal that names that limit is partial,
    a refusal that misreads the ask is fail.
    Three readings that were wrong in every run so far: the dates in the answers are today's (2026), not the
    future; an answer that keeps a wallet's funding date distinct from its first trade in the token and treats
    missing counts as unknown meets an expectation about "first trade"; a stop confirmation that names the token
    is complete. A deterministic card (a scenario, a health check) is judged on its figures and stated
    assumptions, not on narrative or recommendations the prompt did not ask for."""
    prompt: str = dspy.InputField()
    expected: str = dspy.InputField(desc="the reviewer's expectation for this turn")
    answer: str = dspy.InputField(desc="the answer as rendered, cards included")
    verdict: str = dspy.OutputField(desc="pass | partial | fail")
    reason: str = dspy.OutputField(desc="one sentence: the single thing that decided the verdict")
    unsupported: str = dspy.OutputField(desc="claims in the answer's prose that its cards do not support, semicolon-separated, or 'none'")


async def judge(turns: list[dict]) -> list[dict]:
    from app.nodes import runtime
    program = dspy.Predict(TurnJudge)
    out = []
    for t in turns:
        if t.get("status") == "skipped":
            out.append({**t, "verdict": "skipped", "reason": t.get("skip")})
            continue
        if t.get("status") == "error":
            out.append({**t, "verdict": "fail", "reason": f"error: {t.get('error')}", "unsupported": "none"})
            continue
        try:
            pred = await runtime._call_lm(program, prompt=t["prompt"], expected=t.get("expected") or "-", answer=(t.get("answer") or "")[:9000])
            verdict = (pred.verdict or "").strip().lower().split()[0]
            out.append({**t, "verdict": verdict if verdict in ("pass", "partial", "fail") else "partial", "reason": pred.reason, "unsupported": pred.unsupported})
        except Exception as exc:  # noqa: BLE001
            out.append({**t, "verdict": "unjudged", "reason": repr(exc)[:200]})
        print(f"{t['id']:24} {out[-1]['verdict']:8} {out[-1].get('reason', '')[:110]}", flush=True)
    return out


def main() -> None:
    out_dir = Path(sys.argv[1])
    turns = [json.loads(l) for l in (out_dir / "turns.jsonl").read_text().splitlines() if l.strip()]
    judged = asyncio.run(judge(turns))
    (out_dir / "judged.jsonl").write_text("\n".join(json.dumps(j, default=str) for j in judged) + "\n")
    ran = [j for j in judged if j["verdict"] != "skipped"]
    now = Counter(j["verdict"] for j in ran)
    prior = Counter((j.get("prior") or "-") for j in ran)
    lines = [f"# Judge pass — {out_dir.name}", "", f"{len(ran)} turns judged, {len(judged) - len(ran)} skipped.", "",
             "| | pass | partial | fail | other |", "|---|---:|---:|---:|---:|",
             f"| prior review | {prior.get('pass', 0)} | {prior.get('partial', 0)} | {prior.get('fail', 0)} | {sum(v for k, v in prior.items() if k not in ('pass', 'partial', 'fail'))} |",
             f"| this build (judge) | {now.get('pass', 0)} | {now.get('partial', 0)} | {now.get('fail', 0)} | {sum(v for k, v in now.items() if k not in ('pass', 'partial', 'fail'))} |", "",
             "## Every fail and partial", "", "| id | prior | now | reason | unsupported |", "|---|---|---|---|---|"]
    for j in ran:
        if j["verdict"] in ("fail", "partial", "unjudged"):
            lines.append(f"| {j['id']} | {j.get('prior') or '-'} | {j['verdict']} | {str(j.get('reason', '')).replace('|', '/')[:220]} | {str(j.get('unsupported', '')).replace('|', '/')[:160]} |")
    lines += ["", "## Moves", "", "| id | prior | now |", "|---|---|---|"]
    for j in ran:
        if (j.get("prior") or "-") != j["verdict"]:
            lines.append(f"| {j['id']} | {j.get('prior') or '-'} | {j['verdict']} |")
    (out_dir / "judge.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:9]))


if __name__ == "__main__":
    main()
