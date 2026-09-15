"""The router-facing side of the knowledge service.

`knowledge_base_search` is a ProviderTool under the `knowledge` capability:
its matcher fires on "what is / explain / how does X work" asks that name a
protocol in the registry, and its output is retrieved passages with numbered
citations -- the LLM synthesises over them in the research node like any
other tool result. Live numbers (TVL, price) deliberately stay with the
DefiLlama / market tools.
"""

from __future__ import annotations

import asyncio
import re

from app.knowledge.entities import EntityResolver
from app.knowledge.retrieval import build_context, search
from app.knowledge.store import get_store

TRIGGER = re.compile(
    r"\b(?:what\s+is|what\s+are|what's|explain|describe|how\s+does|how\s+do|how\s+is|overview\s+of|tell\s+me\s+about|introduction\s+to|"
    r"how\s+(?:does|do)\s+.+\s+work|docs?\s+(?:for|on)|documentation|liquidation|collateral|governance|tokenomics|architecture|mechanism|"
    r"compete|competitors?|alternatives?\s+to|integrat(?:e|es|ion)\s+with|deployed\s+on|supports?)\b",
    re.IGNORECASE,
)
_LIVE = re.compile(r"\b(?:tvl|price|volume|market\s*cap|holders?|apy|apr|yield\s+now|right\s+now|today|current)\b", re.IGNORECASE)

# The resolver snapshot is the one piece of knowledge state that sync code may
# read: the router's matcher runs inside request handling (and, for the tool
# call, in a worker thread), and neither may touch the asyncpg pool from a
# foreign event loop. `resolver()` refreshes it on the main loop; `matches()`
# only ever reads it.
_resolver_cache: tuple[float, EntityResolver] | None = None
_RESOLVER_TTL = 120.0
_main_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _main_loop
    _main_loop = loop


async def resolver(force: bool = False) -> EntityResolver:
    """Resolver over the store's entities, rebuilt every couple of minutes and
    right after admin bootstrap / ingest. Must be awaited on the main loop."""
    global _resolver_cache
    import time

    if not force and _resolver_cache and _resolver_cache[0] > time.monotonic():
        return _resolver_cache[1]
    store = await get_store()
    built = EntityResolver(await store.list_entities())
    _resolver_cache = (time.monotonic() + _RESOLVER_TTL, built)
    return built


def snapshot() -> EntityResolver | None:
    return _resolver_cache[1] if _resolver_cache else None


def _run(coro):
    """Run a knowledge coroutine from sync code. Inside the app the main loop
    owns the DB pool, so the work is scheduled there and awaited from this
    worker thread; outside an app (tests, scripts) a private loop is fine."""
    loop = _main_loop
    if loop is not None and loop.is_running():
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=90)
        raise RuntimeError("knowledge tool called synchronously on the event loop thread")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def matches(request: str) -> bool:
    """Knowledge asks that name something in the registry and don't ask for a
    live number. Sync and I/O-free: reads the resolver snapshot only."""
    if not TRIGGER.search(request or "") or _LIVE.search(request or ""):
        return False
    res = snapshot()
    if res is None or len(res) == 0:
        return False
    return any(r.entity.entity_type in ("protocol", "token") for r in res.mentions(request)) or any(
        (r := res.resolve(tok.lstrip("$"), context=request)) and r.confidence >= 0.75 and r.entity.entity_type == "protocol"
        for tok in re.findall(r"[A-Za-z$][A-Za-z0-9$.-]+", request)
    )


def knowledge_base_search(request: str) -> str:
    """Retrieve passages with citations for a protocol / concept question."""
    hits, plan = _run(_search_with_resolver(request))
    if not hits:
        raise RuntimeError("No indexed knowledge matched this question")
    context, citations = build_context(hits)
    entities = ", ".join(f"{r.entity.canonical_name} ({r.entity.entity_type}, {r.confidence:.2f})" for r in plan.entities[:6]) or "none resolved"
    lines = [
        "# Knowledge base",
        f"**Provider**: Dopamint knowledge service · **Entities**: {entities}" + (f" · **Graph**: {len(plan.graph_expanded)} related" if plan.graph_expanded else ""),
        "",
        "Answer from these passages and cite them as [n]. Do not add facts that are not in them; say what is missing.",
        "",
        context,
        "",
        "## Sources",
        *[f"[{c['n']}] {c['protocol'] + ' · ' if c['protocol'] else ''}{c['title']}{' › ' + c['heading'] if c['heading'] else ''} — {c['url']} ({'/'.join(c['sources'])})" for c in citations],
    ]
    return "\n".join(lines)


async def _search_with_resolver(request: str):
    return await search(request, limit=6, resolver=await resolver())


def get_embedder_name() -> str:
    from app.knowledge.embeddings import get_embedder

    return get_embedder().name


def reset() -> None:
    global _resolver_cache
    _resolver_cache = None
