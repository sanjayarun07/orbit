"""Multi-chain token deep-dive -- the "analysis" lens.

A faithful reverse-engineering of Minara's `analysis` skill (its 10-dimension
crypto-token-analysis framework + snapshot evidence bundle), redesigned for Anvaya:

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
    "- Never write 'secure', 'safe' or 'unanimously bullish': say which checks passed, which were "
    "not run, and the sample behind any crowd read. Sources are records and links, not tool names.\n"
    "- Never state a launch or creation date without a source that dates it; a first-buyer time is "
    "not a launch date. A token program does not imply extensions.\n"
    "- Supply-control claims are bounded by coverage: a bundle check of N early buyers speaks for those N, "
    "never for 'distributed control'; top token accounts are accounts, not beneficial owners, unless owners were resolved.\n"
    "- Use only figures present in the evidence bundle below; never reconstruct a "
    "current metric, price, or event from prior knowledge. State a gap; do not fill it.\n"
    "- Holder lists include pools, exchanges, and treasury/program addresses — they "
    "do NOT describe beneficial owners. Top-10 concentration above ~30% is elevated "
    "risk, but read known-entity wallets in that context.\n"
    "- Concentration figures from different sources use different bases: a holders table counts every "
    "position (pools and exchanges included), a security profile applies its own exclusions. State each "
    "top-N figure with its source and basis; never merge figures from two sources into one 'top 10 … top 50' "
    "statement, and never write a top-50 share smaller than a top-10 share as if they shared a basis.\n"
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


# The lens's dimensions as a reader names them: an ask that names three or
# more of them about one token is a due-diligence ask whatever verb it uses
# ("Build an evidence table: identity, holders, liquidity, deployer and
# security" ran one dimension in the frozen trust run, 2026-09-23).
_DIMENSION_WORDS = {
    "identity_safety": re.compile(r"\b(?:identity|canonical|verified|legit(?:imacy)?|mint\s+authority|freeze\s+authority|honeypot|rug)\b", re.I),
    "holders": re.compile(r"\b(?:holders?|concentration|whales?|top\s+wallets?|distribution)\b", re.I),
    "liquidity": re.compile(r"\b(?:liquidity|pools?|lp\b|depth|slippage)\b", re.I),
    "deployer": re.compile(r"\b(?:deployer|creator|launch|bundl\w+|snip\w+|first\s+buyers?)\b", re.I),
    "security": re.compile(r"\b(?:security|audit\w*|contract\s+risk|exploit|permissions?|upgradeable|proxy)\b", re.I),
    "supply": re.compile(r"\b(?:unlocks?|vesting|supply\s+schedule|emissions?|inflation|circulating)\b", re.I),
    "sentiment": re.compile(r"\b(?:sentiment|social|twitter|crypto\s+twitter|\bx\s+says?)\b", re.I),
    "market": re.compile(r"\b(?:price\s+action|volume|market\s+cap|mcap|fdv|momentum)\b", re.I),
}


_TOP_N = re.compile(r"\btop[\s-]?(\d{1,3})\b[^.\n|]{0,60}?(\d{1,3}(?:\.\d+)?)\s*%", re.I)
_CONC_LINE = re.compile(r"(?im)^.*\btop[\s-]?(?:10|50|100)\b.*(?:%|per\s*cent).*$")


def concentration_conflicts(text: str) -> list[str]:
    """Lines where a larger top-N carries a smaller share than a smaller
    top-N: impossible on one basis ("Top 10 hold 38.37%, top 50 hold 17.47%"
    merged a holders table with a security profile, UAT preflight
    2026-09-27)."""
    out = []
    for line in (text or "").splitlines():
        pairs = [(int(n), float(p)) for n, p in _TOP_N.findall(line)]
        for n1, p1 in pairs:
            for n2, p2 in pairs:
                if n2 > n1 and p2 < p1:
                    out.append(line.strip()[:200])
                    break
            else:
                continue
            break
    return out


def sourced_concentration(evidence: str) -> list[str]:
    """The concentration lines as the evidence cards state them, each with
    its source card's own words about the basis."""
    lines = []
    for line in (evidence or "").splitlines():
        if _CONC_LINE.match(line) and "|" not in line[:3]:
            cleaned = re.sub(r"\s+", " ", re.sub(r"\*\*|`", "", line.strip().lstrip("-* "))).strip()
            if cleaned and cleaned not in lines:
                lines.append(cleaned[:300])
    return lines[:4]


