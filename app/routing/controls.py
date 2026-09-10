"""High-precision execution control detection."""

import re

from .lexicon import EXECUTION_EXPLANATION, TRADE_CANCEL, TRADE_CONFIRM, TRADE_MODIFIER


def is_execution_explanation(request: str) -> bool:
    return bool(EXECUTION_EXPLANATION.search(request))


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
