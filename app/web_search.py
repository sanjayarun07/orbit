"""General-purpose live search via OpenAI's hosted Responses API tool.

Uses the existing OPENAI_API_KEY; no separate search API key required.
"""

from datetime import datetime
import re
from urllib.parse import urlparse

from openai import OpenAI

from app.call_budget import charge_and_check
from app.metrics import increment
from app.settings import settings
from app.tool_results import compact_tool_result, strip_inline_citation_markers

_client: OpenAI | None = None
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")
_BARE_LINK = re.compile(r"(?<!\()https?://[^\s<>]+")
_CRYPTO_TRENDS = re.compile(
    r"\b(?:trending|trend|trends|moving|hot|narratives?)\b.*\b(?:crypto|web3|tokens?|coins?|market)\b|"
    r"\b(?:crypto|web3)\b.*\b(?:trending|trend|trends|moving|hot|right now)\b",
    re.IGNORECASE,
)


def is_crypto_trends_query(query: str) -> bool:
    return bool(_CRYPTO_TRENDS.search(query))


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.openai_api_key)
    return _client


def _openai_web_search(query: str) -> str:
    """Search the live web for current information on any topic.

    Returns a grounded answer plus a compact source list so callers can expose
    verifiable links rather than presenting a citation-free summary.
    """
    today = datetime.now().astimezone().date().isoformat()
    if is_crypto_trends_query(query):
        task = (
            "Create a concise, decision-useful crypto market brief for this request: "
            f"{query}\n\n"
            "Prioritize: (1) market regime and BTC/ETH context, (2) current on-chain "
            "activity including DEX volume or growth by major chains, (3) protocols, "
            "launchpads and narratives receiving measurable attention, and (4) a few "
            "high-attention tokens only when supported by current volume, flows, or "
            "other quantitative evidence. Label paid/boosted or very new tokens as "
            "speculative. Include the measurement window and data timestamp. Put a "
            "clickable source beside every quantitative claim. Prefer CoinGecko, "
            "DefiLlama, DEX Screener, official project data and reputable reporting. "
            "Exclude unrelated crime, court, botnet, and generic educational stories "
            "unless they are materially moving the market. Do not include videos. "
            "Do not imply access to the user's wallet. End with a short risk section."
        )
    else:
        task = (
            f"Search the live web and answer this request: {query}\n\n"
            "Use authoritative, recent sources. For news, separate the date an "
            "event happened from the date an article was published. Include "
            "clickable source links in the answer."
        )
    if not charge_and_check(settings.openai_web_search_cost_usd):
        # This is the ReAct-loop's hosted web-search fallback -- unlike
        # MCPGateway.call()/ProviderRouter.route(), it has no existing
        # raise-on-failure convention (it always just returns text), so
        # match that style: a short, honestly-labeled string the calling
        # agent can reason about, not an exception.
        increment("openai_web_search_budget_skips")
        return "Web search unavailable: per-turn data budget reached."
    response = _get_client().responses.create(
        model=settings.openai_web_search_model,
        tools=[{"type": "web_search"}],
        tool_choice="required",
        include=["web_search_call.action.sources"],
        input=(
            f"Today is {today}. {task}"
        ),
    )
    sources: list[tuple[str, str]] = []
    for item in response.output:
        if getattr(item, "type", None) != "web_search_call":
            continue
        action = getattr(item, "action", None)
        for source in getattr(action, "sources", None) or []:
            url = getattr(source, "url", None)
            if not url:
                continue
            title = getattr(source, "title", None) or url
            if all(existing_url != url for _, existing_url in sources):
                sources.append((title, url))

    answer = _dedupe_response(strip_inline_citation_markers(response.output_text))
    if sources and not any(url in answer for _, url in sources):
        links = "\n".join(f"- [{title}]({url})" for title, url in sources[:8])
        answer = f"{answer}\n\nSources:\n{links}"
    return compact_tool_result(answer)


def web_search(query: str) -> str:
    """Use Perplexity when configured, retaining OpenAI hosted search fallback."""
    if settings.perplexity_api_key:
        from app.perplexity_tools import perplexity_web_search

        return perplexity_web_search(query)
    return _openai_web_search(query)


def _dedupe_response(value: str) -> str:
    """Remove exact repeated response blocks occasionally emitted by providers."""
    text = value.strip()
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) >= 2 and len(paragraphs) % 2 == 0:
        middle = len(paragraphs) // 2
        if paragraphs[:middle] == paragraphs[middle:]:
            return "\n\n".join(paragraphs[:middle])
    return text


def append_web_sources(answer: str, trajectory: dict | None) -> str:
    """Keep web citations visible if an agent summary drops its tool links."""
    if not trajectory:
        return answer

    sources: list[tuple[str, str]] = []
    index = 0
    while f"tool_name_{index}" in trajectory:
        if trajectory.get(f"tool_name_{index}") in {
            "web_search",
            "perplexity_web_search",
            "perplexity_finance_search",
            "semantic_cache",
        }:
            observation = str(trajectory.get(f"observation_{index}") or "")
            for title, url in _MARKDOWN_LINK.findall(observation):
                if all(existing_url != url for _, existing_url in sources):
                    sources.append((title, url))
            for match in _BARE_LINK.findall(observation):
                url = match.rstrip(".,;:!?)]")
                if all(existing_url != url for _, existing_url in sources):
                    sources.append((urlparse(url).netloc or "Source", url))
        index += 1

    missing = [(title, url) for title, url in sources if f"]({url})" not in answer]
    if not missing:
        return answer
    links = "\n".join(f"- [{title}]({url})" for title, url in missing[:8])
    return f"{answer.rstrip()}\n\nSources:\n{links}"
