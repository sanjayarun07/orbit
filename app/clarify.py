"""What a clarifying answer looks like, and what market text looks like.

From the twenty-prompt live run of 2026-09-18: a clarification must be a
terminal state. The web's own "which unlock do you mean?" was read as
context and synthesized into a SUI unlock nobody asked about; a "please
send the full names" answer grew related questions; a probe that said
"concept" (Mercury retrograde) was merged with an astronomy answer. Every
layer that decides whether to build on a text now asks these two
questions first.
"""
from __future__ import annotations

import re

# Phrases that only a question to the user contains, wherever they sit.
_ASKS_BACK = re.compile(
    r"\b(?:which one (?:do )?you mean|do you mean|reply with|please (?:specify|clarify|send|name|paste|share|add|tell me)"
    r"|could you (?:please )?(?:specify|clarify|tell me)|i need (?:a bit )?more (?:context|detail)|i need to know|name the token|name it \("
    r"|what does .{1,40} refer to|which (?:token|chain|one|wallet|contract)(?:'s| do| should| is)?\b[^.\n]{0,60}\?)",
    re.I,
)
# A line that opens with a question word, in the first lines of the text.
_ASKS_BACK_HEAD = re.compile(r"(?:^|\n)\s*(?:which|what does|could you|do you mean)\b", re.I)
_TRAILING_QUESTION = re.compile(r"\?\s*(?:\*[^*\n]{0,160}\*\s*|_[^_\n]{0,160}_\s*)?$")

MARKET = re.compile(
    r"\b(?:token|tokens|coin|coins|crypto|cryptocurrency|blockchain|memecoin|meme coin|solana|ethereum|base|arbitrum|bsc|bnb chain|polygon|"
    r"defi|protocol|dex|exchange|listed|listing|ticker|stock|shares|equity|nasdaq|nyse|nse|price|market cap|mcap|liquidity|trading|traded|"
    r"holders|supply|circulating|vesting|unlock|unlocks|cliff|tge|tokenomics|allocation|emissions|airdrop|tvl|wallet|onchain|on-chain|mint|contract address)\b",
    re.I,
)


def is_clarification(text: str | None) -> bool:
    """True when the text asks the user for the missing subject or detail
    instead of answering: a question in its first lines, or a question as
    its last line (a trailing disclaimer or emphasis allowed)."""
    body = (text or "").strip()
    if not body:
        return False
    if _ASKS_BACK.search(body):
        return True
    head = "\n".join(body.splitlines()[:4])
    if _ASKS_BACK_HEAD.search(head):
        return True
    return bool(_TRAILING_QUESTION.search(body))


def is_market_text(text: str | None, window: int = 800) -> bool:
    """True when the opening of the text is about a token, protocol, company
    or market -- what Orbit answers about -- rather than the sky, a medicine
    or a politician."""
    head = (text or "")[:window]
    return len(MARKET.findall(head)) >= 2
