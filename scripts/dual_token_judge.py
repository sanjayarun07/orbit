"""Score the four-turn dual-token sequence (evals/journeys/dual-token-2026-09-24.json).

    .venv/bin/python scripts/dual_token_judge.py reports/dual-token-sol-5/part1 [more dirs...]

Four points per run, read from the synthesis above the cards:
  1. the VVV -> sVVV -> DIEM mint/burn flow (DIEM named, sVVV or lock/burn named)
  2. the dual-token links carried into turn two (fee/burn/collateral/redemption/governance named, not a refusal)
  3. Akash's AKT -> ACT burn-mint equilibrium (ACT named with burn or mint), not what Akash is
  4. stays on the own-token pattern (lexical) and, with --model, names other projects that meet it (model judge)
Plus the seconds per turn and whether any turn was a clarification or a "nothing usable".
These are lexical checks for a repeated measurement; read the transcripts for the verdict.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
from pathlib import Path

import dspy


def synthesis(answer: str) -> str:
    cut = answer.find("\n---\n")
    return (answer[:cut] if cut > 0 else answer).lower()


class OtherAnalogues(dspy.Signature):
    """Judge the final answer of a research sequence whose question was "which
    other projects follow lock collateral -> mint a tradable second token",
    in a conversation about Venice (VVV locked to mint tradable DIEM) and
    Akash (AKT burned to mint non-transferable ACT). List every project the
    answer names, OTHER than Venice and Akash, as actually meeting that
    mechanism -- the project's own token locked or burned as collateral to
    mint a separately tradable second token -- with a [n] source marker next
    to the claim. A project named only as a contrast, a non-match, a
    receipt-token or bridge example, or a stablecoin backed by external
    collateral does not count. Output the qualifying names comma-separated,
    or exactly "none" when the answer names none (including when it says
    Venice is the only confirmed match)."""

    answer: str = dspy.InputField()
    qualifying: str = dspy.OutputField(desc='comma-separated project names, or "none"')


_other = dspy.Predict(OtherAnalogues)


def other_analogues(answer: str) -> str:
    """The research tier's reading of criterion 4 (review of c03378b1: the
    lexical check passed answers that named no other project)."""
    from app.nodes import runtime
    result = asyncio.run(runtime._call_research_lm(_other, answer=answer[:6000]))
    return (getattr(result, "qualifying", "") or "none").strip()


def score(turns: list[dict], model_judge: bool = False) -> dict:
    by = {t["turn"]: (t.get("answer") or "") for t in turns}
    s1, s2, s3, s4 = (synthesis(by.get(i, "")) for i in (1, 2, 3, 4))
    refusal = re.compile(r"nothing usable|no (?:specific |available )?information|which token|name it \(")
    return {
        "1_vvv_diem_flow": ("diem" in s1) and any(w in s1 for w in ("svvv", "lock", "burn")),
        "2_links_carried": (not refusal.search(s2)) and sum(w in s2 for w in ("fee", "burn", "collateral", "redeem", "redemption", "governance", "buyback")) >= 2,
        "3_akash_bme": ("act" in re.findall(r"\b[a-z]+\b", s3)) and any(w in s3 for w in ("burn", "mint")) and "marketplace" not in s3[:300],
        # Lexical: the answer stays on the Venice pattern and does not lead with receipt tokens or bridges.
        # It passes an honest "only Venice" answer, which is not an answer to "which other projects": the model
        # judge below names the other projects the answer actually establishes (review of c03378b1).
        "4_stays_on_pattern": ("venice" in s4) and not re.search(r"^.{0,400}\b(?:lido|rocket pool|bridge|wsteth|receipt)", s4, re.S),
        "4_other_projects_named": (other_analogues(by.get(4, "")) if model_judge else "not judged"),
        "clarified_or_empty": [t["turn"] for t in turns if refusal.search(synthesis(t.get("answer") or ""))],
        "seconds": [round((t.get("ms") or 0) / 1000) for t in sorted(turns, key=lambda t: t["turn"])],
    }


def main(dirs: list[str]) -> None:
    model_judge = "--model" in dirs
    dirs = [d for d in dirs if d != "--model"]
    runs: dict[str, list[dict]] = {}
    for d in dirs:
        path = Path(d) / "turns.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            r = json.loads(line)
            runs.setdefault(f"{d}:{r['journey']}", []).append(r)
    totals = {k: 0 for k in ("1_vvv_diem_flow", "2_links_carried", "3_akash_bme", "4_stays_on_pattern")}
    others = 0
    seconds: list[int] = []
    for name, turns in runs.items():
        s = score(turns, model_judge)
        for k in totals:
            totals[k] += int(bool(s[k]))
        named = s["4_other_projects_named"]
        others += int(model_judge and named.lower() != "none")
        seconds += s["seconds"]
        print(name, {k: (v if k in ("clarified_or_empty", "seconds", "4_other_projects_named") else ("yes" if v else "NO")) for k, v in s.items()})
    n = len(runs)
    if n:
        print(f"\n{n} runs:", ", ".join(f"{k} {v}/{n}" for k, v in totals.items()),
              (f"| 4_other_projects_named {others}/{n}" if model_judge else "| 4_other_projects_named: pass --model"),
              f"| seconds per turn: median {statistics.median(seconds):.0f}, max {max(seconds)}")


if __name__ == "__main__":
    main(sys.argv[1:])
