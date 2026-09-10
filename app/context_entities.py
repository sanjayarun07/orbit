"""Deterministic entity carry-over for safe conversational follow-ups."""

from __future__ import annotations

from dataclasses import dataclass
import re
from solders.pubkey import Pubkey


_EVM_ADDRESS = r"0x[0-9a-fA-F]{40}"
_SOLANA_ADDRESS = r"(?<![A-Za-z0-9])[1-9A-HJ-NP-Za-km-z]{32,44}(?![A-Za-z0-9])"
_ANY_ADDRESS = re.compile(rf"{_EVM_ADDRESS}|{_SOLANA_ADDRESS}")
_LABELED_TOKEN_ADDRESS = re.compile(
    rf"\b(?:solana\s+)?(?:token\s+)?(?:mint|contract)(?:\s+address)?\s*(?::|is)?\s*({_EVM_ADDRESS}|{_SOLANA_ADDRESS})",
    re.IGNORECASE,
)
_TOKEN_WORD = re.compile(r"\b(?:token|coin|contract|mint|memecoin|meme coin|erc-?20)\b", re.IGNORECASE)
_TOKEN_FOLLOWUP = re.compile(
    r"\b(?:this|that|the)\s+(?:token|coin|contract)\b"
    r"|\b(?:its|their)\s+(?:holders?|liquidity|volume|price|trades?|safety|risk)\b",
    re.IGNORECASE,
)
_WALLET_FOLLOWUP = re.compile(
    r"\b(?:this|that|the)\s+(?:wallet|address|portfolio)\b"
    r"|\b(?:its|their)\s+(?:transactions?|balances?|holdings?|counterparties|pnl|leverage|positions?)\b",
    re.IGNORECASE,
)
_WALLET_WORD = re.compile(
    r"\b(?:wallet|address|portfolio|holdings?|balances?|transactions?|counterparties|pnl|leverage|positions?)\b",
    re.IGNORECASE,
)
_PROFILER_ADDRESS = re.compile(rf"profiler\?address=({_EVM_ADDRESS}|{_SOLANA_ADDRESS})", re.IGNORECASE)
_CHAINS = re.compile(
    r"\b(solana|ethereum|base|arbitrum|optimism|polygon|bnb|bsc|avalanche|sui|tron|robinhood(?:\s+chain)?)\b",
    re.IGNORECASE,
)
_NAMED_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])\$?([A-Za-z][A-Za-z0-9._-]{1,15})\s+"
    r"(?:token|coin|memecoin|meme\s+coin)\b",
    re.IGNORECASE,
)
_NAMED_TRADE_TARGET = re.compile(
    r"\b(?:buy|swap(?:\s+[^\n]{0,40}?\s+to)?|sell|trade|exchange)\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TokenReference:
    address: str
    chain: str | None


@dataclass(frozen=True)
class WalletReference:
    address: str
    chain: str | None


def _plain(text: str) -> str:
    return text.replace("`", "").replace("*", "")


def _valid_address(value: str) -> bool:
    if value.startswith("0x"):
        return bool(re.fullmatch(_EVM_ADDRESS, value))
    try:
        Pubkey.from_string(value)
        return True
    except ValueError:
        return False


def _addresses(text: str) -> list[re.Match]:
    return [match for match in _ANY_ADDRESS.finditer(text) if _valid_address(match.group(0))]


def _chain_for(address: str, text: str) -> str | None:
    if not address.startswith("0x"):
        return "solana"
    matches = list(_CHAINS.finditer(text))
    if not matches:
        return None
    chain = matches[-1].group(1).lower()
    if chain == "bsc":
        return "bnb"
    if chain.startswith("robinhood"):
        return "robinhood"
    return chain


def extract_token_reference(*texts: str) -> TokenReference | None:
    """Find an explicitly labelled or token-qualified address, newest text first."""
    for raw in texts:
        if not raw:
            continue
        text = _plain(raw)
        text = re.sub(r"https?://[^\s)<>]+", "", text)
        labeled = [
            match
            for match in _LABELED_TOKEN_ADDRESS.finditer(text)
            if _valid_address(match.group(1))
        ]
        if labeled:
            address = labeled[-1].group(1)
            return TokenReference(address, _chain_for(address, text))
        if _TOKEN_WORD.search(text):
            addresses = _addresses(text)
            if addresses:
                address = addresses[-1].group(0)
                return TokenReference(address, _chain_for(address, text))
    return None


def extract_wallet_reference(*texts: str) -> WalletReference | None:
    """Find the latest wallet address without mistaking a linked token contract for it."""
    context = "\n".join(_plain(raw) for raw in texts if raw)
    for raw in texts:
        if not raw:
            continue
        text = _plain(raw)

        # Nansen profiler links identify their address as a wallet even when the
        # same answer also contains token contract links.
        profiler = list(_PROFILER_ADDRESS.finditer(text))
        if profiler:
            address = profiler[-1].group(1)
            return WalletReference(address, _chain_for(address, context))

        user_lines = re.findall(r"(?mi)^user:\s*(.+)$", text)
        candidates = reversed(user_lines) if user_lines else [text]
        for candidate in candidates:
            addresses = _addresses(candidate)
            if not addresses:
                continue
            # A bare address in a user turn is a wallet candidate. An address
            # described as a token/contract/mint is deliberately excluded.
            stripped = candidate.strip()
            bare_address = bool(re.fullmatch(rf"(?:this address\s+)?({_EVM_ADDRESS}|{_SOLANA_ADDRESS})", stripped, re.IGNORECASE))
            if not _TOKEN_WORD.search(candidate) and (bare_address or user_lines or _WALLET_WORD.search(candidate)):
                address = addresses[-1].group(0)
                return WalletReference(address, _chain_for(address, context))
    return None


def resolve_contextual_request(
    request: str, conversation_history: str, session_context: dict | None = None
) -> str:
    """Attach prior token or wallet identity to an unambiguous follow-up."""
    request_addresses = _addresses(request)
    if not conversation_history or request_addresses:
        if not session_context or request_addresses:
            return request
    focus = (session_context or {}).get("focus") or {}
    # A named execution follow-up may omit the chain and mint because the
    # immediately preceding token research already established them. Bind the
    # subject only when its name matches the canonical focus; never infer from
    # an unrelated historical token.
    trade_target = _NAMED_TRADE_TARGET.search(request)
    focus_label = str(focus.get("label") or "")
    named_research = re.search(r"\b(?:about|analy[sz]e|research)\s+\$?([A-Za-z][A-Za-z0-9._-]{1,15})\b", request, re.I)
    if named_research and focus.get("kind") == "token" and named_research.group(1).casefold() == focus_label.casefold():
        address = f" mint {focus['address']}" if focus.get("address") else ""
        chain = f" on {focus['chain']}" if focus.get("chain") else ""
        return f"{request}\nResolved subject: token {focus_label}{address}{chain}."
    if (
        trade_target
        and focus.get("kind") == "token"
        and focus_label
        and trade_target.group(1).lower() == focus_label.lower()
        and focus.get("chain")
    ):
        address = f" with mint {focus['address']}" if focus.get("address") else ""
        return (
            f"{request}\nResolved from canonical session context: "
            f"token {focus_label}{address} on {focus['chain']}."
        )
    if _TOKEN_FOLLOWUP.search(request):
        if focus.get("kind") == "token" and focus.get("address"):
            chain = f" on {focus['chain']}" if focus.get("chain") else ""
            return f"{request}\nResolved from canonical session context: token {focus['address']}{chain}."
        reference = extract_token_reference(conversation_history)
        if reference is not None:
            chain = f" on {reference.chain}" if reference.chain else ""
            return f"{request}\nResolved from conversation context: token {reference.address}{chain}."
    if _WALLET_FOLLOWUP.search(request):
        wallet = (session_context or {}).get("connected_wallet") or {}
        if focus.get("kind") == "wallet" and focus.get("address"):
            wallet = focus
        if wallet.get("address"):
            chain = f" on {wallet['chain']}" if wallet.get("chain") else ""
            return f"{request}\nResolved from canonical session context: wallet {wallet['address']}{chain}."
        reference = extract_wallet_reference(conversation_history)
        if reference is not None:
            chain = f" on {reference.chain}" if reference.chain else ""
            return f"{request}\nResolved from conversation context: wallet {reference.address}{chain}."
    return request
