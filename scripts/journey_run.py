"""Run multi-turn journeys through the real chat turn, one session per
journey, so a correction or a topic switch meets the context the previous
turn left (the expanded UI review of 2026-09-24: every four-turn journey
failed end to end). A journey is a list of prompts; the record is one line
per turn with the answer, tools, the contract the turn used and the session
context's focus and last contract after it.

    .venv/bin/python scripts/journey_run.py journeys.json --out reports/journeys-<sha> [--wallet ADDRESS]

journeys.json: {"J2": ["Top Hyperliquid crypto perp gainers today.", "I meant the past hour, not 24 hours.", ...], ...}
A prompt prefixed with "fresh:" starts a new session inside the journey.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TESTER_USER_ID = "a5b1b793-7421-45b3-83a3-aac056a753f0"


async def run(journeys: dict, out_dir: Path, wallet: str | None, repeat: int = 1) -> None:
    from app import accounts, sessions, tequity
    from app.execution_policy import execute_chat_turn
    from app.identity import _identity_for_user
    from app.models import ChatRequest
    from app.settings import settings
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
    identity = await _identity_for_user(user, "127.0.0.1")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    records = out_dir / "turns.jsonl"
    transcript = out_dir / "transcript.md"
    transcript.write_text(f"# Journeys — {stamp} UTC — build {git_sha()} — wallet {wallet or 'none'}\n\n")
    journeys = {(f"{name}#{i + 1}" if repeat > 1 else name): prompts for name, prompts in journeys.items() for i in range(repeat)}
    for name, prompts in journeys.items():
        session = f"journey-{stamp}-{name}"
        with transcript.open("a") as t:
            t.write(f"## {name}\n\n")
        for i, prompt in enumerate(prompts, start=1):
            if prompt.startswith("fresh:"):
                session = f"journey-{stamp}-{name}-fresh{i}"
                prompt = prompt[len("fresh:"):].strip()
            t0 = time.time()
            rec = {"journey": name, "turn": i, "prompt": prompt, "session": session}
            try:
                for attempt in range(4):
                    try:
                        resp = await asyncio.wait_for(execute_chat_turn(ChatRequest(message=prompt, session_id=session, wallet_address=wallet), identity), timeout=170)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if attempt < 3 and ("already updating this chat" in str(exc) or "Too many chat requests" in str(exc)):
                            await asyncio.sleep(20)
                            continue
                        raise
                traj = resp.trajectory if isinstance(resp.trajectory, dict) else {}
                ctx = await sessions.get_session_context(session)
                rec.update(status="ok", answer=resp.answer or "", intent=resp.intent, tools=[v for k, v in traj.items() if k.startswith("tool_name")],
                           focus=ctx.get("focus"), last_contract=ctx.get("last_contract"), research_objective=ctx.get("research_objective"))
            except Exception as exc:  # noqa: BLE001
                rec.update(status="error", error=repr(exc)[:300], answer="")
            rec["ms"] = round((time.time() - t0) * 1000)
            with records.open("a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
            with transcript.open("a") as t:
                t.write(f"**{i}. {prompt}**\n\n_{rec['status']} · {rec['ms'] / 1000:.1f}s · tools {rec.get('tools')} · focus {json.dumps(rec.get('focus'), default=str)[:80]} · last_contract {json.dumps(rec.get('last_contract'), default=str)[:120]}_\n\n{rec.get('answer') or rec.get('error')}\n\n---\n\n")
            print(f"{name:4} {i} {rec['status']:6} {rec['ms'] / 1000:5.1f}s {rec.get('tools', [])[:3]} | {(rec.get('answer') or rec.get('error') or '')[:90].replace(chr(10), ' / ')}", flush=True)
            await asyncio.sleep(2.0)


def git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("journeys")
    ap.add_argument("--out", default=None)
    ap.add_argument("--wallet", default=None)
    ap.add_argument("--repeat", type=int, default=1, help="run every journey this many times (fresh sessions each time)")
    ap.add_argument("--only", default=None, help="comma-separated journey names")
    args = ap.parse_args()
    journeys = json.loads(Path(args.journeys).read_text())
    if args.only:
        keep = set(args.only.split(","))
        journeys = {k: v for k, v in journeys.items() if k in keep}
    out_dir = Path(args.out or (ROOT / f"reports/journeys-{git_sha()}"))
    asyncio.run(run(journeys, out_dir, args.wallet, args.repeat))


if __name__ == "__main__":
    main()
