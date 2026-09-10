"""Deterministic intent safeguards for requests that require fresh data."""

import re


_CURRENT_INFORMATION = re.compile(
    r"\b(?:latest|breaking|news|current|currently|today|today's|tonight|"
    r"right now|now|recent|recently|live|up[- ]to[- ]date|as of|this week|"
    r"this month|this year)\b",
    re.IGNORECASE,
)
_OWN_WALLET = re.compile(
    r"\b(?:my|connected)\b.{0,24}\b(?:wallet|portfolio|balance|balances|holdings)\b",
    re.IGNORECASE,
)
_TRADE_ACTION = re.compile(
    r"\b(?:swap|buy|sell|exchange)\b|\bprepare\b.{0,30}\btrade\b",
    re.IGNORECASE,
)


def current_information_intent(request: str) -> str | None:
    """Force broad current-information questions through live research.

    Wallet reads and requested trade actions retain their specialized safety
    routes even when the user describes them as current or latest.
    """
    if not _CURRENT_INFORMATION.search(request):
        return None
    if _OWN_WALLET.search(request) or _TRADE_ACTION.search(request):
        return None
    return "research"
