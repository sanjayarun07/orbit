import httpx

from app import token_resolve
from app.context_entities import resolve_pending_token, resolve_contextual_request
from app.settings import settings


_SEARCH = {
    "pairs": [
        # Two Base pairs of the same token -> deduped, max liquidity kept.
        {"chainId": "base", "baseToken": {"symbol": "PEPE", "address": "0xBASE", "name": "Pepe"}, "liquidity": {"usd": 90_000}},
        {"chainId": "base", "baseToken": {"symbol": "PEPE", "address": "0xBASE", "name": "Pepe"}, "liquidity": {"usd": 120_000}},
        {"chainId": "ethereum", "baseToken": {"symbol": "PEPE", "address": "0xETH", "name": "Pepe"}, "liquidity": {"usd": 310_000_000}},
        # A different symbol in the same search response is ignored.
        {"chainId": "solana", "baseToken": {"symbol": "PEPECOIN", "address": "Sol1", "name": "x"}, "liquidity": {"usd": 5_000}},
        # Missing address is skipped.
        {"chainId": "bsc", "baseToken": {"symbol": "PEPE", "address": "", "name": "Pepe"}, "liquidity": {"usd": 1}},
    ]
}


def test_token_candidates_dedupes_ranks_and_filters(monkeypatch):
    monkeypatch.setattr(token_resolve, "_get", lambda path, params: _SEARCH)
    cands = token_resolve.token_candidates("PEPE")
    # Deduped to two tokens (Base summed 90k+120k=210k), richest first, others dropped.
    assert [(c["chain"], c["address"]) for c in cands] == [("ethereum", "0xETH"), ("base", "0xBASE")]
    assert cands[1]["liquidity_usd"] == 210_000
    # Chain filter narrows to the requested chain only.
    only_base = token_resolve.token_candidates("PEPE", ("base",))
    assert [c["chain"] for c in only_base] == ["base"]


def test_token_candidates_matches_symbol_with_dollar_prefix(monkeypatch):
    monkeypatch.setattr(token_resolve, "_get", lambda path, params: _SEARCH)
    assert token_resolve.token_candidates("$PEPE")  # leading $ stripped


def test_clear_winner_single_and_dominant():
    single = [{"chain": "solana", "address": "a", "liquidity_usd": 100}]
    assert token_resolve.clear_winner(single)["chain"] == "solana"
    dominant = [
        {"chain": "solana", "address": "a", "liquidity_usd": 5_000_000},
        {"chain": "base", "address": "b", "liquidity_usd": 50_000},
    ]
    assert token_resolve.clear_winner(dominant)["chain"] == "solana"


def test_clear_winner_none_when_comparable_or_below_floor():
    comparable = [
        {"chain": "ethereum", "address": "a", "liquidity_usd": 310_000_000},
        {"chain": "base", "address": "b", "liquidity_usd": 120_000_000},
    ]
    assert token_resolve.clear_winner(comparable) is None
    # Top dominates by ratio but is under the floor -> still ambiguous.
    thin = [
        {"chain": "base", "address": "a", "liquidity_usd": 40_000},
        {"chain": "solana", "address": "b", "liquidity_usd": 10},
    ]
    assert token_resolve.clear_winner(thin) is None
    assert token_resolve.clear_winner([]) is None


# ---- Bitquery EVM resolver ----

class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, *a, **k):
        return _FakeResp(self._payload)


def test_bitquery_evm_candidates_ranks_and_filters_dust(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "k")
    payload = {"data": {"EVM": {"DEXTradeByTokens": [
        {"Trade": {"Currency": {"Name": "Pepe", "Symbol": "PEPE", "SmartContract": "0x6982"}}, "vol": 3_866_052, "traders": 120},
        {"Trade": {"Currency": {"Symbol": "PEPE", "SmartContract": "0xdead"}}, "vol": 4656, "traders": 3},  # < min traders -> dropped
        {"Trade": {"Currency": {"Symbol": "OTHER", "SmartContract": "0xbeef"}}, "vol": 9e9, "traders": 999},  # wrong symbol -> dropped
    ]}}}
    monkeypatch.setattr(token_resolve.httpx, "Client", lambda *a, **k: _FakeClient(payload))
    out = token_resolve.bitquery_evm_candidates("PEPE", "ethereum")
    assert [(c["address"], c["traders"]) for c in out] == [("0x6982", 120)]
    assert out[0]["liquidity_usd"] == 3_866_052


