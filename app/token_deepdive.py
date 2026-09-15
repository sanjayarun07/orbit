"""Multi-chain token deep-dive -- the "analysis" lens.

A faithful reverse-engineering of Minara's `analysis` skill (its 10-dimension
crypto-token-analysis framework + snapshot evidence bundle), redesigned for Orbit:

- Evidence is composed DETERMINISTICALLY through our ProviderRouter (one router
  call per dimension, run concurrently), not via an LLM tool-search loop -- so a
  deep-dive is fast and its tool selection is the same audited routing every other
  answer uses.
- Each dimension carries a self-describing coverage envelope (status / source /
  as-of), mirroring the snapshot contract's "ok:true means collection finished,
  not that all evidence is available". The synthesis lens is told exactly what is
  and isn't covered so it never fabricates a missing dimension.
- The anti-hallucination rules the Minara lens embeds in prose are encoded here as
  one reusable, testable constant (`ANALYSIS_RULES`) and fed to the synthesis
  signature -- and the whole answer then passes through the step-7 validator,
  which Minara has no equivalent of.

Scope: multi-chain Web3 tokens (Solana + EVM). The bundle only claims dimensions
we can actually source; genuine gaps (unlocks on chains DefiLlama doesn't cover,
exchange net-flows, on-chain quant like MVRV) are surfaced as `unavailable` with a
reason, never silently dropped.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from app.provider_registry import get_provider_router

# DefiLlama emissions (free): a protocol-slug list + per-slug unlock schedule whose
# metadata.token is "chain:address" -- so a symbol-guessed slug is VERIFIED by the
# resolved address before its unlocks are trusted. Unlocks are the most
# deterministic near-term headwind, so this closes the highest-value evidence gap.
_UNLOCK_LIST_URL = "https://defillama-datasets.llama.fi/emissionsProtocolsList"
_UNLOCK_SLUG_URL = "https://defillama-datasets.llama.fi/emissions/{slug}"
_unlock_list_cache: tuple[float, list[str]] | None = None
_unlock_cache: dict[str, tuple[float, str | None]] = {}
_UNLOCK_TTL = 3600.0

# The reverse-engineered "do not confuse X with Y" discipline from Minara's
# crypto-token-analysis lens, kept as data so it is reused verbatim by the
# synthesis signature and covered by a test. Every line is a real confusion class
# that silently corrupts a token verdict.
ANALYSIS_RULES = (
    "Grounding rules — apply strictly, never violate:\n"
    "- Use only figures present in the evidence bundle below; never reconstruct a "
    "current metric, price, or event from prior knowledge. State a gap; do not fill it.\n"
    "- Holder lists include pools, exchanges, and treasury/program addresses — they "
    "do NOT describe beneficial owners. Top-10 concentration above ~30% is elevated "
    "risk, but read known-entity wallets in that context.\n"
    "- A null or absent mint/freeze authority is NOT proof of no mint/freeze power; "
    "absent safety signals are UNKNOWN, not 'safe'. Never assert no owner, no tax, "
    "no blacklist, immutable code, or audited safety without evidence in this bundle.\n"
    "- Global/market cap is not contract supply or FDV; do not conflate them. Do not "
    "sum global and DEX volume as a cross-market total.\n"
    "- Pool/token-side reserves are not executable depth at a chosen slippage; a "
    "cross-source price difference is not an executable spread; a stale or "
    "unknown-time price is an observation, not a current executable quote.\n"
    "- Token unlocks are the most deterministic near-term headwind — weight upcoming "
    "unlocks heavily when present; preserve missing unlock amounts as unknown.\n"
    "- For a native L1 coin, contract-safety screening is non-applicable; do not "
    "substitute a wrapped token's data.\n"
    "- Repeated trades alone do not establish bots or wash trading; an address label "
    "establishes neither beneficial ownership nor a vesting lock."
)

# The 10-dimension framework, as the evidence dimensions we compose. Each maps to a
# router query over a capability set; the label + gap_reason are shown to the user.
_MARKET = ("market_data", "token_discovery")
_SECURITY = ("token_security", "token_discovery")
_HOLDERS = ("token_discovery", "token_security")


@dataclass
class DimensionEvidence:
    name: str          # canonical dimension id (e.g. "identity_safety")
    label: str         # human label for the coverage envelope
    status: str        # "available" | "unavailable"
    detail: str        # the tool output, or a short reason when unavailable
    source: str | None = None   # the tool/provider that produced it
    as_of: str | None = None


@dataclass
class TokenEvidenceBundle:
    address: str
    chain: str
    dimensions: list[DimensionEvidence] = field(default_factory=list)

    @property
    def coverage(self) -> tuple[int, int]:
        available = sum(1 for d in self.dimensions if d.status == "available")
        return available, len(self.dimensions)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _http_json(url: str, timeout: float = 20.0):
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers={"User-Agent": "Orbit-Web3-Copilot/0.2"})
        resp.raise_for_status()
        return resp.json()


def _unlock_slug_list() -> list[str]:
    global _unlock_list_cache
    if _unlock_list_cache and time.monotonic() - _unlock_list_cache[0] < _UNLOCK_TTL:
        return _unlock_list_cache[1]
    try:
        data = _http_json(_UNLOCK_LIST_URL)
        slugs = [str(s) for s in data] if isinstance(data, list) else []
    except Exception:
        slugs = []
    _unlock_list_cache = (time.monotonic(), slugs)
    return slugs


def token_unlocks(symbol: str | None, address: str, chain: str) -> str | None:
    """Next upcoming token-unlock event from DefiLlama emissions, or None when the
    token isn't tracked (most memecoins) or can't be verified. Matches a slug from
    the symbol/name, then VERIFIES `metadata.token` ends with the resolved address
    so a wrong slug guess can never attach another token's unlocks."""
    if not symbol or not address:
        return None
    key = f"{chain}:{address.lower()}"
    cached = _unlock_cache.get(key)
    if cached and time.monotonic() - cached[0] < _UNLOCK_TTL:
        return cached[1]
    slugs = _unlock_slug_list()
    slug_set = set(slugs)
    sym = symbol.strip().lstrip("$").lower()
    if len(sym) < 3:
        return None  # 2-char tickers are too ambiguous to slug-match safely
    # Exact matches first, then a couple of prefix matches (arb -> arbitrum). Every
    # candidate is address-verified below, so a loose prefix can't mis-attribute.
    candidates = [c for c in (sym, sym.replace(" ", "-")) if c in slug_set]
    candidates += [s for s in slugs if s.startswith(sym) and s not in candidates][:2]
    result: str | None = None
    for slug in dict.fromkeys(candidates):
        try:
            data = _http_json(_UNLOCK_SLUG_URL.format(slug=slug))
        except Exception:
            continue
        meta = (data.get("metadata") or {}) if isinstance(data, dict) else {}
        token_ref = str(meta.get("token") or "").lower()
        if not token_ref.endswith(address.lower()):
            continue  # slug matched a different token of the same ticker -- reject
        now = time.time()
        upcoming = sorted(
            (e for e in (meta.get("events") or []) if isinstance(e, dict) and (e.get("timestamp") or 0) > now),
            key=lambda e: e["timestamp"],
        )
        if not upcoming:
            result = f"**Provider**: DefiLlama emissions · No upcoming unlock events scheduled for {symbol.upper()}."
            break
        lines = [f"**Provider**: DefiLlama emissions · **Token**: {symbol.upper()} ({slug})", "", "Next unlocks:"]
        for e in upcoming[:3]:
            when = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
            toks = (e.get("noOfTokens") or [None])[0]
            amount = f"{float(toks):,.0f} tokens" if isinstance(toks, (int, float)) else "amount unknown (preserve as unknown)"
            lines.append(f"- {when} · {amount} · {e.get('unlockType', '?')} · {e.get('category', '')}".rstrip(" ·"))
        result = "\n".join(lines)
        break
    _unlock_cache[key] = (time.monotonic(), result)
    return result


