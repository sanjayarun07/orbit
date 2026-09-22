"""The holders pilot: the consolidated job (app/jobs_holders.py) against the
current router tool, on the same tokens, measured.

    python scripts/holders_pilot.py [--tokens BONK,WIF,...]

For each token: the current path (the router's holders tool, as a chat turn
would call it) and the job path (holder positions + security profile under
one plan, through the operation cache). Prints latency, Mobula requests
made, the top-10 figures each produced, and whether the envelope disclosed
a gap. Reads provider keys from the configured environment; never prints
them. Costs a handful of Mobula calls per token.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import evidence, jobs, jobs_holders, metrics  # noqa: E402,F401
from app.provider_registry import get_provider_router  # noqa: E402
from app.symbol_registry import contract, leader, listed  # noqa: E402

DEFAULT = "BONK,WIF,FARTCOIN"


def _mobula_calls() -> int:
    return int(metrics.snapshot().get("mobula_requests", 0))


async def current_path(address: str, chain: str) -> dict:
    router = get_provider_router()
    t0, calls = time.monotonic(), _mobula_calls()
    token = evidence.start_turn()
    try:
        result = await asyncio.to_thread(router.try_route, f"top holders of {address} on {chain}", "token_discovery", (chain,))
        envs = evidence.collected()
    finally:
        evidence.end_turn(token)
    env = next((e for e in envs if e.tool == "mobula_token_holders"), None)
    return {"latency_s": round(time.monotonic() - t0, 1), "mobula_calls": _mobula_calls() - calls,
            "tool": getattr(result, "tool", None), "positions_top10": env.data.get("top10_pct_of_supply") if env else None,
            "profile_top10": None, "gaps": [e.gap() for e in envs if e.status != "complete"]}


async def job_path(address: str, chain: str, symbol: str) -> dict:
    t0, calls = time.monotonic(), _mobula_calls()
    job = await jobs.create(jobs_holders.KIND, {"address": address, "chain": chain, "symbol": symbol})
    row = await jobs.run(job)
    figures = (row.get("result") or {}).get("figures") or {}
    envs = row.get("evidence") or []
    return {"latency_s": round(time.monotonic() - t0, 1), "mobula_calls": _mobula_calls() - calls, "tool": "inspect_holders job",
            "positions_top10": figures.get("positions_top10"), "profile_top10": figures.get("profile_top10"),
            "gaps": [e.get("coverage", {}).get("missing") for e in envs if e.get("status") != "complete"], "status": row["status"]}


async def main(tokens: list[str]) -> int:
    print(f"{'token':<10} {'path':<8} {'s':>6} {'calls':>5}  {'positions top10':>15}  {'profile top10':>13}  gaps")
    for symbol in tokens:
        lead = leader(listed(symbol))
        resolved = contract(lead["id"]) if lead else None
        if not resolved:
            print(f"{symbol:<10} unresolved (symbol registry)")
            continue
        address, chain = resolved
        for name, fn in (("current", lambda: current_path(address, chain)), ("job", lambda: job_path(address, chain, symbol))):
            out = await fn()
            p, q = out.get("positions_top10"), out.get("profile_top10")
            print(f"{symbol:<10} {name:<8} {out['latency_s']:>6} {out['mobula_calls']:>5}  {'' if p is None else f'{p:.2f}%':>15}  {'' if q is None else f'{q:.2f}%':>13}  {out['gaps'] or '-'}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokens", default=DEFAULT)
    args = ap.parse_args()
    sys.exit(asyncio.run(main([t.strip().upper() for t in args.tokens.split(",") if t.strip()])))
