"""Market-wide DefiLlama datasets (free): every recorded hack, and every
stablecoin with its peg and mechanism. These are `scope = "global"`
connectors: ingestion runs each once per pass rather than once per
protocol, and each document decides for itself which protocol it belongs
to (resolved by name against the registry) or stands alone.

Hacks cover far more than the registry -- Ronin, Wormhole, Euler, FTX -- so
"biggest DeFi hacks" and "what happened to Euler" answer from here even
when the protocol is not indexed. Stablecoins carry the part of the story
that never changes: what backs USDe, what pegs to what, on which chains.
Circulating supply is a dated snapshot in the text; the live number is a tool.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.knowledge.connectors.base import SourceRef
from app.knowledge.connectors.defillama import DefiLlamaConnector, _date, _money
from app.knowledge.entities import EntityResolver, chain_id, slugify
from app.knowledge.models import NormalizedDocument, Protocol
from app.knowledge.normalize import content_hash

logger = logging.getLogger(__name__)

HACKS_URL = "https://api.llama.fi/hacks"
STABLECOINS_URL = "https://stablecoins.llama.fi/stablecoins?includePrices=false"
MIN_STABLECOIN_CIRCULATING_USD = 1_000_000.0
_UA = {"User-Agent": "Dopamint-Knowledge/0.1"}

PEG_NAMES = {"peggedUSD": "US dollar", "peggedEUR": "euro", "peggedGBP": "pound sterling", "peggedCHF": "Swiss franc", "peggedJPY": "yen",
             "peggedCAD": "Canadian dollar", "peggedAUD": "Australian dollar", "peggedCNY": "yuan", "peggedBTC": "bitcoin", "peggedETH": "ether",
             "peggedVAR": "a basket / variable target", "peggedXAU": "gold", "peggedREAL": "Brazilian real", "peggedTRY": "Turkish lira"}


class GlobalHacksConnector:
    name = "llama_hacks"
    source_type = "incident"
    scope = "global"
    refresh_every = timedelta(hours=24)

    def __init__(self, resolver: EntityResolver | None = None, protocols: dict[str, Protocol] | None = None):
        self.resolver = resolver
        self.protocols = protocols or {}

    def applies(self, protocol: Protocol) -> bool:
        return protocol.id == "global"

    def discover(self, protocol: Protocol):
        return [SourceRef(url=HACKS_URL, title="DefiLlama hacks", kind="bundle")]

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        return None

    def documents(self, protocol: Protocol, ref: SourceRef) -> list[NormalizedDocument]:
        with httpx.Client(timeout=40, headers=_UA) as client:
            resp = client.get(ref.url)
            resp.raise_for_status()
            rows = resp.json()
        return self.build([r for r in rows if isinstance(r, dict)], self.resolver, self.protocols)

    @classmethod
    def build(cls, hacks: list[dict], resolver: EntityResolver | None, protocols: dict[str, Protocol]) -> list[NormalizedDocument]:
        out: list[NormalizedDocument] = []
        seen: set[str] = set()
        for hack in hacks:
            hack = _normalise_hack(hack)
            if not hack:
                continue
            owner = cls.owner(hack["name"], resolver, protocols)
            doc = DefiLlamaConnector.incident_document(owner, hack) if owner else cls.standalone(hack)
            if doc and doc.url not in seen:
                seen.add(doc.url)
                out.append(doc)
        return out

    @staticmethod
    def owner(name: str, resolver: EntityResolver | None, protocols: dict[str, Protocol]) -> Protocol | None:
        if not resolver or not name:
            return None
        resolution = resolver.resolve(name)
        if not resolution or resolution.confidence < 0.9 or resolution.entity.entity_type != "protocol":
            return None
        return protocols.get(resolution.entity.id)

    @staticmethod
    def standalone(hack: dict) -> NormalizedDocument | None:
        """A hack on something outside the registry: same shape, no protocol."""
        date = _date(hack.get("date"))
        if not date:
            return None
        name = str(hack.get("name") or "Unknown protocol")
        chains = [c for c in (hack.get("chain") or []) if isinstance(c, str)]
        title = f"{name} exploit — {date}"
        lines = [
            f"# {title}", "",
            f"**Target**: {name}" + (f" ({hack.get('targetType')})" if hack.get("targetType") else "") + f" · **Date**: {date} · **Loss**: {_money(hack.get('amount'))}"
            + (f" · **Returned**: {_money(hack.get('returnedFunds'))}" if hack.get("returnedFunds") else "") + (f" · **Chains**: {', '.join(chains)}" if chains else ""),
            f"**Classification**: {hack.get('classification') or 'unknown'} · **Technique**: {hack.get('technique') or 'unknown'}"
            + (" · **Bridge hack**" if hack.get("bridgeHack") else "") + (f" · **Language**: {hack.get('language')}" if hack.get("language") and hack.get("language") != "None" else ""),
            "", "## What happened",
            f"{name} suffered a {str(hack.get('classification') or 'security').lower()} incident on {date} using {str(hack.get('technique') or 'an unknown technique').lower()},"
            f" with an estimated {_money(hack.get('amount'))} lost" + (f" of which {_money(hack.get('returnedFunds'))} was later returned" if hack.get("returnedFunds") else "") + ".",
        ]
        if hack.get("source"):
            lines += ["", f"Source: {hack['source']}"]
        content = "\n".join(lines)
        slug = slugify(name)
        incident_id = f"incident:{slug}:{date}"
        return NormalizedDocument(
            source="defillama_hacks", source_type="incident", url=f"https://defillama.com/hacks#{slug}-{date}", protocol_id=None, title=title,
            content=content, content_hash=content_hash(content), published_at=datetime.fromisoformat(date).replace(tzinfo=timezone.utc),
            metadata={"date": date, "amount_usd": hack.get("amount"), "returned_usd": hack.get("returnedFunds"), "technique": hack.get("technique"), "classification": hack.get("classification"),
                      "chains": chains, "target": name, "bridge_hack": bool(hack.get("bridgeHack")),
                      "facts": [{"target": {"id": incident_id, "type": "incident", "name": title, "aliases": _incident_aliases(name),
                                            "metadata": {"date": date, "amount_usd": hack.get("amount"), "technique": hack.get("technique"), "classification": hack.get("classification"), "target": name}}}]
                      + [{"source": incident_id, "relation": "DEPLOYED_ON", "target": {"id": chain_id(c), "type": "chain", "name": c}, "confidence": 0.9, "metadata": {"source": "defillama"}} for c in chains[:4]]},
        )


def _incident_aliases(name: str) -> list[str]:
    """"Ronin Network" is asked about as "Ronin": the full name plus the
    first-word alias people use, so the incident is a recognised mention."""
    from app.knowledge.registry import name_aliases

    out = {name} if len(name) > 3 else set()
    out |= name_aliases(name)
    return sorted(out)


def _normalise_hack(hack: dict) -> dict | None:
    """The global list stringifies numbers, dates and even the chain list."""
    if not hack.get("name"):
        return None
    out = dict(hack)
    for key in ("date", "amount", "returnedFunds"):
        value = out.get(key)
        if isinstance(value, str):
            try:
                out[key] = float(value) if key != "date" else int(float(value))
            except ValueError:
                out[key] = None
    if out.get("returnedFunds") in (0, 0.0):
        out["returnedFunds"] = None
    chain = out.get("chain")
    if isinstance(chain, str):
        import ast

        try:
            parsed = ast.literal_eval(chain)
            out["chain"] = [str(c) for c in parsed] if isinstance(parsed, (list, tuple)) else [chain]
        except (ValueError, SyntaxError):
            out["chain"] = [chain] if chain and chain != "None" else []
    if isinstance(out.get("bridgeHack"), str):
        out["bridgeHack"] = out["bridgeHack"].lower() == "true"
    return out


class StablecoinsConnector:
    name = "llama_stablecoins"
    source_type = "defillama"
    scope = "global"
    refresh_every = timedelta(days=3)

    def __init__(self, resolver: EntityResolver | None = None, min_circulating_usd: float = MIN_STABLECOIN_CIRCULATING_USD):
        self.resolver = resolver
        self.min_circulating_usd = min_circulating_usd

    def applies(self, protocol: Protocol) -> bool:
        return protocol.id == "global"

    def discover(self, protocol: Protocol):
        return [SourceRef(url=STABLECOINS_URL, title="DefiLlama stablecoins", kind="bundle")]

    def fetch(self, protocol: Protocol, ref: SourceRef) -> NormalizedDocument | None:
        return None

    def documents(self, protocol: Protocol, ref: SourceRef) -> list[NormalizedDocument]:
        with httpx.Client(timeout=40, headers=_UA) as client:
            resp = client.get(ref.url)
            resp.raise_for_status()
            assets = (resp.json() or {}).get("peggedAssets") or []
        return self.build([a for a in assets if isinstance(a, dict)], self.resolver, self.min_circulating_usd)

    @classmethod
    def build(cls, assets: list[dict], resolver: EntityResolver | None, min_circulating_usd: float = MIN_STABLECOIN_CIRCULATING_USD) -> list[NormalizedDocument]:
        out = []
        for asset in assets:
            doc = cls.document(asset, resolver, min_circulating_usd)
            if doc:
                out.append(doc)
        return out

    @staticmethod
    def document(asset: dict, resolver: EntityResolver | None, min_circulating_usd: float = MIN_STABLECOIN_CIRCULATING_USD) -> NormalizedDocument | None:
        name, raw_symbol = str(asset.get("name") or ""), str(asset.get("symbol") or "")
        symbol = raw_symbol.upper()
        if not name or not symbol:
            return None
        peg_type = str(asset.get("pegType") or "")
        circulating = asset.get("circulating") or {}
        amount = float(circulating.get(peg_type) or 0) if isinstance(circulating, dict) else 0.0
        if amount < min_circulating_usd:
            return None
        mechanism = str(asset.get("pegMechanism") or "unknown")
        chains = [str(c) for c in (asset.get("chains") or []) if isinstance(c, str)]
        gecko = asset.get("gecko_id") or None
        peg_name = PEG_NAMES.get(peg_type, peg_type.replace("pegged", "") or "unknown")
        snapshot = datetime.now(timezone.utc).date().isoformat()
        issuer = None
        if resolver:
            for candidate in resolver.mentions(name):
                if candidate.entity.entity_type == "protocol" and candidate.confidence >= 0.75:
                    issuer = candidate.entity
                    break
        lines = [
            f"# {name} ({symbol})", "",
            f"**Type**: stablecoin pegged to {peg_name} · **Mechanism**: {mechanism}" + (f" · **Issuer**: {issuer.canonical_name}" if issuer else "")
            + (f" · **CoinGecko**: {gecko}" if gecko else ""),
            f"**Chains** ({len(chains)}): {', '.join(chains[:25])}" + (" …" if len(chains) > 25 else "") if chains else "**Chains**: unknown",
            f"**Circulating**: about {_money(amount)} (DefiLlama snapshot, {snapshot}; ask for the live figure)",
            "", "## Peg and backing",
            _mechanism_text(name, symbol, mechanism, peg_name),
        ]
        if asset.get("priceSource"):
            lines.append(f"Price source used by DefiLlama: {asset['priceSource']}.")
        content = "\n".join(lines)
        entity_id = f"stablecoin:{slugify(gecko or name)}"
        facts = [
            # Keep the symbol as the coin spells it ("USDe") and upper-cased: mentions match the spelling people use.
            {"target": {"id": entity_id, "type": "token", "name": name, "symbol": symbol, "aliases": sorted({s for s in (raw_symbol, symbol) if s and s.lower() != name.lower()}),
                        "metadata": {"peg_type": peg_type, "peg_mechanism": mechanism, "coingecko_id": gecko, "stablecoin": True}}},
            {"source": entity_id, "relation": "PEGGED_TO", "target": {"id": f"asset:{slugify(peg_name)}", "type": "asset", "name": peg_name.capitalize() if peg_name.islower() else peg_name},
             "confidence": 0.98, "metadata": {"source": "defillama", "mechanism": mechanism}},
        ]
        facts += [{"source": entity_id, "relation": "DEPLOYED_ON", "target": {"id": chain_id(c), "type": "chain", "name": c}, "confidence": 0.9, "metadata": {"source": "defillama"}} for c in chains[:40]]
        if issuer:
            facts.append({"source": entity_id, "relation": "ISSUED_BY", "target": {"id": issuer.id, "type": "protocol", "name": issuer.canonical_name}, "confidence": 0.85, "metadata": {"source": "defillama", "method": "name"}})
        return NormalizedDocument(
            source="defillama_stablecoins", source_type="defillama", url=f"https://defillama.com/stablecoin/{slugify(name)}", protocol_id=issuer.id if issuer else None,
            title=f"{name} ({symbol}) — stablecoin", content=content, content_hash=content_hash(content),
            metadata={"symbol": symbol, "peg_type": peg_type, "peg_mechanism": mechanism, "chains": chains, "circulating_usd": amount, "coingecko_id": gecko, "facts": facts},
        )


def _mechanism_text(name: str, symbol: str, mechanism: str, peg_name: str) -> str:
    if mechanism == "fiat-backed":
        return f"{name} is a fiat-backed stablecoin: each {symbol} is meant to be redeemable for one unit of {peg_name}, backed by reserves held off-chain (cash, deposits, short-dated government debt) by the issuer."
    if mechanism == "crypto-backed":
        return f"{name} is crypto-backed: {symbol} is issued against on-chain collateral (or a delta-neutral position) worth more than, or hedged against, the {peg_name} value it represents, with redemption or arbitrage keeping the peg."
    if mechanism == "algorithmic":
        return f"{name} is algorithmic: the {peg_name} peg is maintained by supply expansion and contraction rules rather than full reserves, which historically carries the highest depeg risk."
    return f"{name} targets {peg_name}; DefiLlama records its mechanism as {mechanism}."