async def _route(query: str, capabilities: tuple[str, ...], chains: tuple[str, ...]):
    router = get_provider_router()
    try:
        if len(capabilities) == 1:
            return await asyncio.to_thread(router.try_route, query, capabilities[0], chains)
        return await asyncio.to_thread(router.try_route_across, query, capabilities, chains)
    except Exception:
        return None


async def build_token_evidence(address: str, chain: str, symbol: str | None = None) -> TokenEvidenceBundle:
    """Compose the deep-dive evidence bundle for a resolved (address, chain).

    Runs one router call per dimension concurrently; each dimension is marked
    available (with its source) or unavailable (with a reason). Never raises -- a
    dead source degrades that one dimension, not the bundle."""
    ch = (chain,) if chain else ()
    # (dimension id, label, query, capabilities, chains) -- the composable dims.
    specs = [
        ("identity_safety", "Identity & contract safety",
         f"token security, mint and freeze authority, honeypot and identity for {address} on {chain}", _SECURITY, ch),
        ("market", "Market & liquidity",
         f"price, market cap, FDV, 24h volume and liquidity for {address} on {chain}", _MARKET, ch),
        ("holders", "Holder structure",
         f"top holders and concentration for {address} on {chain}", _HOLDERS, ch),
        ("dex_pairs", "DEX pairs & venues",
         f"dex trading pairs and liquidity depth for {address} on {chain}", ("market_data",), ch),
        ("sentiment", "Market sentiment (global)",
         "crypto fear and greed sentiment index right now", ("market_sentiment",), ()),
    ]
    results = await asyncio.gather(*[_route(q, caps, chs) for _, _, q, caps, chs in specs])

    dims: list[DimensionEvidence] = []
    for (name, label, _q, _caps, _chs), result in zip(specs, results):
        if result is not None and getattr(result, "output", "").strip():
            dims.append(DimensionEvidence(name, label, "available", result.output, result.tool, _now()))
        else:
            dims.append(DimensionEvidence(
                name, label, "unavailable",
                "No usable data was returned by any provider for this dimension.", None, None))

    # Token unlocks -- the most deterministic near-term headwind. Real, free, and
    # verified-by-address via DefiLlama emissions; unavailable when the token isn't
    # tracked (most memecoins) rather than silently omitted.
    unlocks = await asyncio.to_thread(token_unlocks, symbol, address, chain)
    if unlocks:
        dims.append(DimensionEvidence("unlocks", "Token unlocks / emissions", "available", unlocks, "defillama_emissions", _now()))
    else:
        dims.append(DimensionEvidence(
            "unlocks", "Token unlocks / emissions", "unavailable",
            "Not tracked by DefiLlama emissions (typical for memecoins / no vesting schedule)."))

    # Dimensions that need paid data (no free source) -- disclosed as explicit gaps.
    for name, label, reason in (
        ("exchange_flows", "7-day exchange net-flow",
         "Not sourced (CEX in/out-flow accumulation-vs-distribution signal needs a paid provider)."),
        ("onchain_quant", "On-chain valuation (MVRV / cohorts)",
         "Not sourced (Glassnode-class cycle metrics need a paid provider)."),
    ):
        dims.append(DimensionEvidence(name, label, "unavailable", reason))

    return TokenEvidenceBundle(address=address, chain=chain, dimensions=dims)


