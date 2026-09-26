"""Is the largest holder the deployer? Answered from state, never from the web.

The largest position comes from Mobula's holder positions (full addresses),
the deployer from Mobula's token metadata; the two are compared by address.
What the sources do not carry -- a transfer between them, who funded whom --
is said to be not established (the regression run of 2026-09-25 had one of
three runs assert a deployer wallet from a web article).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_QUESTION = re.compile(
    r"\b(?:is|was|isn'?t)\s+(?:the\s+)?(?:largest|biggest|top|first|\d+(?:\.\d+)?\s*%\s*)?\s*(?:[A-Za-z0-9$]+\s+)?(?:account|wallet|holder|position|address)\s+(?:the\s+|a\s+)?(?:deployer|creator|dev(?:eloper)?|team\s+wallet)\b"
    r"|\bwho\s+(?:is\s+)?(?:the\s+)?deployer\b|\bdeployer\s+(?:of|for|behind)\b|\bwho\s+(?:deployed|created|launched)\b", re.I)


def is_deployer_question(text: str) -> bool:
    return bool(_QUESTION.search(text or ""))


def _short(address: str) -> str:
    return f"{address[:4]}…{address[-4:]}" if address and len(address) > 12 else (address or "—")


async def answer(mint: str, chain: str, symbol: str | None = None) -> tuple[str, dict]:
    """(the reply, its trajectory). Raises when Mobula has no holder rows."""
    from app import mobula_meme
    rows = await asyncio.to_thread(mobula_meme._get, "/token/holder-positions", {"address": mint, "blockchain": chain, "limit": 50})
    rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("walletAddress")]
    if not rows:
        raise RuntimeError("Mobula has no holder positions for this token")
    rows.sort(key=lambda r: float(r.get("percentageOfTotalSupply") or 0), reverse=True)
    largest = rows[0]
    try:
        deployer = await asyncio.to_thread(mobula_meme.token_deployer, mint, chain)
    except Exception:
        logger.info("deployer check: token metadata unavailable", exc_info=True)
        deployer = None
    wallet = str(largest["walletAddress"])
    share = float(largest.get("percentageOfTotalSupply") or 0)
    labels = mobula_meme._labels_of(largest)
    name = symbol or mint[:6]
    burn = mobula_meme.is_burn_address(wallet)
    if deployer:
        same = wallet == deployer
        verdict = (f"Yes: the largest {name} position, `{_short(wallet)}` ({share:.2f}% of supply), is the wallet Mobula's metadata names as the deployer."
                   if same else
                   f"No: the largest {name} position, `{_short(wallet)}` ({share:.2f}% of supply), is not the deployer wallet Mobula's metadata names (`{_short(deployer)}`).")
    else:
        same = None
        verdict = (f"Not established: Mobula records no deployer for {name}, so the largest position `{_short(wallet)}` ({share:.2f}% of supply) "
                   "cannot be compared with one.")
    if burn:
        verdict += " That address is a burn address."
    tail = (" No transaction links them in these sources: whether the deployer funded that wallet is not established. "
            "Ask for that wallet's history to check transfers.")
    checked = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    card = "\n".join([
        f"# Deployer check · {name}",
        f"**Provider**: Mobula · **Contract**: `{mint}` · **Chain**: {chain} · **Checked**: {checked}",
        "",
        f"- **Largest position**: `{wallet}` · {share:.2f}% of supply · labels: {', '.join(labels) if labels else 'none'}",
        f"- **Deployer per Mobula metadata**: " + (f"`{deployer}`" if deployer else "not recorded"),
        f"- **Same wallet**: " + ("yes" if same else "no" if same is False else "cannot be compared"),
        "",
        "Holder positions and token metadata are Mobula's records; labels are Mobula's classifications, not proof of identity.",
    ])
    trajectory = {"thought_0": "The largest position and the deployer are compared by address from Mobula's records, never from a web page.",
                  "tool_name_0": "mobula_token_holders", "tool_args_0": {"request": f"holders of {mint} on {chain}"}, "observation_0": card}
    return f"{verdict}{tail}\n\n---\n\n{card}", trajectory