_THRESHOLD_BEFORE = re.compile(r"(?:>|<|≥|≤|above|over|under|below|at\s+least|at\s+most|more\s+than|less\s+than)\s*$", re.I)
_APPROX_BEFORE = re.compile(r"(?:around|about|roughly|approx\.?|approximately|~|circa|c\.)\s*$", re.I)
_PCT_IN_TOPN = re.compile(r"\btop[\s-]?(\d{1,3})\b[^.\n|]{0,60}?(?:(\d{1,3}(?:\.\d+)?)\s*[–-]\s*)?(\d{1,3}(?:\.\d+)?)\s*%", re.I)


def _supported_pct(value: float, evidence_pcts: set[float], approximate: bool = False) -> bool:
    """A written share is the cards' when a card figure rounds to it at the
    written precision ("31%" for 30.92%, "38%" for 38.37%); an approximation
    ("~31%", "about 38%") may sit within 2.5 points of a card figure, and
    "~50%" with no card near it is not the cards' at all."""
    text = f"{value:g}"
    decimals = len(text.split(".")[1]) if "." in text else 0
    if approximate:
        return any(abs(e - value) <= 2.5 for e in evidence_pcts)
    return any(round(e, decimals) == round(value, decimals) or abs(e - value) <= 0.5 * 10 ** -decimals for e in evidence_pcts)


def _unsupported_shares(sentence: str, evidence_pcts: set[float]) -> list[float]:
    out = []
    for m in _PCT_IN_TOPN.finditer(sentence):
        for value in (m.group(2), m.group(3)):
            if not value:
                continue
            before = sentence[:m.start(3) if value == m.group(3) else m.start(2)]
            if _THRESHOLD_BEFORE.search(re.sub(r"[~\s]+$", "", before)):
                continue                                 # "above ~30%", ">30%": a threshold, not a figure
            if not _supported_pct(float(value), evidence_pcts, approximate=bool(_APPROX_BEFORE.search(before))):
                out.append(float(value))
    return out


