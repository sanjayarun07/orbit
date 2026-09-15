"""High-precision execution control detection."""

import re

from .lexicon import (
    CHARTER_CLEAR,
    CHARTER_SET,
    CHARTER_SHOW,
    EXECUTION_EXPLANATION,
    TEAM_DISABLE,
    TEAM_ENABLE,
    TEAM_STATUS,
    TRADE_CANCEL,
    TRADE_CONFIRM,
    TRADE_MODIFIER,
)


def is_execution_explanation(request: str) -> bool:
    return bool(EXECUTION_EXPLANATION.search(request))


def is_charter_set(request: str) -> bool:
    return bool(CHARTER_SET.match(request))


def is_charter_show(request: str) -> bool:
    return bool(CHARTER_SHOW.match(request))


def is_charter_clear(request: str) -> bool:
    return bool(CHARTER_CLEAR.match(request))


def is_charter_command(request: str) -> bool:
    return is_charter_set(request) or is_charter_show(request) or is_charter_clear(request)


def extract_charter(request: str) -> str | None:
    """Pull the rules text out of a 'set my risk charter: ...' message."""
    match = CHARTER_SET.match(request)
    if not match:
        return None
    rules = (match.group("rules") or match.group("rules2") or "").strip()
    return rules or None


def is_team_enable(request: str) -> bool:
    return bool(TEAM_ENABLE.match(request))


def is_team_disable(request: str) -> bool:
    return bool(TEAM_DISABLE.match(request))


def is_team_status(request: str) -> bool:
    return bool(TEAM_STATUS.match(request))


def is_team_command(request: str) -> bool:
    return is_team_enable(request) or is_team_disable(request) or is_team_status(request)


def is_trade_cancellation(request: str) -> bool:
    return bool(TRADE_CANCEL.match(request))


def is_trade_confirmation(request: str) -> bool:
    return bool(TRADE_CONFIRM.match(request))


def is_trade_modifier(request: str) -> bool:
    return bool(TRADE_MODIFIER.match(request)) and bool(re.search(
        r"\b(?:bps?|basis\s+points?|slippage|amount|recipient|destination|source|chain|token)\b"
        r"|(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)\s*(?:SOL|ETH|USDC|USDT|BNB|AVAX|POL)\b"
        r"|0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,44}", request, re.I,
    ))


_WALLET_ACK_BARE = re.compile(r"^\W*(?:yes|yep|yeah|ok(?:ay)?|sure|done|ready|now)(?:[\s,.!-]+(?:yes|yep|yeah|ok(?:ay)?|sure|done|ready|now))*\W*$", re.IGNORECASE)
_WALLET_ACK_MARKER = re.compile(r"\b(?:connected|done|ready|try\s+again|retry|go\s+ahead|proceed|continue)\b", re.IGNORECASE)
_WALLET_ACK_NEGATION = re.compile(r"\b(?:not|no|isn'?t|can'?t|cannot|won'?t|unable|disconnected|fail\w*)\b", re.IGNORECASE)


def is_wallet_connected_ack(request: str) -> bool:
    """A short reply saying the wallet is now connected ("yes connected",
    "wallet is connected now", "done", "try again"). Only meaningful when the
    previous turn parked a swap for lack of a wallet, so the caller checks that
    first; a bare "yes" counts because that turn asked for exactly this."""
    text = request.strip()
    if _WALLET_ACK_BARE.match(text):
        return True
    return (
        len(text.split()) <= 4
        and bool(_WALLET_ACK_MARKER.search(text))
        and not _WALLET_ACK_NEGATION.search(text)
    )
