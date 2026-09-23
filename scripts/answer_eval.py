"""Answer requirements, captured live and replayed on pinned snapshots.

    python scripts/answer_eval.py capture [--base http://127.0.0.1:8001] [--case id ...]
    python scripts/answer_eval.py replay [--judge]

`capture` signs in through the development magic link (DEV_EXPOSE_MAGIC_LINKS
on the target), asks each case's prompt, and pins the answer, its public
trajectory, its evidence envelopes, its validation and the capture time under
evals/answers/snapshots/<id>.json; the throwaway account is deleted at the
end. `replay` runs each case's deterministic checks (app/answer_checks.py)
against its snapshot and exits 1 on any failure; `--judge` also asks the
answer gate's model judge whether the answer addresses the prompt.
tests/test_answer_eval.py replays the pinned snapshots on every commit.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CASES = ROOT / "evals" / "answers" / "cases.json"
SNAPSHOTS = ROOT / "evals" / "answers" / "snapshots"


def load_cases(ids: list[str] | None = None) -> list[dict]:
    cases = json.loads(CASES.read_text())["cases"]
    return [c for c in cases if not ids or c["id"] in ids]


def capture(base: str, ids: list[str] | None) -> int:
    import httpx

    client = httpx.Client(base_url=base, timeout=300)
    email = f"answer-eval-{int(time.time())}@example.invalid"
    start = client.post("/auth/email/start", json={"email": email}).json()
    if not start.get("dev_link"):
        print("the target does not expose the development sign-in link; capture needs DEV_EXPOSE_MAGIC_LINKS=true on a dev server")
        return 2
    client.post("/auth/email/verify", json={"token": start["dev_link"].split("signin=")[1]})
    failures = 0
    try:
        for case in load_cases(ids):
            t0 = time.monotonic()
            r = client.post("/chat", json={"message": case["prompt"]})
            body = r.json()
            snapshot = {
                "id": case["id"], "prompt": case["prompt"], "captured_at": datetime.now(timezone.utc).isoformat(),
                "http_status": r.status_code, "latency_ms": int((time.monotonic() - t0) * 1000),
                "answer": body.get("answer"), "intent": body.get("intent"), "trajectory": body.get("trajectory"),
                "envelopes": body.get("envelopes") or [], "validation": body.get("validation"),
            }
            SNAPSHOTS.mkdir(parents=True, exist_ok=True)
            (SNAPSHOTS / f"{case['id']}.json").write_text(json.dumps(snapshot, indent=2, default=str))
            print(f"{case['id']:<14} http {r.status_code} in {snapshot['latency_ms'] / 1000:.1f}s -> snapshots/{case['id']}.json")
            failures += r.status_code != 200
    finally:
        client.request("DELETE", "/me", json={"confirm_email": email})
    return 1 if failures else 0


def replay(ids: list[str] | None, judge: bool) -> int:
    from app.answer_checks import run_checks, verdict

    failed = 0
    for case in load_cases(ids):
        path = SNAPSHOTS / f"{case['id']}.json"
        if not path.exists():
            print(f"{case['id']:<14} NO SNAPSHOT (capture it first)")
            failed += 1
            continue
        snapshot = json.loads(path.read_text())
        results = run_checks(case, snapshot)
        ok = verdict(results)
        judged = ""
        if judge:
            from app import answer_gate
            from app.settings import settings

            settings.answer_gate_enabled = True
            out = asyncio.run(answer_gate.check(case["prompt"], snapshot.get("answer") or ""))
            judged = f" · judge: {out['verdict']}" + (f" ({out['missing']})" if out.get("missing") else "")
            ok = ok and out["verdict"] in ("answers", "asks")
        print(f"{case['id']:<14} {'PASS' if ok else 'FAIL'}  captured {snapshot['captured_at'][:16]}{judged}")
        for r in results:
            print(f"    {'ok ' if r['ok'] else 'NO '} {r['check']:<16} {r['detail']}")
        failed += not ok
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("capture"); c.add_argument("--base", default="http://127.0.0.1:8001"); c.add_argument("--case", nargs="*")
    r = sub.add_parser("replay"); r.add_argument("--case", nargs="*"); r.add_argument("--judge", action="store_true")
    args = ap.parse_args()
    return capture(args.base, args.case) if args.mode == "capture" else replay(args.case, args.judge)


if __name__ == "__main__":
    sys.exit(main())
