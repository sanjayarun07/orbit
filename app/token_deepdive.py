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
from app.provider_router import NoData
from app.integrations import tradingview
from app import holder_snapshots, mobula_meme, mobula_security, polymarket_odds, sentiment_analyst
from app.signals import Signal, Subject, SubjectSkip

# DefiLlama emissions (free): a protocol-slug list + per-slug unlock schedule whose
# metadata.token is "chain:address" -- so a symbol-guessed slug is VERIFIED by the
# resolved address before its unlocks are trusted. Unlocks are the most
# deterministic near-term headwind, so this closes the highest-value evidence gap.
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
    # Typed analyst votes formed while composing the evidence (a quant read
    # such as the bundle check). The lens's own vote is added by the caller.
    signals: list[Signal] = field(default_factory=list)

    @property
    def coverage(self) -> tuple[int, int]:
        available = sum(1 for d in self.dimensions if d.status == "available")
        return available, len(self.dimensions)


def coverage_rows(bundle: TokenEvidenceBundle) -> list[dict]:
    """The coverage envelope as plain rows for a receipt."""
    return [{"name": d.name, "status": d.status, "source": d.source} for d in bundle.dimensions]


def evidence_skips(bundle: TokenEvidenceBundle) -> list[SubjectSkip]:
    """Every dimension the verdict could not see, with its reason -- recorded,
    never dropped, because a missing dimension bounds how far the verdict can
    be trusted."""
    return [SubjectSkip(subject=d.label, reason=d.detail) for d in bundle.dimensions if d.status != "available"]


