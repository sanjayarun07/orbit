"""Canonical entity ids and the resolver.

Ids: chain:<slug> · protocol:<slug> · token:<chain>:<address> · dao:<slug> ·
person:<slug> · category:<slug>. A contract or mint plus chain outranks a
ticker; a ticker alone never resolves with high confidence because hundreds
of tokens share one symbol.

Confidence ladder (from the design):
    contract address       1.00
    chain + contract       1.00
    CoinGecko id            .98
    DefiLlama id            .98
    exact canonical name    .90
    alias + context         .75
    ticker alone            .40
"""

from __future__ import annotations

import re

from app.knowledge.models import Entity, Resolution

CONFIDENCE = {"contract": 1.0, "chain_contract": 1.0, "coingecko_id": 0.98, "defillama_id": 0.98, "canonical_name": 0.90, "alias": 0.75, "ticker": 0.40}

CHAIN_ALIASES = {
    "ethereum": ["eth", "ethereum mainnet", "mainnet", "chain:1", "eip155:1", "ether"],
    "solana": ["sol", "solana mainnet", "solana-mainnet"],
    "base": ["base mainnet", "eip155:8453", "chain:8453"],
    "arbitrum": ["arbitrum one", "arb", "eip155:42161", "chain:42161"],
    "optimism": ["op mainnet", "op", "eip155:10", "chain:10"],
    "polygon": ["matic", "polygon pos", "eip155:137", "chain:137"],
    "bsc": ["bnb chain", "bnb smart chain", "binance smart chain", "eip155:56", "chain:56"],
    "avalanche": ["avax", "avalanche c-chain", "eip155:43114"],
    "hyperliquid": ["hyperevm", "hyperliquid l1", "eip155:999"],
    "sui": [], "aptos": ["apt"], "tron": ["trx"],
}
_COMMON_WORDS = {"mode", "base", "core", "flow", "near", "zero", "world", "moonbeam", "fuse", "wax", "step", "ronin", "sonic", "unit", "manta", "linea", "blast", "kava", "celo", "ink", "soneium"}
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def chain_id(name: str) -> str:
    return f"chain:{slugify(name)}"


def protocol_id(slug: str) -> str:
    return f"protocol:{slugify(slug)}"


def token_id(chain: str, address: str) -> str:
    return f"token:{slugify(chain)}:{address.lower() if address.startswith('0x') else address}"


def category_id(name: str) -> str:
    return f"category:{slugify(name)}"


def seed_chain_entities() -> list[Entity]:
    return [Entity(id=chain_id(name), entity_type="chain", canonical_name=name.title() if name != "bsc" else "BNB Chain", aliases=aliases) for name, aliases in CHAIN_ALIASES.items()]


