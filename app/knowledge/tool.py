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
    r"compete|competitors?|alternatives?\s+to|integrat(?:e|es|ion)\s+with|deployed\s+on|supports?|"
    r"hack(?:ed|s)?|exploit(?:ed|s)?|incidents?|audit(?:ed|s)?|investors?|backed\s+by|raised|funding|fork(?:ed)?\s+(?:of|from)|oracles?|proposals?|voted?|"
    r"backs|backing|peg(?:ged|s)?|depeg(?:ged|s)?|reserves|stablecoins?|redeem(?:able|ed)?)\b",
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


# Whether to buy, hold or ape into a token is not a documentation question. An
# incident in the registry ("BONK Fun exploit") made "what are the risks of
# buying BONK?" a knowledge match (review of 2026-09-18); the answer was that
# the passages say nothing about BONK. Such asks belong to the token tools.
_OWNERSHIP_RISK = re.compile(
    r"\b(?:risks?|risky|safe|safety|rug|honeypot|scam|legit)\b.{0,40}\b(?:buy|buying|hold|holding|own|owning|invest|investing|ape|aping)\b"
    r"|\b(?:buy|buying|hold|holding|invest|investing|ape|aping)\b.{0,40}\b(?:risks?|risky|safe|safety|rug|honeypot|scam|legit)\b",
    re.IGNORECASE,
)


def matches(request: str) -> bool:
    """Knowledge asks that name something in the registry and don't ask for a
    live number, or whether to own a token. Sync and I/O-free: reads the
    resolver snapshot only."""
    if not TRIGGER.search(request or "") or _LIVE.search(request or "") or _OWNERSHIP_RISK.search(request or ""):
        return False
    res = snapshot()
    if res is None or len(res) == 0:
        return False
    return any(r.entity.entity_type in ("protocol", "token", "incident") for r in res.mentions(request)) or any(
        (r := res.resolve(tok.lstrip("$"), context=request)) and r.confidence >= 0.75 and r.entity.entity_type == "protocol"
        for tok in re.findall(r"[A-Za-z$][A-Za-z0-9$.-]+", request)
    )


def _on_topic(hits, plan) -> bool:
    """Whether the passages are about what the question named. Retrieval
    always returns its nearest passages; when the question resolved to an
    entity none of them mention, they are the nearest passages about
    something else (Lombard and Spark risk sections for a BONK question) and
    an answer built on them can only say the passages do not cover it."""
    entities = [e.entity for e in getattr(plan, "entities", []) or []]
    if not entities:
        return True
    names = {n.lower() for e in entities for n in (e.canonical_name, e.symbol or "") if n}
    for hit in hits:
        haystack = " ".join([hit.protocol_name or "", hit.document_title or "", getattr(hit.chunk, "heading", "") or "", hit.chunk.content or ""]).lower()
        if any(name in haystack for name in names):
            return True
    return False


_PASSAGE_CHARS = 700
_PASSAGES_CHARS = 3500


def _compact_passages(context: str) -> str:
    """The numbered passages, each cut at a sentence boundary to a readable
    length, the whole block bounded: the card is read on a phone under the
    answer, not only by the model."""
    out, used = [], 0
    for block in re.split(r"\n\s*\n", context or ""):
        block = block.strip()
        if not block:
            continue
        head, _, body = block.partition("\n")
        body = " ".join(body.split())
        if len(body) > _PASSAGE_CHARS:
            cut = body[:_PASSAGE_CHARS]
            end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
            body = (cut[:end + 1] if end > _PASSAGE_CHARS // 2 else cut.rsplit(" ", 1)[0]) + " …"
        passage = f"**{head}**\n{body}"
        if used + len(passage) > _PASSAGES_CHARS and out:
            out.append("_Further passages omitted; the sources list them._")
            break
        out.append(passage)
        used += len(passage)
    return "\n\n".join(out)


_STOP_WORDS = {"what", "which", "when", "where", "does", "do", "how", "why", "the", "and", "for", "with", "that", "this", "from", "into",
               "about", "change", "changes", "work", "works", "mean", "means", "have", "has", "are", "is", "its", "their", "your", "explain"}


def _question_terms(request: str) -> set[str]:
    """The question's content words: what a relevant passage has to mention."""
    words = re.findall(r"[a-z0-9][a-z0-9-]{2,}", (request or "").lower())
    return {w for w in words if w not in _STOP_WORDS and not w.isdigit()}


def _rank_hits(hits, request: str):
    """The passages that mention the question's own terms first, and only
    those when any do: an E-mode question showed passages about liquidation
    fees and interest rates because retrieval ranks by the protocol, not the
    ask (UI review, 2026-09-24). Retrieval order breaks ties."""
    terms = _question_terms(request)
    if not terms:
        return list(hits)
    scored = []
    for index, hit in enumerate(hits):
        haystack = " ".join([hit.document_title or "", getattr(hit.chunk, "heading", "") or "", hit.chunk.content or ""]).lower()
        scored.append((sum(1 for t in terms if t in haystack), -index, hit))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    relevant = [hit for score, _, hit in scored if score > 0]
    return relevant or [hit for _, _, hit in scored]


def _uncovered_terms(hits, request: str, plan) -> list[str]:
    """The question's own terms that no passage mentions, the entity's name
    aside: the card says "not in these passages: e-mode" instead of letting
    fee passages stand in for an E-mode answer (UI review, 2026-09-24)."""
    names = {w for e in getattr(plan, "entities", []) or [] for n in (e.entity.canonical_name, e.entity.symbol or "") for w in re.findall(r"[a-z0-9-]+", (n or "").lower())}
    haystack = " ".join(" ".join([h.document_title or "", getattr(h.chunk, "heading", "") or "", h.chunk.content or ""]) for h in hits).lower()
    return sorted(t for t in _question_terms(request) if t not in names and t not in haystack)


def knowledge_base_search(request: str) -> str:
    """Retrieve passages with citations for a protocol / concept question."""
    hits, plan = _run(_search_with_resolver(request))
    if not hits or not _on_topic(hits, plan):
        # No output means the router moves on to the next tool; a citation
        # list that cannot mention the subject is not an answer.
        raise RuntimeError("No indexed knowledge matched this question")
    uncovered = _uncovered_terms(hits, request, plan)
    context, citations = build_context(_rank_hits(hits, request))
    entities = ", ".join(f"{r.entity.canonical_name} ({r.entity.entity_type}, {r.confidence:.2f})" for r in plan.entities[:6]) or "none resolved"
    # The card is what the user sees as well as what the model reads: the
    # passages, each cut to a readable length, and the sources -- never an
    # instruction to the model (UI review, 2026-09-23: "Answer from these
    # passages…" and pages of raw text showed under a short answer). The
    # citing rule lives in the synthesis prompts.
    passages = _compact_passages(context)
    lines = [
        "# Knowledge base",
        f"**Provider**: Dopamint knowledge service · **Entities**: {entities}" + (f" · **Graph**: {len(plan.graph_expanded)} related" if plan.graph_expanded else ""),
        "",
        *([f"**Not in these passages**: {', '.join(uncovered)}. The passages below are about {plan.entities[0].entity.canonical_name if getattr(plan, 'entities', None) else 'the protocol'} "
           "but not that; an answer can say so, never fill it in."] if uncovered else []),
        "## Passages",
        passages,
        "",
        "## Sources",
        *[f"[{c['n']}] [{c['title']}{' › ' + c['heading'] if c['heading'] else ''}]({c['url']})" + (f" · {c['protocol']}" if c['protocol'] else "") + f" ({'/'.join(c['sources'])})" for c in citations],
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
