"""Competition signals, not authorization rules.

These signals only defer a lexical execution candidate to semantic resolution.
Adding a signal can never grant access to execution.
"""

import re


_CONDITIONAL_ORDER = re.compile(
    r"\b(?:if|when|once|whenever|as\s+soon\s+as)\b[^.?!]{0,80}\b(?:buy|sell|swap|long|short|bridge|convert)\b|\bautomatically\s+(?:buy|sell|swap|trade|execute)\b|\b(?:stop[- ]loss|limit\s+order|take[- ]profit)\b", re.I)


def is_conditional_order(request: str) -> bool:
    """A conditional or automatic trade: "if SOL drops under 100 buy 2 SOL",
    "automatically buy". Orbit never executes on its own; the answer must say
    so and offer the alert that exists (UI run, 2026-09-23)."""
    text = request or ""
    if re.search(r"\b(?:should|would|could|can|shall|do|does)\s+i\b|\bwhat\s+if\b|\bwhether\b|\?\s*$", text, re.I):
        return False                                                   # a question about trading is advice, not an order
    return bool(_CONDITIONAL_ORDER.search(text))


CONDITIONAL_ORDER_ANSWER = (
    "Conditional and automatic orders are not something Orbit can do: it never buys, sells or submits a transaction on its own, "
    "and there are no stop-loss, limit or take-profit orders here. Every trade is prepared for review and signed by you in your wallet at that moment.\n\n"
    "What exists: a price alert (`alert me when SOL drops below $100`) that posts to your inbox when the level is crossed, so you can decide then; "
    "and an exit watch (`watch my exit on BONK`) that re-quotes a position you hold and warns when the exit deteriorates. Neither places an order."
)


def has_competing_speech(request: str) -> bool:
    tokens = re.findall(r"[a-z]+", request.lower())
    words = set(tokens)
    command = tokens[1:] if tokens[:1] == ["please"] else tokens
    verbs = {"swap", "buy", "sell", "exchange", "bridge", "trade", "convert", "move"}
    return bool(
        "?" in request
        or (words & verbs and (not command or command[0] not in verbs))
        or words & {
            "should", "would", "could", "whether", "if", "when", "why", "how",
            "explain", "about", "advice", "recommend", "best", "volume",
            "history", "transactions", "news", "not", "never", "dont",
            "where", "fees", "fee", "costs", "rates", "liquidity",
        }
        or "don't" in request.lower()
        or {"buy", "sell"} <= words
    )


BARE_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(%|percent|bps?|basis\s+points?)?\s*$", re.I)


def bare_number_bps(request: str) -> int | None:
    """A message that is only a number, as an answer to "what slippage?":
    "50" or "50 bps" is basis points, "0.5%" is 50 bps. None otherwise."""
    m = BARE_NUMBER.match(request or "")
    if not m:
        return None
    value, unit = float(m.group(1)), (m.group(2) or "").lower()
    bps = round(value * 100) if unit in {"%", "percent"} else round(value)
    return bps if 0 < bps <= 10_000 else None


def is_parameter_fragment(request: str) -> bool:
    """Closed trade parameter vocabulary, only useful with an active workflow.
    A bare number counts: the planner asks "what slippage, in basis points?"
    and the everyday answer is "50", which has no words at all."""
    from .lexicon import CHAIN_ALIASES

    if bare_number_bps(request) is not None:
        return True
    words = re.findall(r"[a-z]+", request.lower())
    allowed = set("on from to with max maximum bps bp basis points slippage sol eth usdc usdt bnb avax pol".split())
    allowed.update(word for alias in CHAIN_ALIASES for word in alias.split())
    return bool(words) and all(word in allowed for word in words) and not has_competing_speech(request)