_PRICE = re.compile(r"price[^$\n]{0,24}\$\s*([0-9][0-9,]*\.?[0-9]*)", re.IGNORECASE)


def extract_market_price(bundle: TokenEvidenceBundle) -> float | None:
    """Best-effort spot price from the market dimension, for the reflection loop's
    realized-move calculation. None when unparseable (then the outcome is 'unknown')."""
    for d in bundle.dimensions:
        if d.name == "market" and d.status == "available":
            m = _PRICE.search(d.detail)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    return None
    return None


def format_evidence_bundle(bundle: TokenEvidenceBundle) -> str:
    """Render the self-describing evidence bundle -- the coverage envelope first,
    then each available dimension's data. This is what the synthesis lens reads
    (and what a client can show as the 'evidence' behind the verdict)."""
    have, total = bundle.coverage
    lines = [
        f"# Evidence bundle — token `{bundle.address}` on {bundle.chain}",
        f"**Coverage**: {have}/{total} dimensions available · **As of** {_now()}",
        "",
        "| Dimension | Status | Source |",
        "|---|---|---|",
    ]
    for d in bundle.dimensions:
        src = d.source or ("—" if d.status == "available" else "not covered")
        lines.append(f"| {d.label} | {d.status} | {src} |")
    for d in bundle.dimensions:
        if d.status == "available":
            lines.extend(["", f"## {d.label}  ·  source: {d.source}", d.detail])
        else:
            lines.extend(["", f"## {d.label}  ·  UNAVAILABLE", f"_{d.detail}_"])
    return "\n".join(lines)