def bundle_signals(bundle: TokenEvidenceBundle) -> list[Signal]:
    return list(getattr(bundle, "signals", None) or [])


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# The DefiLlama emissions client lives in app/token_unlocks.py (the router
# registers that tool and this module imports the router).
from app.token_unlocks import _UNLOCK_SLUG_URL, _http_json, _unlock_slug_list  # noqa: E402


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
    subject = Subject(kind="token", id=address, chain=chain, symbol=symbol)
    signals: list[Signal] = []
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

    # Whether the liquidity can be pulled -- the question that decides a
    # memecoin (user, 2026-09-18: "use this for meme token complete in depth
    # analysis"). Called by name, not through the router, so it never competes
    # with the identity dossier above: they answer different questions.
    if mobula_security.enabled():
        try:
            locks = await asyncio.to_thread(mobula_security.token_security, f"{address} on {chain}")
        except Exception:
            locks = None
        if locks:
            dims.append(DimensionEvidence("liquidity_locks", "Liquidity locks & rug risk", "available", locks, "mobula_token_security", _now()))
        else:
            dims.append(DimensionEvidence("liquidity_locks", "Liquidity locks & rug risk", "unavailable",
                                          "Mobula returned no liquidity analysis for this contract."))

    # What is actually trading right now: the last indexed swaps, buy/sell
    # split and sizes. For a memecoin this is half the thesis.
    if mobula_meme.enabled():
        try:
            trades = await asyncio.to_thread(mobula_meme.token_trades, f"latest trades {address} on {chain}")
        except Exception:
            trades = None
        dims.append(DimensionEvidence("latest_trades", "Latest trades", "available", trades, "mobula_token_trades", _now())
                    if trades else
                    DimensionEvidence("latest_trades", "Latest trades", "unavailable", "Mobula returned no indexed swaps for this contract."))

    # Who got in first and whether they are still in: the sniper / exit read.
    if mobula_meme.enabled():
        try:
            first = await asyncio.to_thread(mobula_meme.token_first_buyers, f"first buyers {address} on {chain}")
        except Exception:
            first = None
        dims.append(DimensionEvidence("first_buyers", "First buyers & snipers", "available", first, "mobula_token_first_buyers", _now())
                    if first else
                    DimensionEvidence("first_buyers", "First buyers & snipers", "unavailable", "Mobula returned no first-buyer data for this contract."))

    # Was the launch bundled: same-second first buyers sharing a funder. One
    # analysis feeds both the evidence card and a typed vote, so the two can
    # never disagree. No first buyers -> the dimension is unavailable AND the
    # vote abstains, with the same reason.
    if mobula_meme.enabled():
        try:
            analysis = await asyncio.to_thread(mobula_meme._bundle_analysis, address, chain)
        except NoData as exc:
            analysis, reason = None, str(exc)
        except Exception as exc:  # noqa: BLE001 - a dead source degrades one dimension
            analysis, reason = None, f"Bundle check did not complete ({type(exc).__name__})."
        if analysis is not None:
            dims.append(DimensionEvidence("bundle", "Bundle check (launch coordination)", "available",
                                          mobula_meme._render_bundle(analysis), "mobula_token_bundle", _now()))
            signals.append(mobula_meme.bundle_signal(subject, _now_iso(), analysis))
        else:
            dims.append(DimensionEvidence("bundle", "Bundle check (launch coordination)", "unavailable", reason))
            signals.append(Signal.abstain(mobula_meme.BUNDLE_MODEL, subject, _now_iso(), reason))

    # What X is saying, judged: a dimension for the lens and a vote on the
    # receipt (user decision 2026-09-21). Needs a symbol to search for.
    if symbol and sentiment_analyst.enabled():
        try:
            out = await sentiment_analyst.analyze(symbol, subject=subject)
        except Exception as exc:  # noqa: BLE001
            out = None
            dims.append(DimensionEvidence("x_sentiment", "X sentiment (crowd lean)", "unavailable", f"X sentiment did not complete ({type(exc).__name__})."))
            signals.append(Signal.abstain(sentiment_analyst.MODEL_NAME, subject, _now_iso(), f"analyst failed: {type(exc).__name__}"))
        if out is not None:
            if out.get("judgement") is not None:
                dims.append(DimensionEvidence("x_sentiment", "X sentiment (crowd lean)", "available", out["card"], "x_sentiment_analyst", _now()))
            else:
                dims.append(DimensionEvidence("x_sentiment", "X sentiment (crowd lean)", "unavailable", out.get("abstain_reason") or "no judgement"))
            signals.append(out["signal"])

    # Money-backed odds, when a market exists (majors and events; rarely memes).
    if symbol and polymarket_odds.enabled():
        try:
            markets = await asyncio.to_thread(polymarket_odds.markets_for, symbol)
            card = polymarket_odds.render_card(symbol, markets)
        except Exception:
            card = None
        if card:
            dims.append(DimensionEvidence("prediction_markets", "Prediction markets (Polymarket)", "available", card, "polymarket_odds", _now()))

    # How the structure has moved since Orbit first recorded this token (the
    # snapshot ledger). Only when there is history: a single row is the
    # present, which the dimensions above already show.
    if holder_snapshots.enabled():
        try:
            card = await holder_snapshots.history_card(subject)
        except Exception:
            card = None
        if card:
            dims.append(DimensionEvidence("history", "Holder history (Orbit ledger)", "available", card, "holder_snapshots", _now()))

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

    # Technical-indicator rating from the user's own TradingView, when connected.
    technicals = await asyncio.to_thread(tradingview.technicals_for, symbol or address) if symbol and tradingview.available() else None
    if technicals:
        dims.append(DimensionEvidence("technicals", "Technical indicators (TradingView)", "available", technicals, "tradingview_snapshot", _now()))

    # Dimensions that need paid data (no free source) -- disclosed as explicit gaps.
    for name, label, reason in (
        ("exchange_flows", "7-day exchange net-flow",
         "Not sourced (CEX in/out-flow accumulation-vs-distribution signal needs a paid provider)."),
        ("onchain_quant", "On-chain valuation (MVRV / cohorts)",
         "Not sourced (Glassnode-class cycle metrics need a paid provider)."),
    ):
        dims.append(DimensionEvidence(name, label, "unavailable", reason))

    return TokenEvidenceBundle(address=address, chain=chain, dimensions=dims, signals=signals)


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
