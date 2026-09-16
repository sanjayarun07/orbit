"""The tool catalogue: every registered tool documents what its API can and
cannot answer, and the router scores by it -- so a request for one
dimension (a volume ranking) is never handed to a tool built for another
(paid boosts) because their keywords overlap."""
import pytest

from app.provider_registry import get_provider_router
from app.tool_catalog import DIMENSION_PATTERNS, TOOL_SPECS, asked_dimensions, catalog_markdown


def test_every_registered_tool_has_a_spec_and_every_spec_names_a_registered_tool():
    names = {tool.name for tool in get_provider_router().tools()}
    assert names == set(TOOL_SPECS), (names ^ set(TOOL_SPECS))
    for tool in get_provider_router().tools():
        assert tool.spec is TOOL_SPECS[tool.name] and tool.description   # the semantic fallback now covers every tool


def test_specs_are_internally_consistent():
    for spec in TOOL_SPECS.values():
        assert spec.dimensions <= set(DIMENSION_PATTERNS), spec.name
        assert spec.not_for <= set(DIMENSION_PATTERNS), spec.name
        assert not (spec.dimensions & spec.not_for), spec.name
        assert spec.api and spec.endpoints and spec.returns and spec.summary, spec.name


@pytest.mark.parametrize("request_text, expected", [
    ("trending tokens by volume in 24hrs", {"volume"}),
    ("top gainers on solana", {"price_change"}),
    ("boosted tokens on base", {"boosts"}),
    ("new pairs on base", {"new_listings"}),
    ("top holders of BONK", {"holders"}),
    ("is this token safe", {"security"}),
    ("how does Aave E-mode work", {"docs"}),
    ("who invested in EigenLayer", {"vcs"}),
    ("price of SOL", set()),
])
def test_asked_dimensions(request_text, expected):
    assert asked_dimensions(request_text) == frozenset(expected)


def test_a_volume_ranking_outscores_the_boosts_tool_even_with_the_same_keywords():
    boosts, volume = TOOL_SPECS["dexscreener_boosted_tokens"], TOOL_SPECS["coingecko_top_volume"]
    ask = "trending tokens by volume in 24hrs"
    assert volume.fit(ask) > 0 > boosts.fit(ask)
    # ...and the other way round for an attention ask.
    ask = "boosted tokens on solana"
    assert boosts.fit(ask) > 0 > volume.fit(ask)
    # A request with no dimension is neutral for everyone: the regex gate and priority decide.
    assert boosts.fit("price of SOL") == volume.fit("price of SOL") == 0.0


@pytest.mark.parametrize("request_text, capability, winner", [
    ("trending tokens by volume in 24hrs", "token_discovery", "coingecko_top_volume"),
    ("top gainers on solana", "token_discovery", "coingecko_gainers_losers"),
    ("trending tokens on pump.fun", "token_discovery", "geckoterminal_pools"),      # real pools outrank paid boosts
    ("boosted tokens on solana", "token_discovery", "dexscreener_boosted_tokens"),
    ("new pairs on base", "token_discovery", "geckoterminal_pools"),
    ("what narratives are trending", "token_discovery", "dexscreener_trending_metas"),
])
def test_router_picks_the_tool_whose_api_answers_the_asked_dimension(request_text, capability, winner):
    names = [tool.name for tool in get_provider_router().candidates(request_text, capability)]
    assert names and names[0] == winner, (request_text, names[:4])


def test_catalog_renders_every_tool():
    doc = catalog_markdown(get_provider_router())
    for name in TOOL_SPECS:
        assert f"## {name}" in doc