def audit_concentration(answer: str, evidence: str) -> str:
    """Remove concentration claims the evidence cannot support as written: a
    sentence or table row that merges bases (a larger top-N with a smaller
    share), or a top-N share no card carries even rounded; then state the
    figures as the cards give them, with their bases. A rounded share of a
    card figure, or a threshold ("above ~30%"), stands."""
    evidence_pcts = {float(p) for _, p in _TOP_N.findall(evidence or "")}
    kept_lines, removed = [], []
    for line in (answer or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            if concentration_conflicts(line) or _unsupported_shares(line, evidence_pcts):
                removed.append(stripped[:160])
                continue
            kept_lines.append(line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z(*])", line)
        kept = []
        for sentence in sentences:
            if concentration_conflicts(sentence) or _unsupported_shares(sentence, evidence_pcts):
                removed.append(sentence.strip()[:160])
                continue
            kept.append(sentence)
        if kept or not sentences:
            kept_lines.append(" ".join(kept) if kept else line)
    if not removed:
        return answer
    sourced = sourced_concentration(evidence)
    block = ["", "**Holder concentration, as the sources state it** (each figure on its own source's basis; a holders table counts every position, a security profile applies its own exclusions):"]
    block += [f"- {line}" for line in sourced] if sourced else ["- no concentration figure in the evidence bundle"]
    block.append(f"- removed from the written analysis: {len(removed)} statement{'s' if len(removed) != 1 else ''} that merged or restated these figures on a basis the sources do not support")
    return "\n".join(kept_lines).rstrip() + "\n" + "\n".join(block)


def names_dimensions(request: str) -> int:
    """How many of the lens's dimensions the request names."""
    return sum(1 for pattern in _DIMENSION_WORDS.values() if pattern.search(request or ""))


@dataclass
class DimensionEvidence:
    name: str          # canonical dimension id (e.g. "identity_safety")
    label: str         # human label for the coverage envelope
    status: str        # "available" | "unavailable"
    detail: str        # the tool output, or a short reason when unavailable
    source: str | None = None   # the tool/provider that produced it
    as_of: str | None = None
    envelope: dict | None = None  # the tool's evidence envelope (app/evidence.py), when it recorded one


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
    """The coverage envelope as plain rows for a receipt: a dimension's
    availability, and when its tool recorded an envelope, that tool's own
    coverage and failed operations (a bundle check with 18 of 25 funding
    lookups failed is "available" and "partial" at once)."""
    rows = []
    for d in bundle.dimensions:
        row = {"name": d.name, "status": d.status, "source": d.source}
        if d.envelope:
            row["evidence"] = d.envelope.get("status")
            row["coverage"] = d.envelope.get("coverage")
            row["anchors"] = len(d.envelope.get("anchors") or [])
            if d.envelope.get("errors"):
                row["errors"] = d.envelope["errors"]
        rows.append(row)
    return rows


def attach_envelopes(dims: list[DimensionEvidence]) -> None:
    """Pair each dimension with the envelope its tool recorded this turn."""
    from app import evidence as _evidence

    by_tool = {e.tool: e for e in _evidence.collected()}
    for d in dims:
        env = by_tool.get(d.source or "")
        if env is not None:
            d.envelope = env.public()


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


async def _direct(name: str, factory, *, args=None, volatile: bool = False):
    """The default `call` hook: no cache, just the call."""
    result = factory()
    return await result if asyncio.iscoroutine(result) else result


async def _no_step(step_id: str, title: str | None = None) -> None:
    return None


async def _dim(query: str, caps: tuple[str, ...], chs: tuple[str, ...]) -> dict | None:
    """One router dimension as a JSON-able record (a job caches it)."""
    result = await _route(query, caps, chs)
    if result is not None and getattr(result, "output", "").strip():
        return {"output": result.output, "tool": result.tool}
    return None


async def build_token_evidence(address: str, chain: str, symbol: str | None = None, *, call=None, step=None) -> TokenEvidenceBundle:
    """Compose the deep-dive evidence bundle for a resolved (address, chain).

    Runs one router call per dimension concurrently; each dimension is marked
    available (with its source) or unavailable (with a reason). Never raises -- a
    dead source degrades that one dimension, not the bundle.

    `call(name, factory, args=..., volatile=...)` wraps every source: the
    durable job passes its operation cache so a resumed deep dive repeats no
    paid call; `step(id)` marks the job's plan as the bundle proceeds."""
    call = call or _direct
    step = step or _no_step
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
    await step("dimensions")
    results = await asyncio.gather(*[
        call(f"dim:{name}", lambda q=q, caps=caps, chs=chs: _dim(q, caps, chs), args={"q": q, "caps": caps, "chains": chs}, volatile=(name == "market"))
        for name, _, q, caps, chs in specs])

    dims: list[DimensionEvidence] = []
    for (name, label, _q, _caps, _chs), result in zip(specs, results):
        if isinstance(result, dict) and (result.get("output") or "").strip():
            dims.append(DimensionEvidence(name, label, "available", result["output"], result.get("tool"), _now()))
        else:
            dims.append(DimensionEvidence(
                name, label, "unavailable",
                "No usable data was returned by any provider for this dimension.", None, None))

    # Whether the liquidity can be pulled -- the question that decides a
    # memecoin (user, 2026-09-18: "use this for meme token complete in depth
    # analysis"). Called by name, not through the router, so it never competes
    # with the identity dossier above: they answer different questions.
    await step("forensics")
    if mobula_security.enabled():
        try:
            locks = await call("mobula:security", lambda: asyncio.to_thread(mobula_security.token_security, f"{address} on {chain}"), args=[address, chain])
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
            trades = await call("mobula:trades", lambda: asyncio.to_thread(mobula_meme.token_trades, f"latest trades {address} on {chain}"), args=[address, chain], volatile=True)
        except Exception:
            trades = None
        dims.append(DimensionEvidence("latest_trades", "Latest trades", "available", trades, "mobula_token_trades", _now())
                    if trades else
                    DimensionEvidence("latest_trades", "Latest trades", "unavailable", "Mobula returned no indexed swaps for this contract."))

    # Who got in first and whether they are still in: the sniper / exit read.
    if mobula_meme.enabled():
        try:
            first = await call("mobula:first_buyers", lambda: asyncio.to_thread(mobula_meme.token_first_buyers, f"first buyers {address} on {chain}"), args=[address, chain])
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
            cached = await call("mobula:bundle", lambda: _bundle_record(address, chain), args=[address, chain])
            analysis = mobula_meme.BundleAnalysis(**cached) if isinstance(cached, dict) and "level" in cached else None
            if isinstance(cached, dict) and cached.get("no_data"):
                raise NoData(cached["no_data"])
        except NoData as exc:
            analysis, reason = None, str(exc)
        except Exception as exc:  # noqa: BLE001 - a dead source degrades one dimension
            analysis, reason = None, f"Bundle check did not complete ({type(exc).__name__})."
        if analysis is not None:
            mobula_meme.bundle_evidence(analysis)
            dims.append(DimensionEvidence("bundle", "Bundle check (launch coordination)", "available",
                                          mobula_meme._render_bundle(analysis), "mobula_token_bundle", _now()))
            signals.append(mobula_meme.bundle_signal(subject, _now_iso(), analysis))
        else:
            dims.append(DimensionEvidence("bundle", "Bundle check (launch coordination)", "unavailable", reason))
            signals.append(Signal.abstain(mobula_meme.BUNDLE_MODEL, subject, _now_iso(), reason))

    # What X is saying, judged: a dimension for the lens and a vote on the
    # receipt (user decision 2026-09-21). Needs a symbol to search for.
    await step("crowd")
    if symbol and sentiment_analyst.enabled():
        try:
            out = await call("x:sentiment", lambda: _sentiment_record(symbol, subject), args=[symbol])
            if isinstance(out, dict) and isinstance(out.get("signal"), dict):
                out = {**out, "signal": Signal(**out["signal"])}
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
            markets = await call("polymarket:markets", lambda: asyncio.to_thread(polymarket_odds.markets_for, symbol), args=[symbol], volatile=True)
            card = polymarket_odds.render_card(symbol, markets)
        except Exception:
            card = None
        if card:
            dims.append(DimensionEvidence("prediction_markets", "Prediction markets (Polymarket)", "available", card, "polymarket_odds", _now()))

    # How the structure has moved since Anvaya first recorded this token (the
    # snapshot ledger). Only when there is history: a single row is the
    # present, which the dimensions above already show.
    await step("ledger")
    if holder_snapshots.enabled():
        try:
            card = await call("ledger:history", lambda: holder_snapshots.history_card(subject), args=[subject.key])
        except Exception:
            card = None
        if card:
            dims.append(DimensionEvidence("history", "Holder history (Anvaya ledger)", "available", card, "holder_snapshots", _now()))

    # Token unlocks -- the most deterministic near-term headwind. Real, free, and
    # verified-by-address via DefiLlama emissions; unavailable when the token isn't
    # tracked (most memecoins) rather than silently omitted.
    unlocks = await call("defillama:unlocks", lambda: asyncio.to_thread(token_unlocks, symbol, address, chain), args=[symbol, address, chain])
    if unlocks:
        dims.append(DimensionEvidence("unlocks", "Token unlocks / emissions", "available", unlocks, "defillama_emissions", _now()))
    else:
        dims.append(DimensionEvidence(
            "unlocks", "Token unlocks / emissions", "unavailable",
            "Not tracked by DefiLlama emissions (typical for memecoins / no vesting schedule)."))

    # Technical-indicator rating from the user's own TradingView, when connected.
    technicals = (await call("tradingview:technicals", lambda: asyncio.to_thread(tradingview.technicals_for, symbol or address), args=[symbol], volatile=True)
                  if symbol and tradingview.available() else None)
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

    attach_envelopes(dims)
    return TokenEvidenceBundle(address=address, chain=chain, dimensions=dims, signals=signals)


async def _bundle_record(address: str, chain: str) -> dict:
    """The bundle analysis as a JSON-able record; NoData becomes a record too,
    so a cached "no first buyers" is not retried."""
    import dataclasses
    try:
        analysis = await asyncio.to_thread(mobula_meme._bundle_analysis, address, chain)
    except NoData as exc:
        return {"no_data": str(exc)}
    return dataclasses.asdict(analysis)


async def _sentiment_record(symbol: str, subject: Subject) -> dict | None:
    out = await sentiment_analyst.analyze(symbol, subject=subject)
    if out is None:
        return None
    signal = out.get("signal")
    return {**out, "signal": signal.model_dump(mode="json") if hasattr(signal, "model_dump") else signal}


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
