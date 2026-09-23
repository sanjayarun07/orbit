"""Re-run the recorded trust reviews on one frozen build, through the real
chat turn (app/execution_policy.execute_chat_turn: routing, session context,
history, credits, turn log), as the tester account.

Cases come from the reviewers' matrices: the 94-case persona run
(reports/e2e-recorded-2026-09-23/matrix.csv) and the Home Explore review
(reports/home-explore-2026-09-23/matrix.csv), plus the four headline cards
Home shows right now. Browser-only cases (UI navigation, modals) and cases
that create notifications, alerts or reminders are skipped and listed as
such. Each persona keeps one session so follow-ups carry context.

    .venv/bin/python scripts/trust_rerun.py --out reports/trust-rerun-<sha>

Writes turns.jsonl (one record per turn: prompt, expected, answer, tools,
latency, error) and transcript.md; the judgement is a separate read.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TESTER_USER_ID = "a5b1b793-7421-45b3-83a3-aac056a753f0"
SKIP_PERSONAS = {"ui_navigation"}
SKIP_EXPLORE = {"E24", "E25", "E26", "E27"}          # modal, alert, reminder, task list


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for r in csv.DictReader(open(ROOT / "reports/e2e-recorded-2026-09-23/matrix.csv")):
        if r["persona"] in SKIP_PERSONAS or not r["prompt"].strip():
            continue
        if (r.get("requires_confirmation") or "").strip().lower() == "true":
            cases.append({"id": r["id"], "persona": r["persona"], "prompt": r["prompt"], "expected": r["expected"], "prior": r["status"], "skip": "creates a notification, alert, reminder or watch"})
            continue
        cases.append({"id": r["id"], "persona": r["persona"], "prompt": r["prompt"], "expected": r["expected"], "prior": r["status"]})
    for r in csv.DictReader(open(ROOT / "reports/home-explore-2026-09-23/matrix.csv")):
        if r["group"] != "Explore" or r["id"] in SKIP_EXPLORE:
            continue
        cases.append({"id": r["id"], "persona": "explore", "prompt": r["prompt"], "expected": r["title"], "prior": r["verdict"].lower(), "session": "each"})
    return cases


async def headline_cases() -> list[dict]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            data = (await client.get("http://127.0.0.1:8000/home/highlights")).json()
    except Exception as exc:  # noqa: BLE001
        return [{"id": "N-fetch", "persona": "news", "prompt": "", "expected": "", "prior": "", "skip": f"highlights unavailable: {exc!r}"}]
    out = []
    for i, card in enumerate(data.get("cards") or []):
        prompt = card.get("prompt") or f"What does this mean for the market: {card.get('title')}"
        out.append({"id": f"N{i:02d}", "persona": "news", "prompt": prompt, "expected": f"headline: {card.get('title')}", "prior": "", "session": "each",
                    "headline": card.get("title"), "card": card})
    return out


async def run(cases: list[dict], out_dir: Path) -> None:
    from app import accounts
    from app.execution_policy import execute_chat_turn
    from app.identity import _identity_for_user
    from app.models import ChatRequest
    from app.settings import settings
    from app import tequity
    loop = asyncio.get_running_loop()
    tequity.set_loop(loop)
    try:
        from app.knowledge import tool as kb_tool
        from app import sentiment_analyst
        kb_tool.set_loop(loop)
        sentiment_analyst.set_loop(loop)
    except Exception:
        pass
    settings.provider_semantic_cache_threshold = 1.01
    from app.nodes import runtime as runtime_mod
    for lm in (runtime_mod._primary_lm, runtime_mod._intent_lm, runtime_mod._synthesis_lm, runtime_mod._planner_lm, getattr(runtime_mod, "_fallback_lm", None)):
        if lm is not None:
            lm.cache = False
    user = await accounts.get_user(TESTER_USER_ID)
    if not user:
        raise SystemExit("tester user not found")
    identity = await _identity_for_user(user, "127.0.0.1")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")     # unique per run: the chat lock is per session id
    out_dir.mkdir(parents=True, exist_ok=True)
    records = out_dir / "turns.jsonl"
    transcript = out_dir / "transcript.md"
    with transcript.open("w") as t:
        t.write(f"# Trust re-run — {stamp} UTC — build {git_sha()}\n\nTester account, no connected wallet. One session per persona.\n\n")
    for n, case in enumerate(cases, start=1):
        rec = {**case, "at": datetime.now(timezone.utc).isoformat()}
        rec.pop("card", None)
        if case.get("skip"):
            rec["status"] = "skipped"
        else:
            session = f"trust-{stamp}-{case['persona']}" + (f"-{case['id']}" if case.get("session") == "each" else "")
            t0 = time.time()
            try:
                for attempt in (0, 1):
                    try:
                        resp = await asyncio.wait_for(execute_chat_turn(ChatRequest(message=case["prompt"], session_id=session), identity), timeout=170)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt == 0 and "already updating this chat" in str(exc):
                            await asyncio.sleep(12)          # the previous turn's lock is still expiring
                            continue
                        raise
                rec.update(status="ok", answer=resp.answer or "", intent=resp.intent, capabilities=list(resp.capabilities or []),
                           tools=[v for k, v in (resp.trajectory or {}).items() if k.startswith("tool_name")] if isinstance(resp.trajectory, dict) else [],
                           gate=getattr(resp, "gate", None), validation=getattr(resp, "validation", None))
            except Exception as exc:  # noqa: BLE001
                rec.update(status="error", error=repr(exc)[:300], answer="")
            rec["ms"] = round((time.time() - t0) * 1000)
            rec["session"] = session
        with records.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
        with transcript.open("a") as t:
            t.write(f"## {case['id']} ({case['persona']}, prior {case.get('prior') or '-'})\n\n**Prompt:** {case['prompt']}\n\n**Expected:** {case.get('expected') or '-'}\n\n")
            if rec["status"] == "skipped":
                t.write(f"_Skipped: {case['skip']}_\n\n")
            else:
                t.write(f"_{rec['status']} · {rec.get('ms', 0) / 1000:.1f}s · tools {rec.get('tools')}_\n\n{rec.get('answer') or rec.get('error')}\n\n---\n\n")
        print(f"{n:3}/{len(cases)} {case['id']:24} {rec['status']:8} {rec.get('ms', 0) / 1000:6.1f}s {rec.get('tools', [])[:3]}", flush=True)
        if rec["status"] != "skipped":
            await asyncio.sleep(2.0)


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None, help="comma-separated case ids or personas")
    args = ap.parse_args()
    out_dir = Path(args.out or (ROOT / f"reports/trust-rerun-{git_sha()}"))
    cases = load_cases()
    cases += asyncio.run(headline_cases())
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep or c["persona"] in keep]
    print(f"{len(cases)} cases -> {out_dir}", flush=True)
    asyncio.run(run(cases, out_dir))


if __name__ == "__main__":
    main()
