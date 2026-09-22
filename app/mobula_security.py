"""Meme-token due diligence: LP lock state, holder concentration, and the
contract switches that let an issuer rug.

User decision (2026-09-18): "use this for meme token complete in depth
analysis", noting fomo.family runs its meme data on the same infrastructure.
Mobula's /token/security answers what our existing security tools could not
for a memecoin: for each of the token's busiest pools, how much of the LP is
burned, locked in a known locker, held by an unidentified contract, or
sitting in a plain wallet that can pull it. It works on Solana and on every
EVM chain, so one card covers a Raydium launch and a Uniswap one alike.

The Solana identity dossier (Jupiter verification, organic score) and this
card answer different questions and both run in a deep dive.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app.provider_router import NoData, ProviderRouter, ProviderTool
from app.settings import settings
from app.tool_catalog import TOOL_SPECS

logger = logging.getLogger(__name__)

from app import mobula_client
_EVM = re.compile(r"\b(0x[0-9a-fA-F]{40})\b")
_SOL = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
_CHAIN = re.compile(r"\bon\s+([A-Za-z][A-Za-z ]{2,20}?)\b(?:\s|$|,|\.)", re.I)
_CHAIN_NAMES = {
    "solana": "solana", "ethereum": "ethereum", "eth": "ethereum", "base": "base", "arbitrum": "arbitrum",
    "bsc": "bnb smart chain (bep20)", "bnb": "bnb smart chain (bep20)", "bnb chain": "bnb smart chain (bep20)",
    "polygon": "polygon", "avalanche": "avalanche c-chain", "optimism": "optimism", "hyperevm": "hyperevm",
    "robinhood": "Robinhood Chain", "robinhood chain": "Robinhood Chain",
}
# Percent of LP in plain wallets above which the pool can be pulled at will.
_RUG_RISK_PCT = 50.0


def enabled() -> bool:
    return bool(settings.mobula_api_key)


def _subject(request: str) -> tuple[str, str] | None:
    """(address, chain) for a token lookup, or None. A base58 address is
    Solana. An EVM address is valid on every EVM chain, so without a named
    chain there is no subject: the research node asks which chain rather
    than a tool answering for Ethereum by default (review, 2026-09-20)."""
    text = request or ""
    evm, sol = _EVM.search(text), _SOL.search(text)
    named = _CHAIN.search(text)
    chain = _CHAIN_NAMES.get((named.group(1) if named else "").strip().lower()) if named else None
    if evm:
        return (evm.group(1), chain) if chain else None
    if sol:
        return sol.group(1), chain or "solana"
    return None


def evm_without_chain(request: str) -> bool:
    """An EVM contract in the request and no chain named beside it."""
    text = request or ""
    named = _CHAIN.search(text)
    return bool(_EVM.search(text)) and not (named and _CHAIN_NAMES.get(named.group(1).strip().lower()))


# The words that make a question about whether a token is safe to hold or
# can be pulled. Without one of these the card is not the answer: "what is
# 0x… on base" is a market question, not a rug check.
SECURITY_ASK = re.compile(
    r"\b(?:rug(?:pull|ged|s)?|rug\s*check|honeypot|scam|safe(?:ty)?|secur\w+|risk\w*|legit|liquidity|lp\b|locked?|lock(?:ing|s)?|burn(?:ed|t)?|"
    r"mint(?:able|\s+authority)?|freez\w+|pausab\w+|renounc\w+|fees?\b|tax(?:es)?\b|holders?|concentrat\w+|deployer|dev\b|bundle[ds]?|snip\w+|"
    r"deep\s*dive|due\s+diligence|audit\w*|can\s+i\s+sell|warnings?)\b",
    re.IGNORECASE,
)


def matches(request: str) -> bool:
    """A token address with a question about its safety or its liquidity."""
    return _subject(request) is not None and bool(SECURITY_ASK.search(request or ""))


def _pct(value, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _flag(value, yes: str, no: str, unknown: str = "not reported") -> str:
    if value is True:
        return yes
    if value is False:
        return no
    return unknown


def _pool_verdict(pool: dict) -> str:
    burned = float(pool.get("burnedPercentage") or 0)
    locked = float(pool.get("lockedPercentage") or 0)
    unlocked = float(pool.get("unlockedPercentage") or 0)
    contract = float(pool.get("contractPercentage") or 0)
    if burned + locked >= 90:
        return "burned or locked" if burned >= locked else "locked"
    if unlocked >= _RUG_RISK_PCT:
        return "**pullable**"
    if contract >= _RUG_RISK_PCT:
        return "held by an unidentified contract"
    return "mixed"


def _lockers(pool: dict) -> str:
    names = {str(h.get("protocol")).strip() for h in (pool.get("topHolders") or [])
             if isinstance(h, dict) and h.get("type") == "locked" and h.get("protocol")}
    return ", ".join(sorted(names)[:3])


def token_security(request: str) -> str:
    """The rug-risk card: LP lock state per pool, holder concentration, fees
    and the contract switches, for a memecoin on any supported chain."""
    subject = _subject(request)
    if subject is None:
        raise ValueError("No token address found in the request")
    address, chain = subject
    data = mobula_client.get(2, "/token/security", {"blockchain": chain, "address": address})
    if not isinstance(data, dict) or not data:
        raise NoData("Mobula has no security analysis for this token")

    lines = [
        "# Token security & liquidity",
        f"**Provider**: Mobula · **Contract**: `{address}` · **Chain**: {chain} · **Checked**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    pools = [p for p in (data.get("liquidityAnalysis") or []) if isinstance(p, dict)]
    if pools:
        lines += ["## Liquidity: can it be pulled?", "| Pool | DEX | Burned | Locked | Contract | Unlocked | Read |",
                  "|---|---|---:|---:|---:|---:|---|"]
        for pool in pools[:3]:
            locker = _lockers(pool)
            verdict = _pool_verdict(pool) + (f" ({locker})" if locker else "")
            lines.append(f"| `{str(pool.get('poolAddress') or '')[:10]}…` | {pool.get('poolType') or '—'} | "
                         f"{_pct(pool.get('burnedPercentage'))} | {_pct(pool.get('lockedPercentage'))} | "
                         f"{_pct(pool.get('contractPercentage'))} | {_pct(pool.get('unlockedPercentage'))} | {verdict} |")
        worst = max((float(p.get("unlockedPercentage") or 0) for p in pools[:3]), default=0.0)
        lines += ["", f"Burned LP can never be withdrawn; locked LP is held by a locker contract until it expires; "
                      f"unlocked LP sits in wallets that can remove it at any time. Worst pool here is "
                      f"{_pct(worst)} unlocked."]
    else:
        lines.append("No liquidity-pool analysis was returned for this token.")

    lines += ["", "## Holders", f"- **Top 10**: {_pct(data.get('top10HoldingsPercentage'))} of supply (Mobula's security profile, with its own exclusions; the holders table counts every position) · "
              f"**Top 50**: {_pct(data.get('top50HoldingsPercentage'))} · **Top 100**: {_pct(data.get('top100HoldingsPercentage'))}"]
    if data.get("burnedHoldingsPercentage") is not None:
        lines.append(f"- **Burned supply**: {_pct(data.get('burnedHoldingsPercentage'))} · "
                     f"**Held by contracts**: {_pct(data.get('contractHoldingsPercentage'))}")
    if data.get("proTraderVolume24hPercentage") is not None:
        lines.append(f"- **Pro-trader share of 24h volume**: {_pct(data.get('proTraderVolume24hPercentage'))}")

    lines += ["", "## Contract switches",
              f"- **Honeypot**: {_flag(data.get('isHoneypot'), '**yes — you may not be able to sell**', 'no')}",
              f"- **Mintable**: {_flag(data.get('isMintable'), '**yes — supply can grow**', 'no')} · "
              f"**Freezable**: {_flag(data.get('isFreezable'), '**yes**', 'no')} · "
              f"**Transfers pausable**: {_flag(data.get('transferPausable'), '**yes**', 'no')}",
              f"- **Ownership renounced**: {_flag(data.get('renounced'), 'yes', '**no — the owner keeps control**')} · "
              f"**Balances mutable**: {_flag(data.get('balanceMutable'), '**yes**', 'no')}"]
    fees = [f"buy {_pct(data.get('buyFeePercentage'))}", f"sell {_pct(data.get('sellFeePercentage'))}",
            f"transfer {_pct(data.get('transferFeePercentage'))}"]
    lines.append(f"- **Fees**: {', '.join(fees)}")
    if data.get("isLaunchpadToken") is not None:
        lines.append(f"- **Launchpad token**: {_flag(data.get('isLaunchpadToken'), 'yes', 'no')}")

    copies = _logo_reuses(address, chain)
    if copies:
        count, samples = copies
        lines += ["", "## Copycats",
                  f"**{count} other token(s) reuse this exact logo.** " + ("Highest-volume lookalikes: " + "; ".join(samples) + "." if samples else "")
                  + " Verify the contract, not the picture."]
    lines += ["", "Source: [Mobula token security](https://docs.mobula.io/cookbooks/token-security-liquidity-analysis)",
              "Lock state is read from the pool's LP-token holders; a lock that expires, or a locker this endpoint does not "
              "recognise, is not a guarantee. This is a risk read, not an audit."]
    return "\n".join(lines)


_PULSE_CHAIN = {"solana": "solana:solana", "ethereum": "evm:1", "base": "evm:8453", "bnb smart chain (bep20)": "evm:56", "arbitrum": "evm:42161",
                "polygon": "evm:137", "avalanche c-chain": "evm:43114", "optimism": "evm:10", "hyperevm": "evm:999", "Robinhood Chain": "evm:4663"}


def _logo_reuses(address: str, chain: str) -> tuple[int, list[str]] | None:
    """How many other tokens reuse this exact logo, and the busiest of them:
    byte-identical images only, so a copycat with a re-encoded picture is missed."""
    chain_id = _PULSE_CHAIN.get(chain)
    if not chain_id:
        return None
    try:
        data = mobula_client.get(2, "/token/logo-reuses", {"address": address, "chainId": chain_id}) or {}
    except Exception:
        logger.info("logo reuse lookup failed for %s", address, exc_info=True)
        return None
    count = int(data.get("count") or 0)
    tokens = sorted((t for t in (data.get("tokens") or []) if isinstance(t, dict)), key=lambda t: float(t.get("volume24hUSD") or 0), reverse=True)
    samples = [f"{t.get('symbol') or '?'} on {t.get('chainId') or '?'} (`{str(t.get('address') or '')[:10]}…`)" for t in tokens[:3]]
    return (count, samples) if count else None


class MobulaSecurityProvider:
    name = "mobula"

    def enabled(self) -> bool:
        return enabled()

    def register(self, router: ProviderRouter) -> None:
        router.register(ProviderTool(
            "mobula_token_security", self.name, ("token_security",), token_security,
            enabled=self.enabled, matches=matches,
            # Deliberately narrow: the generic security words belong to the
            # chain-native tools (Jupiter Shield, GoPlus, Honeypot.is), which
            # answer "is this token safe" first. These are the words only this
            # card answers, so it joins the plan without displacing them.
            keywords=("lp lock", "liquidity lock", "locked liquidity", "burned liquidity", "rug pull"),
            # Naming the chains it really covers earns the same chain-fit score
            # the per-chain tools get, so an explicit LP-lock question reaches
            # this card rather than a generic pair lookup.
            chains=("solana", "ethereum", "base", "arbitrum", "bsc", "bnb", "polygon", "avalanche", "optimism", "hyperevm", "robinhood"),
            cost_usd=settings.mobula_request_cost_usd, quota_per_minute=settings.mobula_requests_per_minute,
            # Below the chain-native security tools (Jupiter Shield on Solana,
            # GoPlus and Honeypot.is on EVM): they answer "is this token safe"
            # first, and this joins the multi-tool plan beside them. The deep
            # dive calls it by name, so its rank never gates a meme analysis.
            cache_ttl_seconds=120, priority=4, spec=TOOL_SPECS.get("mobula_token_security"),
            description="Whether a token's liquidity can be pulled: burned, locked, contract-held and unlocked LP per pool, with holder concentration, fees and the mint/freeze/pause switches, on Solana and every EVM chain",
        ))
