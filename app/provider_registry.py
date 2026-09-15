"""Built-in API providers registered behind provider-neutral capabilities."""

from __future__ import annotations

import re
import json
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

from app.additional_providers import ADDITIONAL_PROVIDERS
from app.market_providers import MARKET_PROVIDERS
from app.perplexity_tools import (
    perplexity_available,
    perplexity_fetch_url,
    perplexity_finance_search,
    perplexity_people_search,
    perplexity_web_search,
)
from app.agent import token_identity, token_safety_warnings, token_top_holders_rpc
from app.geckoterminal_tools import geckoterminal_pools, _network as _gt_network
from app.provider_router import ProviderRouter, ProviderTool
from app.rootdata_provider import ROOTDATA_PROVIDERS
from app.settings import settings
from app.web_search import _openai_web_search


def _fetch_url(request: str) -> str:
    match = re.search(r"https?://[^\s<>]+", request)
    if not match:
        raise ValueError("No URL found")
    return perplexity_fetch_url(match.group(0).rstrip(".,)"))


_SOLANA_MINT = re.compile(r"(?<![A-Za-z0-9])([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])")
_SAFETY_WORDS = re.compile(
    r"\b(?:safe|safety|secure|security|rug|rug\s*pull|honeypot|scam|legit|audit|"
    r"risky?|freeze|freezable|mint\s+authority|sellable|sellability|warnings?)\b",
    re.IGNORECASE,
)


# "trending/new/hot/top TOKENS|PAIRS|POOLS" on a chain/launchpad -- the
# discovery word must modify the noun (adjacent, through discovery adjectives
# only) so "top holders of BONK token" is NOT caught.
_GT_DISCOVERY = re.compile(
    r"\b(?:trending|hot|hottest|top|latest|newest|newer|popular|biggest|new|fresh)"
    r"(?:\s+(?:trending|hot|new|latest|top|meme|active|popular|newest|biggest|fresh))*"
    r"\s+(?:tokens?|coins?|gems?|memecoins?|pairs?|pools?|launches?|listings?)\b"
    r"|\b(?:pump\.?\s?fun|pumpfun|letsbonk|bonk\.?fun|moonshot)\b",
    re.IGNORECASE,
)


# Well-known Solana token programs, so the answer names the program rather than
# only echoing an opaque address.
_TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Standard SPL Token",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022 (SPL Token Extensions)",
}


def _fmt_int(value: object) -> str | None:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: object) -> str | None:
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return None


_HOLDERS_WORDS = re.compile(r"\b(?:top\s+)?holders?\b", re.IGNORECASE)


