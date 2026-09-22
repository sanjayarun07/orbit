"""The holders question as a durable job: the pilot for consolidated research.

"Who holds X" is answered today by one router tool (Mobula holder
positions, with Bitquery and the Solana RPC behind it) and, separately,
by the security profile's own top-10 figure, which applies Mobula's
exclusions and can differ by 3x. This handler asks both under one plan,
through the operation cache, and answers with both figures labelled by
their definitions, the flagged wallets, and an envelope that says what
was and was not seen. It exists to be measured against the current path
(scripts/holders_pilot.py): correctness, latency, provider calls.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app import evidence, jobs, mobula_meme, mobula_security
from app.provider_router import NoData

KIND = "inspect_holders"


async def inspect_holders(job: dict, ctx: "jobs.JobContext") -> dict:
    spec = job["spec"]
    address, chain, symbol = spec["address"], spec["chain"], spec.get("symbol")
    await ctx.plan(["Holder positions", "Security profile", "Answer"])

    await ctx.step("0")
    try:
        holders_card = await ctx.call("mobula:holders", lambda: asyncio.to_thread(mobula_meme.token_holders, f"top holders of {address} on {chain}"),
                                      args=[address, chain], volatile=True)
    except NoData as exc:
        holders_card = None
        await ctx.event("error", "No holder positions", str(exc))
    await ctx.finish_step("0")

    await ctx.step("1")
    try:
        profile = await ctx.call("mobula:security", lambda: asyncio.to_thread(mobula_security.token_security, f"{address} on {chain}"), args=[address, chain])
    except Exception as exc:  # noqa: BLE001 - one source down is a gap, not a failure
        profile = None
        await ctx.event("error", "Security profile unavailable", f"{type(exc).__name__}: {exc}"[:300])
    await ctx.finish_step("1")

    await ctx.step("2")
    envs = {e.tool: e for e in evidence.collected()}
    holders_env = envs.get("mobula_token_holders")
    positions_top10 = (holders_env.data.get("top10_pct_of_supply") if holders_env and holders_env.status == "complete" else None)
    profile_top10 = _profile_top10(profile)
    name = symbol or address
    lines = [f"# Who holds {name}", f"**Chain**: {chain} · **Contract**: `{address}` · **Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", ""]
    if positions_top10 is not None:
        lines.append(f"- **Top 10 by indexed positions**: {positions_top10:.2f}% of supply (the ten largest positions Mobula indexes; pools, exchanges and burn addresses included).")
    if profile_top10 is not None:
        lines.append(f"- **Top 10 by Mobula's security profile**: {profile_top10:.2f}% of supply (Mobula's own exclusions; a lower figure than the positions view is expected).")
    if positions_top10 is not None and profile_top10 is not None and abs(positions_top10 - profile_top10) > 5:
        lines.append(f"- The two figures differ by {abs(positions_top10 - profile_top10):.1f} points because they count different wallets; neither describes beneficial owners.")
    gaps = [e.gap() for e in envs.values() if e.status != "complete"]
    if holders_card is None:
        gaps.append("No holder positions were indexed for this contract.")
    if gaps:
        lines += ["", "Not seen: " + " ".join(gaps)]
    if holders_card:
        lines += ["", holders_card]
    if profile:
        lines += ["", profile]
    await ctx.finish_step("2")
    return {"answer": "\n".join(lines), "trajectory": {"tool_name_0": "inspect_holders", "tool_args_0": {"address": address, "chain": chain}},
            "figures": {"positions_top10": positions_top10, "profile_top10": profile_top10}}


def _profile_top10(card: str | None) -> float | None:
    import re
    if not card:
        return None
    m = re.search(r"\*\*Top 10\*\*:\s*([0-9.]+)%", card)
    return float(m.group(1)) if m else None


jobs.register(KIND, inspect_holders)
