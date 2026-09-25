"""Social handles in wallet asks.

Live, 2026-09-21: a beta user asked "@frankdegods wallet analysis". Anvaya
has no way to turn an X or Telegram handle into a wallet, and instead of
saying so it ran the portfolio tools on the user's own connected wallet and
then explained that "the address you provided appears to be an
Ethereum-style address". Wrong on both counts. A handle in a wallet ask is
answered honestly: not resolvable here yet, paste the address.

Not a handle: an email-looking token, or "@" followed by nothing wallet-like.
"""
from __future__ import annotations

import re

_HANDLE = re.compile(r"(?<![\w.])@([A-Za-z0-9_]{2,32})\b")
_WALLET_WORDS = re.compile(r"\b(?:wallet|portfolio|holdings?|positions?|balances?|pnl|profit|trades?|bags?|what\s+(?:does|do)\s+\S+\s+(?:hold|own))\b", re.I)
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def social_handle(request: str) -> str | None:
    """The handle a wallet ask names, when it names one and no address."""
    text = request or ""
    if _ADDRESS.search(text) or "@" not in text:
        return None
    if re.search(r"\S+@\S+\.\S+", text):  # an email, not a handle
        return None
    match = _HANDLE.search(text)
    if not match or not _WALLET_WORDS.search(text):
        return None
    return match.group(1)


def answer_for(handle: str) -> str:
    return (f"I can't turn **@{handle}** into a wallet yet: Anvaya doesn't resolve X or Telegram handles to addresses. "
            "Paste the wallet address (fomo.family, GMGN and most trackers show it on the profile) and I'll run the full portfolio "
            "and trading analysis on it.")