def _fmt_amount(value: object) -> str:
    """Whole-number commas for large token amounts, 6 significant digits below."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.0f}" if abs(number) >= 1000 else f"{number:,.6g}"


def _solana_rpc_top_holders(request: str) -> str:
    """Largest token accounts for a Solana mint from the RPC node itself --
    the keyless holders answer that stays available when the paid providers
    (Bitquery) are out of credits or rate-limited."""
    match = _SOLANA_MINT.search(request)
    if not match:
        raise ValueError("A Solana mint address is required for a top-holders lookup")
    mint = match.group(1)
    data = token_top_holders_rpc(mint)
    holders = data.get("holders") or []
    if not holders:
        raise RuntimeError(f"The RPC node returned no token accounts for {mint}")
    lines = [
        "# Largest token accounts — Solana RPC",
        f"**Provider**: Solana RPC (`getTokenLargestAccounts`) · **Checked**: {_utc()} · **Mint**: `{mint}`",
        "",
    ]
    supply = data.get("total_supply")
    if supply:
        lines.append(f"Total supply: **{_fmt_amount(supply)}** · Top 10 accounts hold **{data.get('top10_supply_pct')}%** of supply")
        lines.append("")
    lines += ["| # | Owner wallet | Token account | Amount | % of supply |", "|---:|---|---|---:|---:|"]
    for index, holder in enumerate(holders[:20], start=1):
        owner = holder.get("owner")
        owner_cell = f"[{owner[:4]}…{owner[-4:]}](https://solscan.io/account/{owner})" if owner else "—"
        account = holder.get("token_account") or ""
        pct = holder.get("supply_pct")
        lines.append(
            f"| {index} | {owner_cell} | `{account[:4]}…{account[-4:]}` | {_fmt_amount(holder.get('amount', 0))} | "
            f"{f'{pct:.2f}%' if pct is not None else '—'} |"
        )
    lines += [
        "",
        "Rows are token accounts, not entities: a liquidity pool, exchange, or program can own several, "
        "and the owner wallet is the account's authority. Labels need an indexed provider (Bitquery/Nansen).",
    ]
    return "\n".join(lines)


def _solana_token_security(request: str) -> str:
    """Jupiter identity + Shield safety dossier for a Solana mint -- the
    token_security answer for Solana, where GoPlus/Honeypot (EVM-only) do not
    apply. Combines the Jupiter token-registry record (verified status, tags,
    organic score, holder count, audit, and the actual mint/freeze authority
    addresses) with Jupiter Shield's scam/freeze warnings, so the answer is a
    full identity + risk picture rather than two bare warning strings.
    """
    match = _SOLANA_MINT.search(request)
    if not match:
        raise ValueError("A Solana mint address is required for a token-safety check")
    mint = match.group(1)
    record = token_identity(mint) or {}
    shield = token_safety_warnings(mint) or {}
    warnings: list[str] = []
    for entries in (shield.get("warnings") or {}).values():
        for entry in entries or []:
            if isinstance(entry, dict):
                warnings.append(str(entry.get("message") or entry.get("type") or entry))
            else:
                warnings.append(str(entry))

    lines = [
        "# Token security & identity — Jupiter",
        f"**Provider**: Jupiter (token registry + Shield) · **Checked**: {_utc()} · **Mint**: `{mint}` · Solana",
        "",
    ]

    if record:
        name = record.get("name") or "?"
        symbol = record.get("symbol") or "?"
        program = record.get("tokenProgram") or ""
        program_label = _TOKEN_PROGRAMS.get(program, "Non-standard / custom program")
        verified = record.get("isVerified")
        tags = ", ".join(record.get("tags") or []) or "—"
        score = record.get("organicScore")
        score_label = record.get("organicScoreLabel")
        holders = _fmt_int(record.get("holderCount"))
        lines.extend([
            "## Identity",
            "| Field | Value |",
            "|---|---|",
            f"| Name / Symbol | {name} ({symbol}) |",
            f"| Token program | {program_label}{f' `{program}`' if program else ''} |",
            f"| Decimals | {record.get('decimals', '—')} |",
            f"| Jupiter verified | {'Yes' if verified else 'No'} |",
            f"| Jupiter tags | {tags} |",
            f"| Organic-activity score | {score}/100{f' ({score_label})' if score_label else ''} |"
            if score is not None else "| Organic-activity score | — |",
            f"| Holders | {holders or '—'} |",
            "",
        ])

    lines.append("## Safety")
    lines.append("| Signal | Finding |")
    lines.append("|---|---|")
    mint_authority = record.get("mintAuthority")
    freeze_authority = record.get("freezeAuthority")
    if record:
        lines.append(
            f"| Mint authority | Active — `{mint_authority}` (supply can be increased) |"
            if mint_authority else "| Mint authority | None (supply is fixed) |"
        )
        lines.append(
            f"| Freeze authority | Active — `{freeze_authority}` (token accounts can be frozen) |"
            if freeze_authority else "| Freeze authority | None (accounts cannot be frozen) |"
        )
    shield_finding = "; ".join(warnings[:6]) if warnings else "No known-malicious/scam flag returned"
    lines.append(f"| Jupiter Shield | {shield_finding} |")
    audit = record.get("audit") or {}
    top_pct = _fmt_pct(audit.get("topHoldersPercentage"))
    if top_pct:
        lines.append(f"| Top-holder concentration | ~{top_pct} (Jupiter audit) |")
    dev_pct = _fmt_pct(audit.get("devBalancePercentage"))
    if dev_pct is not None:
        lines.append(f"| Developer holdings | ~{dev_pct} across {audit.get('devMints', '?')} dev mint(s) |")
    lines.append("")

    # Latest external context (issuer confirmation, recent incidents/depeg news)
    # -- the current information on-chain data alone can't provide. Budget-gated,
    # cached, and failure-safe so it never breaks the deterministic dossier.
    if settings.token_security_web_context and perplexity_available():
        subject = f"{record.get('name')} ({record.get('symbol')})" if record else "the token"
        query = (
            f"Solana token {subject} with mint {mint}: who is the official issuer, is this the "
            "legitimate/native mint (not a bridged or lookalike token), and any security incidents, "
            "exploits, or depeg events in the last 12 months? Be concise."
        )
        try:
            context = perplexity_web_search(query).strip()
        except Exception:
            context = ""
        if context:
            lines.extend(["## Latest context (web)", context, ""])

    lines.extend([
        "**Verification rule:** confirm the FULL mint above, not just the ticker or logo — "
        "bridged or lookalike tokens (e.g. \"USDC.e\") share a symbol but are different, "
        "non-interchangeable mints.",
        "",
        "Jupiter Shield flags known-malicious, freezable, or scam mints; a clean result and a "
        "\"verified\" tag are positive signals, not a guarantee or an audit. An active mint or "
        "freeze authority is expected for an issuer-backed stablecoin but means supply and "
        "account access are centrally controlled — verify the issuer independently before trading.",
    ])
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_provider_router() -> ProviderRouter:
    router = ProviderRouter()
    for provider_type in MARKET_PROVIDERS:
        provider_type().register(router)
    for provider_type in ADDITIONAL_PROVIDERS:
        provider_type().register(router)
    for provider_type in ROOTDATA_PROVIDERS:
        provider_type().register(router)
    router.register(ProviderTool(
        "geckoterminal_pools", "geckoterminal", ("market_data", "token_discovery"), geckoterminal_pools,
        # Real per-chain trending/new pools (incl. Base, which DEX Screener's
        # curated feeds miss). Only fires with a resolvable chain/launchpad, so
        # it never wastes a call on a scopeless request; priority above the
        # DEX Screener discovery tools, which stay as failover.
        matches=lambda request: bool(_GT_DISCOVERY.search(request)) and _gt_network(request) is not None,
        keywords=("trending", "new", "pairs", "pools", "tokens", "gems", "hot", "top", "launch", "pump", "launchpad"),
        # Chains it covers, so it earns the same chain-fit bonus the DEX Screener
        # discovery tools do (otherwise chains=() would lose the tie on a named
        # chain); priority above them so it decisively wins trending/new
        # discovery even when a rival picks up an extra keyword hit.
        chains=("solana", "base", "ethereum", "arbitrum", "bsc", "polygon", "avalanche", "sui", "robinhood", "hyperliquid"),
        cache_ttl_seconds=45, quota_per_minute=30, priority=10,
        description="Real trending or newly-created pools on a specific chain or launchpad (GeckoTerminal): price, volume, liquidity, pool age",
    ))
    router.register(ProviderTool(
        # Keyless holders source for Solana; ranks below bitquery_token_top_holders
        # (priority 9, labelled owners) and takes over when that one is out.
        "solana_rpc_token_top_holders", "solana_rpc", ("token_discovery", "token_security"), _solana_rpc_top_holders,
        matches=lambda request: bool(_SOLANA_MINT.search(request)) and bool(_HOLDERS_WORDS.search(request)),
        keywords=("holders", "top holders", "largest accounts", "concentration"),
        chains=("solana",), cache_ttl_seconds=300, priority=7,
        description="Largest 20 token accounts for a Solana mint with owner wallet and share of supply (Solana RPC getTokenLargestAccounts)",
    ))
    router.register(ProviderTool(
        "solana_token_security", "jupiter", ("token_security",), _solana_token_security,
        matches=lambda request: bool(_SOLANA_MINT.search(request)) and bool(_SAFETY_WORDS.search(request)),
        keywords=("safety", "security", "rug", "honeypot", "scam", "safe", "audit"),
        chains=("solana",), cache_ttl_seconds=120, priority=10,
        description="Jupiter Shield safety/scam/freeze-authority check for a Solana token by mint address",
    ))
    router.register(ProviderTool(
        "perplexity_web_search", "perplexity", ("web_research", "equity_research", "finance_data", "project_intelligence", "vc_intelligence"), perplexity_web_search,
        enabled=perplexity_available, cost_usd=settings.perplexity_web_search_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=4,
    ))
    router.register(ProviderTool(
        "perplexity_fetch_url", "perplexity", ("url_fetch",), _fetch_url,
        enabled=perplexity_available, cost_usd=settings.perplexity_fetch_url_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=5,
    ))
    router.register(ProviderTool(
        "perplexity_people_search", "perplexity", ("people_intelligence",), perplexity_people_search,
        enabled=perplexity_available, cost_usd=settings.perplexity_people_search_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=5,
    ))
    router.register(ProviderTool(
        "perplexity_finance_search", "perplexity", ("finance_data", "equity_research"), perplexity_finance_search,
        enabled=perplexity_available, cost_usd=settings.perplexity_finance_search_cost_usd,
        quota_per_minute=settings.perplexity_requests_per_minute, priority=5,
    ))
    router.register(ProviderTool(
        "openai_web_search", "openai", ("web_research", "url_fetch", "people_intelligence", "finance_data"),
        _openai_web_search, enabled=lambda: bool(settings.openai_api_key),
        cost_usd=settings.openai_web_search_cost_usd,
        quota_per_minute=settings.openai_web_search_requests_per_minute, priority=1,
    ))
    path = Path(settings.provider_overrides_path)
    if path.exists():
        try:
            value = json.loads(path.read_text())
            if isinstance(value, dict):
                router.apply_overrides(value)
        except (OSError, json.JSONDecodeError):
            pass
    return router


def save_provider_overrides() -> None:
    path = Path(settings.provider_overrides_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(get_provider_router().overrides(), indent=2, sort_keys=True))
    temporary.replace(path)
