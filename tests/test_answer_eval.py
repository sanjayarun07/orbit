"""The answer requirements (evals/answers/cases.json) hold on their pinned
snapshots. Routing tests say the right tool was reached; this says the
answer satisfied the question, deterministically, relative to the time the
snapshot was captured."""
import json
from pathlib import Path

import pytest

from app.answer_checks import run_checks, verdict

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "evals" / "answers" / "cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_the_pinned_answer_meets_its_requirements(case):
    path = ROOT / "evals" / "answers" / "snapshots" / f"{case['id']}.json"
    if not path.exists():
        pytest.skip(f"no snapshot for {case['id']}: python scripts/answer_eval.py capture --case {case['id']}")
    results = run_checks(case, json.loads(path.read_text()))
    assert verdict(results), "\n".join(f"{r['check']} {r['args']}: {r['detail']}" for r in results if not r["ok"])


def test_checks_are_relative_to_the_capture_time_not_the_clock():
    from app.answer_checks import future_date
    snap = {"answer": "The next unlock is on 2026-10-16.", "captured_at": "2026-09-22T00:00:00+00:00"}
    assert future_date(snap)[0]
    assert not future_date({**snap, "captured_at": "2026-11-01T00:00:00+00:00"})[0]