def test_bitquery_evm_candidates_empty_without_key_or_chain(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "")
    assert token_resolve.bitquery_evm_candidates("PEPE", "ethereum") == []
    monkeypatch.setattr(settings, "bitquery_api_key", "k")
    assert token_resolve.bitquery_evm_candidates("PEPE", "solana") == []  # not an EVM chain


def test_bitquery_evm_candidates_swallows_errors(monkeypatch):
    monkeypatch.setattr(settings, "bitquery_api_key", "k")
    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(token_resolve.httpx, "Client", boom)
    assert token_resolve.bitquery_evm_candidates("PEPE", "base") == []


# ---- follow-up disambiguation ----

_ETH_ADDR = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
_BASE_ADDR = "0x6921b130d297cc43754afba22e5eac0fbf8db75b"
_PENDING = {
    "original_request": "top holders of PEPE",
    "symbol": "PEPE",
    "candidates": [
        {"chain": "ethereum", "address": _ETH_ADDR, "symbol": "PEPE", "liquidity_usd": 3.1e8},
        {"chain": "base", "address": _BASE_ADDR, "symbol": "PEPE", "liquidity_usd": 1.2e8},
    ],
}


def test_resolve_pending_token_by_chain():
    assert resolve_pending_token("Base", _PENDING) == f"top holders of PEPE {_BASE_ADDR} on base"


def test_resolve_pending_token_by_ordinal():
    assert resolve_pending_token("the second one", _PENDING) == f"top holders of PEPE {_BASE_ADDR} on base"
    assert resolve_pending_token("1", _PENDING) == f"top holders of PEPE {_ETH_ADDR} on ethereum"


def test_resolve_pending_token_by_pasted_address():
    assert resolve_pending_token(_BASE_ADDR, _PENDING) == f"top holders of PEPE {_BASE_ADDR} on base"


def test_resolve_pending_token_no_match_returns_none():
    # An unrelated question names no candidate -> treated as a fresh query.
    assert resolve_pending_token("what's trending on solana today?", _PENDING) is None


def test_resolve_contextual_request_consumes_pending_before_history():
    out = resolve_contextual_request("Base", "", {"pending_token": _PENDING})
    assert out == f"top holders of PEPE {_BASE_ADDR} on base"


def test_bitquery_evm_lookup_distinguishes_no_token_from_no_answer(monkeypatch):
    """`answered` lets the resolver trust an empty result ("no real token on
    this chain") but fall back when Bitquery could not be asked at all."""
    monkeypatch.setattr(token_resolve.settings, "bitquery_api_key", None)
    assert token_resolve.bitquery_evm_lookup("PEPE", "ethereum") == ([], False)      # unconfigured
    monkeypatch.setattr(token_resolve.settings, "bitquery_api_key", "k")
    assert token_resolve.bitquery_evm_lookup("PEPE", "solana") == ([], False)        # not an EVM chain

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp(self._payload)

    monkeypatch.setattr(token_resolve.httpx, "Client", lambda **k: _Client({"data": {"EVM": {"DEXTradeByTokens": []}}}))
    assert token_resolve.bitquery_evm_lookup("BONK", "ethereum") == ([], True)       # answered: nothing real
    monkeypatch.setattr(token_resolve.httpx, "Client", lambda **k: _Client({"errors": [{"message": "402 credits"}]}))
    assert token_resolve.bitquery_evm_lookup("BONK", "ethereum") == ([], False)      # errored: no verdict
