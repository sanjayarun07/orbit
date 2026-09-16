"""Protocol registry bootstrapped from DefiLlama's /protocols endpoint (free,
no key). The registry is the backbone: every document, chunk and edge hangs
off a protocol id, and each protocol becomes an entity with its chains,
category and token as first edges of the graph.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import httpx

from app.knowledge.entities import category_id, chain_id, protocol_id, seed_chain_entities, slugify
from app.knowledge.models import Entity, Protocol, Relationship
from app.knowledge.overrides import overrides_for

logger = logging.getLogger(__name__)

DEFILLAMA_PROTOCOLS = "https://api.llama.fi/protocols"
_SKIP_CATEGORIES = {"CEX", "Chain"}


def _fetch_protocols() -> list[dict]:
    with httpx.Client(timeout=30, headers={"User-Agent": "Dopamint-Knowledge/0.1"}) as client:
        resp = client.get(DEFILLAMA_PROTOCOLS)
        resp.raise_for_status()
        data = resp.json()
    return data if isinstance(data, list) else []


_VERSION_SUFFIX = re.compile(r"\s+v\d+(?:\.\d+)?$", re.I)
_GENERIC_WORDS = {"the", "protocol", "finance", "network", "labs", "dao", "swap", "bridge", "staked", "liquid", "staking", "lend", "lending", "core", "pool", "vault", "money", "capital", "chain", "token"}


def name_aliases(name: str) -> set[str]:
    """How people actually refer to a protocol: "Kamino" for "Kamino Lend",
    "Uniswap" for "Uniswap V3", "Morpho" for "Morpho Blue". Shared first words
    ("Binance Staked ETH", "Binance Bitcoin") end up on several entities and
    the resolver then treats them as ambiguous, which is the right outcome."""
    out: set[str] = set()
    base = _VERSION_SUFFIX.sub("", name.strip())
    if base and base.lower() != name.lower():
        out.add(base)
    first = re.split(r"[\s\-]+", base, maxsplit=1)[0] if base else ""
    if first and first.lower() != base.lower() and len(first) >= 4 and first.isalpha() and first.lower() not in _GENERIC_WORDS:
        out.add(first)
    return out


def protocol_from_llama(item: dict) -> Protocol | None:
    slug = str(item.get("slug") or slugify(item.get("name") or ""))
    if not slug or (item.get("category") in _SKIP_CATEGORIES):
        return None
    name = str(item.get("name") or slug)
    aliases = {name, slug.replace("-", " "), *name_aliases(name)}
    if item.get("symbol") and item["symbol"] != "-":
        aliases.add(str(item["symbol"]))
    for extra in (item.get("parentProtocol") or "", item.get("module") or ""):
        if extra and isinstance(extra, str) and 2 < len(extra) < 40:
            aliases.add(extra.replace("parent#", "").replace("-", " "))
    github = None
    for repo in item.get("github") or []:
        if isinstance(repo, str) and repo:
            github = repo.split("/")[0]
            break
    website = item.get("url")
    extra = overrides_for(slug)
    return Protocol(
        id=protocol_id(slug), slug=slug, name=name, symbol=(item.get("symbol") if item.get("symbol") not in (None, "-") else None),
        category=item.get("category"), description=(item.get("description") or None), website=website,
        docs_url=extra.get("docs_url"), github_org=github, governance_url=extra.get("governance_url"), forum_url=extra.get("forum_url"),
        defillama_slug=slug, coingecko_id=item.get("gecko_id"),
        twitter_handle=item.get("twitter"), chains=[slugify(c) for c in (item.get("chains") or [])],
        contracts=[{"chain": slugify(chain), "address": address, "label": "token"} for chain, address in _token_addresses(item)],
        aliases=sorted(a for a in aliases if a and a.lower() != name.lower()), tvl_usd=float(item.get("tvl") or 0) or None,
        last_updated=datetime.now(timezone.utc),
    )


def _token_addresses(item: dict) -> list[tuple[str, str]]:
    address = item.get("address")
    if not address or not isinstance(address, str) or address in ("-", "0x0000000000000000000000000000000000000000"):
        return []
    if ":" in address:
        chain, addr = address.split(":", 1)
        return [(chain, addr)]
    return [("ethereum", address)]


def entities_for(protocol: Protocol) -> tuple[list[Entity], list[Relationship]]:
    """The protocol entity plus its chains, category and token, and the edges
    that connect them. Structured sources first: these are high-confidence."""
    now = datetime.now(timezone.utc)
    entities = [Entity(
        id=protocol.id, entity_type="protocol", canonical_name=protocol.name, symbol=protocol.symbol, aliases=protocol.aliases,
        metadata={"defillama_slug": protocol.defillama_slug, "coingecko_id": protocol.coingecko_id, "category": protocol.category, "website": protocol.website, "github_org": protocol.github_org, "tvl_usd": protocol.tvl_usd},
    )]
    relationships = []
    for chain in protocol.chains:
        entities.append(Entity(id=chain_id(chain), entity_type="chain", canonical_name=chain.replace("-", " ").title(), aliases=[]))
        relationships.append(Relationship(protocol.id, "DEPLOYED_ON", chain_id(chain), confidence=0.98, valid_from=None, observed_at=now, metadata={"source": "defillama"}))
    if protocol.category:
        entities.append(Entity(id=category_id(protocol.category), entity_type="category", canonical_name=protocol.category, aliases=[]))
        relationships.append(Relationship(protocol.id, "IN_CATEGORY", category_id(protocol.category), confidence=0.98, observed_at=now, metadata={"source": "defillama"}))
    for contract in protocol.contracts:
        if contract.get("label") == "token" and contract.get("address"):
            # Canonical name is "<Protocol> token", never the bare ticker: a ticker
            # must only ever resolve through the low-confidence rungs of the ladder.
            token = Entity(id=f"token:{contract['chain']}:{contract['address'].lower() if contract['address'].startswith('0x') else contract['address']}", entity_type="token",
                           canonical_name=f"{protocol.name} token", symbol=protocol.symbol, chain=contract["chain"], address=contract["address"],
                           aliases=[], metadata={"coingecko_id": protocol.coingecko_id})
            entities.append(token)
            relationships.append(Relationship(token.id, "TOKEN_OF", protocol.id, confidence=0.98, observed_at=now, metadata={"source": "defillama"}))
    return entities, relationships


async def bootstrap(limit: int = 50, items: list[dict] | None = None) -> dict:
    """Top-`limit` protocols by TVL into the registry, with their entities and
    first-degree edges. Returns counts."""
    from app.knowledge.store import get_store

    store = await get_store()
    raw = items if items is not None else _fetch_protocols()
    raw = sorted((i for i in raw if isinstance(i, dict)), key=lambda i: -(float(i.get("tvl") or 0)))
    written = entities_written = relationships_written = 0
    for chain in seed_chain_entities():
        await store.upsert_entity(chain)
    for item in raw:
        if written >= limit:
            break
        protocol = protocol_from_llama(item)
        if protocol is None:
            continue
        await store.upsert_protocol(protocol)
        entities, relationships = entities_for(protocol)
        for entity in entities:
            await store.upsert_entity(entity)
            entities_written += 1
        for rel in relationships:
            relationships_written += int(await store.upsert_relationship(rel))
        written += 1
    logger.info("knowledge: registry bootstrapped with %d protocols", written)
    return {"protocols": written, "entities": entities_written, "relationships": relationships_written}


# DefiLlama's `url` is usually the app, not the site: app.morpho.org, portal.arbitrum.io,
# data.grove.finance. Strip those labels back to the registrable domain before guessing.
_APP_LABELS = {"app", "www", "docs", "portal", "data", "stake", "bridge", "dashboard", "swap", "trade", "mainnet", "go", "my"}


def _registrable_domain(host: str) -> str:
    labels = [label for label in host.lower().split(".") if label]
    while len(labels) > 2 and labels[0] in _APP_LABELS:
        labels.pop(0)
    return ".".join(labels)


def guess_docs_urls(protocol: Protocol) -> list[str]:
    """Ranked candidates for where a protocol's docs live. The crawler tries
    them in order and keeps the first root that actually serves a docs site
    (a 404 or a link-less app shell is skipped, not indexed)."""
    out: list[str] = []
    if protocol.docs_url:
        out.append(protocol.docs_url.rstrip("/"))
    site = (protocol.website or "").strip()
    host = re.sub(r"^https?://", "", site).split("/", 1)[0].split("?", 1)[0].lower()
    if host:
        if host.startswith("docs."):
            out.append(f"https://{host}")
        domain = _registrable_domain(host)
        if domain:
            out.extend([f"https://docs.{domain}", f"https://{domain}/docs", f"https://www.{domain}/docs"])
    seen: set[str] = set()
    return [u for u in out if not (u in seen or seen.add(u))]


def guess_docs_url(protocol: Protocol) -> str | None:
    """The most likely docs root (first candidate of `guess_docs_urls`)."""
    candidates = guess_docs_urls(protocol)
    return candidates[0] if candidates else None