class EntityResolver:
    """Resolves mentions against an in-memory index of entities. The store
    feeds it; ingestion re-feeds it as entities are written."""

    def __init__(self, entities: list[Entity] | None = None):
        self._by_id: dict[str, Entity] = {}
        self._by_name: dict[str, str] = {}
        self._by_alias: dict[str, list[str]] = {}
        self._by_symbol: dict[str, list[str]] = {}
        self._by_address: dict[tuple[str | None, str], str] = {}
        self._by_external: dict[tuple[str, str], str] = {}  # (coingecko_id|defillama_slug, value) -> id
        for entity in entities or []:
            self.add(entity)

    def add(self, entity: Entity) -> None:
        self._by_id[entity.id] = entity
        self._by_name[entity.canonical_name.lower()] = entity.id
        # Case-folded keys: "aave" and "AAVE" are one alias, not two candidates
        # (two entries for one entity looked ambiguous and blocked resolution).
        for alias in entity.aliases:
            bucket = self._by_alias.setdefault(alias.lower(), [])
            if entity.id not in bucket:
                bucket.append(entity.id)
        if entity.symbol:
            bucket = self._by_symbol.setdefault(entity.symbol.upper(), [])
            if entity.id not in bucket:
                bucket.append(entity.id)
        if entity.address:
            self._by_address[(entity.chain, entity.address.lower())] = entity.id
            self._by_address[(None, entity.address.lower())] = entity.id
        for key in ("coingecko_id", "defillama_slug"):
            if entity.metadata.get(key):
                self._by_external[(key, str(entity.metadata[key]).lower())] = entity.id

    def get(self, entity_id: str) -> Entity | None:
        return self._by_id.get(entity_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def resolve(self, mention: str, chain: str | None = None, context: str = "") -> Resolution | None:
        text = (mention or "").strip()
        if not text:
            return None
        lowered = text.lower().lstrip("$")
        # 1. addresses
        if _EVM.match(text) or _SOL.match(text):
            hit = self._by_address.get((slugify(chain) if chain else None, text.lower())) or self._by_address.get((None, text.lower()))
            if hit:
                return Resolution(self._by_id[hit], CONFIDENCE["chain_contract" if chain else "contract"], "chain_contract" if chain else "contract")
            return None
        # 2. external ids
        for key in ("coingecko_id", "defillama_slug"):
            hit = self._by_external.get((key, lowered))
            if hit:
                return Resolution(self._by_id[hit], CONFIDENCE["coingecko_id" if key == "coingecko_id" else "defillama_id"], key)
        # 3. exact canonical name
        hit = self._by_name.get(lowered)
        if hit:
            return Resolution(self._by_id[hit], CONFIDENCE["canonical_name"], "canonical_name")
        # 4. alias (+ context disambiguation)
        candidates = self._by_alias.get(lowered) or []
        if len(candidates) == 1:
            return Resolution(self._by_id[candidates[0]], CONFIDENCE["alias"], "alias")
        if len(candidates) > 1:
            pick = self._disambiguate(candidates, chain, context)
            if pick:
                return Resolution(self._by_id[pick], CONFIDENCE["alias"] - 0.1, "alias")
        # 5. ticker alone: low confidence, prefer a single unambiguous match
        symbols = self._by_symbol.get(text.upper().lstrip("$")) or []
        if len(symbols) == 1:
            return Resolution(self._by_id[symbols[0]], CONFIDENCE["ticker"], "ticker")
        if len(symbols) > 1:
            pick = self._disambiguate(symbols, chain, context)
            if pick:
                return Resolution(self._by_id[pick], CONFIDENCE["ticker"], "ticker")
        return None

    def _disambiguate(self, candidates: list[str], chain: str | None, context: str) -> str | None:
        context = (context or "").lower()
        scored = []
        for cid in candidates:
            entity = self._by_id[cid]
            score = 0
            if chain and entity.chain == slugify(chain):
                score += 2
            if entity.canonical_name.lower() in context:
                score += 2
            if any(alias.lower() in context for alias in entity.aliases):
                score += 1
            scored.append((score, cid))
        scored.sort(reverse=True)
        if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
        return None

    def mentions(self, text: str, context: str = "") -> list[Resolution]:
        """Every entity mentioned in a text, by canonical name or alias, with
        the resolver's confidence. Cheap and deterministic: the ingestion
        extractor uses it before any model-based extraction."""
        found: dict[str, Resolution] = {}
        text = text or ""
        lowered = text.lower()
        names = [(name, eid) for name, eid in self._by_name.items()] + [(alias, eids[0]) for alias, eids in self._by_alias.items() if len(eids) == 1]
        for name, eid in names:
            if len(name) < 3:
                continue
            entity = self._by_id[eid]
            if len(name) <= 5 or name in _COMMON_WORDS:
                # Short or dictionary-word names ("Mode", "Base", "Core", "Flow") only count
                # as a mention when capitalised and standalone -- "E-mode" is not Mode L2.
                pattern = r"(?<![A-Za-z0-9$#-])" + re.escape(entity.canonical_name if name == entity.canonical_name.lower() else name.capitalize()) + r"(?![a-z0-9-])"
                if not re.search(pattern, text):
                    continue
            elif not re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", lowered):
                continue
            if True:
                method = "canonical_name" if name == self._by_id[eid].canonical_name.lower() else "alias"
                resolution = Resolution(self._by_id[eid], CONFIDENCE[method], method)
                if eid not in found or found[eid].confidence < resolution.confidence:
                    found[eid] = resolution
        return list(found.values())
