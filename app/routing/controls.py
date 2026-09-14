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
