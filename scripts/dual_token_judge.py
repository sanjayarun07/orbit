"""Score the four-turn dual-token sequence (evals/journeys/dual-token-2026-09-24.json).

    .venv/bin/python scripts/dual_token_judge.py reports/dual-token-sol-5/part1 [more dirs...]

Four points per run, read from the synthesis above the cards:
  1. the VVV -> sVVV -> DIEM mint/burn flow (DIEM named, sVVV or lock/burn named)
  2. the dual-token links carried into turn two (fee/burn/collateral/redemption/governance named, not a refusal)
  3. Akash's AKT -> ACT burn-mint equilibrium (ACT named with burn or mint), not what Akash is
  4. own-token-collateral analogues: Venice named, and the answer does not lead with receipt tokens or bridges
Plus the seconds per turn and whether any turn was a clarification or a "nothing usable".
These are lexical checks for a repeated measurement; read the transcripts for the verdict.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path


def synthesis(answer: str) -> str:
    cut = answer.find("\n---\n")
    return (answer[:cut] if cut > 0 else answer).lower()


def score(turns: list[dict]) -> dict:
    by = {t["turn"]: (t.get("answer") or "") for t in turns}
    s1, s2, s3, s4 = (synthesis(by.get(i, "")) for i in (1, 2, 3, 4))
    refusal = re.compile(r"nothing usable|no (?:specific |available )?information|which token|name it \(")
    return {
        "1_vvv_diem_flow": ("diem" in s1) and any(w in s1 for w in ("svvv", "lock", "burn")),
        "2_links_carried": (not refusal.search(s2)) and sum(w in s2 for w in ("fee", "burn", "collateral", "redeem", "redemption", "governance", "buyback")) >= 2,
        "3_akash_bme": ("act" in re.findall(r"\b[a-z]+\b", s3)) and any(w in s3 for w in ("burn", "mint")) and "marketplace" not in s3[:300],
        "4_own_token_analogues": ("venice" in s4) and not re.search(r"^.{0,400}\b(?:lido|rocket pool|bridge|wsteth|receipt)", s4, re.S),
        "clarified_or_empty": [t["turn"] for t in turns if refusal.search(synthesis(t.get("answer") or ""))],
        "seconds": [round((t.get("ms") or 0) / 1000) for t in sorted(turns, key=lambda t: t["turn"])],
    }


def main(dirs: list[str]) -> None:
    runs: dict[str, list[dict]] = {}
    for d in dirs:
        path = Path(d) / "turns.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            r = json.loads(line)
            runs.setdefault(f"{d}:{r['journey']}", []).append(r)
    totals = {k: 0 for k in ("1_vvv_diem_flow", "2_links_carried", "3_akash_bme", "4_own_token_analogues")}
    seconds: list[int] = []
    for name, turns in runs.items():
        s = score(turns)
        for k in totals:
            totals[k] += int(bool(s[k]))
        seconds += s["seconds"]
        print(name, {k: (v if k in ("clarified_or_empty", "seconds") else ("yes" if v else "NO")) for k, v in s.items()})
    n = len(runs)
    if n:
        print(f"\n{n} runs:", ", ".join(f"{k} {v}/{n}" for k, v in totals.items()),
              f"| seconds per turn: median {statistics.median(seconds):.0f}, max {max(seconds)}")


if __name__ == "__main__":
    main(sys.argv[1:])
