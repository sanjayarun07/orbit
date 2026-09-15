from datetime import datetime, timezone

from app import answer_validator
from app.answer_validator import validate_answer


def _traj(tool, observation):
    return {"tool_name_0": tool, "tool_args_0": {}, "observation_0": observation}


def _now_stamp(minutes_ago=0):
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M UTC")


def _check(validation, name):
    return next(c for c in validation.checks if c.name == name)


def test_none_for_plain_general_answer():
    assert validate_answer("hi there", "Hello! How can I help?", None, "general") is None


def test_fresh_attributed_grounded_answer_is_ok():
    stamp = _now_stamp(2)
    answer = (
        f"# Top token holders\n**Provider**: Bitquery · **Data freshness**: {stamp} · **Token**: Bonk on Solana\n"
        "| # | Holder | Balance |\n| 1 | AST…JZ | 350,787,248,145 |"
    )
    traj = _traj("bitquery_token_top_holders", "Holder AST…JZ balance 350,787,248,145")
    v = validate_answer("top holders of BONK", answer, traj, "research")
    assert v is not None and v.status == "ok"
    assert _check(v, "provenance").status == "ok"
    assert any(s.lower() == "bitquery" for s in v.sources)
    assert _check(v, "freshness").status == "ok"
    assert _check(v, "grounding").status == "ok"


def test_stale_data_warns_on_freshness():
    stamp = _now_stamp(120)  # two hours old
    answer = f"**Provider**: GeckoTerminal · **Data freshness**: {stamp}\nSOL price is 101.38"
    traj = _traj("geckoterminal_pools", "SOL 101.38")
    v = validate_answer("what's the current SOL price", answer, traj, "research")
    fresh = _check(v, "freshness")
    assert fresh.status == "warn" and "old" in fresh.detail
    assert v.status == "warn"
    assert v.age_minutes is not None and v.age_minutes > 60


def test_time_sensitive_without_timestamp_warns():
    answer = "SOL is trading around $101 with strong momentum."
    v = validate_answer("what is the current price of SOL", answer, None, "research")
    assert _check(v, "freshness").status == "warn"


def test_figures_without_evidence_warn_on_grounding_and_provenance():
    answer = "SOL is at $59.48B market cap with 12,345,678 holders."  # no trajectory, no source
    v = validate_answer("tell me about SOL", answer, None, "research")
    assert _check(v, "grounding").status == "warn"
    assert _check(v, "provenance").status == "warn"
    assert v.status == "warn"


def test_grounding_conservative_when_most_figures_match(monkeypatch):
    # 3 of 4 figures present in the observation -> grounded (below the warn bar).
    answer = "Price $101.38, mcap $59.48B, volume 1,200,000, and a projected $999.99."
    obs = "price 101.38 marketcap 59.48 billion volume 1,200,000 traded today"
    traj = _traj("dexscreener_pair_search", obs)
    v = validate_answer("price of SOL now", answer, traj, "research")
    assert _check(v, "grounding").status == "ok"


def test_explanation_with_no_data_has_no_warnings():
    answer = "Relay is a routing and execution layer that bridges assets across chains."
    v = validate_answer("how does relay work?", answer, _traj("perplexity_web_search", "Relay bridges..."), "research")
    # Data intent via trajectory, but no numeric claims and not time-sensitive.
    assert v.status == "ok"
    assert _check(v, "grounding").status == "not_applicable"
    assert _check(v, "freshness").status == "not_applicable"


def test_cited_source_without_provider_tag_counts_as_provenance():
    answer = "TVL is 12,345,678. Source: [DeFiLlama](https://defillama.com)"
    traj = _traj("defillama_chain_tvl", "TVL 12,345,678")
    v = validate_answer("tvl on solana", answer, traj, "research")
    assert _check(v, "provenance").status == "ok"


# ---- cross-source consistency ----

_MINT = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def _two_source_traj(obs_a, obs_b, tool_a="dexscreener_pair_search", tool_b="geckoterminal_pools"):
    return {
        "tool_name_0": tool_a, "observation_0": obs_a,
        "tool_name_1": tool_b, "observation_1": obs_b,
    }


def test_consistency_agree_across_two_providers_same_token():
    a = f"**Token**: {_MINT}\n**Liquidity**: $2.0M\n**Price (USD)**: $0.000010"
    b = f"**Token**: {_MINT}\n**Liquidity**: $2.1M\n**Price (USD)**: $0.0000102"
    answer = f"**Data freshness**: {_now_stamp(1)}\nPrice ~$0.00001, liquidity ~$2M for token {_MINT}."
    v = validate_answer("price and liquidity of that token", answer, _two_source_traj(a, b), "research")
    assert _check(v, "consistency").status == "ok"
    assert v.status == "ok"


def test_consistency_warns_on_material_disagreement_same_token():
    a = f"**Token**: {_MINT}\n**Liquidity**: $2.0M"
    b = f"**Token**: {_MINT}\n**Liquidity**: $9.0M"   # 78% higher -> flag
    v = validate_answer("liquidity of that token", "Liquidity is a few million.", _two_source_traj(a, b), "research")
    cons = _check(v, "consistency")
    assert cons.status == "warn" and "liquidity" in cons.detail
    assert v.status == "warn"


def test_consistency_not_applicable_when_tokens_differ():
    a = "**Token**: 0x6982508145454ce325ddbe47a25d4ec3d2311933\n**Liquidity**: $2.0M"
    b = f"**Token**: {_MINT}\n**Liquidity**: $9.0M"   # different tokens -> never compared
    v = validate_answer("liquidity", "Some liquidity.", _two_source_traj(a, b), "research")
    assert _check(v, "consistency").status == "not_applicable"


def test_consistency_sees_nested_team_trajectory():
    # The team desk nests sub-trajectories under a key -> the scan descends in.
    nested = {"market_research": {
        "tool_name_0": "dexscreener_pair_search", "observation_0": f"**Token**: {_MINT}\n**Liquidity**: $2.0M",
        "tool_name_1": "geckoterminal_pools", "observation_1": f"**Token**: {_MINT}\n**Liquidity**: $9.0M",
    }}
    v = validate_answer("liquidity of that token", "A few million in liquidity.", nested, "team")
    assert _check(v, "consistency").status == "warn"


def test_consistency_not_applicable_single_source():
    a = f"**Token**: {_MINT}\n**Liquidity**: $2.0M\n**Price (USD)**: $0.00001"
    v = validate_answer("liquidity of that token", "Liquidity ~$2M.", _traj("dexscreener_pair_search", a), "research")
    assert _check(v, "consistency").status == "not_applicable"


def test_sources_list_sees_nested_team_trajectory():
    nested = {"market_research": _traj("dexscreener_pair_search", f"**Token**: {_MINT}\n**Liquidity**: $2.0M")}
    v = validate_answer("liquidity of that token", "Liquidity ~$2M.", nested, "team")
    assert "dexscreener" in v.sources
