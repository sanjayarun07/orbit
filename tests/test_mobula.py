"""Mobula: wallet tracking, portfolio analysis and meme-token rug risk.

User decision (2026-09-18): "use this explicitly for wallet tracking and
portfolio analysis related queries at high priority order", and "use this
for meme token complete in depth analysis". The fixtures here are the live
shapes those endpoints returned that day.
"""
import pytest

from app import mobula_security, mobula_wallet
from app.provider_registry import get_provider_router
from app.settings import settings

# conftest points these at a network guard; the card tests drive the real ones.
_REAL_SECURITY = mobula_security.token_security

WALLET = "0x6DbA597fe4bA47F97F1f0C32feEC4bf6Aea11460"
FARTCOIN = "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr(settings, "mobula_api_key", "test-key")


# --- which tool takes which wallet question -----------------------------------

@pytest.mark.parametrize("request_text,expected", [
    (f"{WALLET} wallet portfolio", "mobula_wallet_portfolio"),
    (f"what does {WALLET} hold", "mobula_wallet_portfolio"),
    (f"when did {WALLET} buy BONK", "mobula_wallet_history"),
    (f"{WALLET} recent activity", "mobula_wallet_history"),
    (f"is {WALLET} profitable", "mobula_wallet_analysis"),
    (f"pnl for {WALLET}", "mobula_wallet_analysis"),
])
def test_mobula_ranks_first_on_wallet_questions(request_text, expected):
    ranked = [t.name for t in get_provider_router()._ranked_union(request_text, ("wallet_intelligence", "portfolio"), (), None)]
    assert ranked and ranked[0] == expected, ranked


def test_a_question_with_no_address_or_no_wallet_words_claims_nothing():
    assert not mobula_wallet.matches("price of BONK")
    assert not mobula_wallet.matches("wallet portfolio")          # no address
    assert mobula_wallet.matches(f"{WALLET} portfolio")


def test_hyperliquid_perps_still_belong_to_goldrush():
    """Mobula indexes HyperEVM (chain 999) but not the perps clearinghouse."""
    ranked = [t.name for t in get_provider_router()._ranked_union(
        f"open perps for {WALLET} on hyperliquid", ("wallet_intelligence",), (), None)]
    assert "goldrush_hyperliquid_positions" in ranked


# --- the portfolio card -------------------------------------------------------

PORTFOLIO = {"total_wallet_balance": 12345.6, "assets": [
    {"asset": {"symbol": "HETF", "name": "Hyper ETF", "blockchains": ["Base"]}, "token_balance": 2034400.0,
     "price": 0.005, "estimated_balance": 10172.0, "price_change_24h": 3.5, "allocation": 82.4,
     "cross_chain_balances": {"Base": {"balance": 2034400.0}}},
    {"asset": {"symbol": "ETH", "name": "Ethereum", "blockchains": ["Arbitrum"]}, "token_balance": 0.8,
     "price": 2578.0, "estimated_balance": 2062.4, "price_change_24h": -1.2, "allocation": 16.7,
     "cross_chain_balances": {"Arbitrum": {"balance": 0.8}}},
    {"asset": {"symbol": "www.base1.cfd", "name": "www.base1.cfd - claim Your Base airdrop"}, "token_balance": 5000.0,
     "price": 0, "estimated_balance": 0},
    {"asset": {"symbol": "DUST", "name": "Dust"}, "token_balance": 1.0, "price": 0.1, "estimated_balance": 0.1},
]}


def test_the_portfolio_card_prices_holdings_and_hides_spam_and_dust(monkeypatch):
    monkeypatch.setattr(mobula_wallet, "_get", lambda url, params: PORTFOLIO)
    card = mobula_wallet.portfolio(f"{WALLET} wallet portfolio")
    assert card.startswith("# Wallet portfolio — 0x6DbA…1460") and "**Total**: $12.35K" in card
    assert "| HETF | Base | 2,034,400 | $0.005 | $10.17K | +3.5% | 82.4% |" in card
    assert "www.base1.cfd" not in card and "| DUST |" not in card
    assert "1 holding(s) under $1" in card and "1 spam/airdrop token(s)" in card
    assert card.index("HETF") < card.index("ETH"), "sorted by value"


# --- the history card ---------------------------------------------------------

def _tx(ts, symbol, kind="buy", amount=100.0, usd=250.0, chain="Base"):
    return {"timestamp": ts, "type": kind, "amount": amount, "amount_usd": usd, "blockchain": chain, "asset": {"symbol": symbol, "name": symbol}}


def test_the_history_card_dates_activity_and_drops_airdrop_spam(monkeypatch):
    calls = []

    def fake(url, params):
        calls.append(url)
        if url.endswith("/wallet/transactions"):
            return {"transactions": [_tx(1789000000000, "BONK"), _tx(1788000000000, "www.bopx.club 🟢", usd=0.0, chain="Plasma")]}
        return {"balance_usd": 12345.6, "balance_history": [[1780000000000, 900.0], [1789000000000, 12345.6]]}

    monkeypatch.setattr(mobula_wallet, "_get", fake)
    card = mobula_wallet.history(f"when did {WALLET} buy BONK")
    assert "**Transactions**: 1 (plus 1 spam/airdrop transfers, not shown)" in card
    assert "| buy | BONK |" in card and "www.bopx.club" not in card
    assert "## Portfolio value" in card and "$12.35K" in card
    assert any(u.endswith("/wallet/transactions") for u in calls) and any(u.endswith("/wallet/history") for u in calls)


def test_a_wallet_whose_every_transaction_is_spam_says_so(monkeypatch):
    monkeypatch.setattr(mobula_wallet, "_get", lambda url, params: (
        {"transactions": [_tx(1789000000000, "www.bipx.club 💵", usd=0.0)]} if url.endswith("/wallet/transactions") else {}))
    card = mobula_wallet.history(f"{WALLET} recent activity")
    assert "Every one of the 1 transactions Mobula indexed" in card and "airdrop-spam" in card


# --- the analysis card --------------------------------------------------------

def test_the_analysis_card_reports_pnl_and_where_trades_landed(monkeypatch):
    monkeypatch.setattr(mobula_wallet, "_get", lambda url, params: {
        "stat": {"totalValue": 12345.6, "periodTotalPnlUSD": 2100.0, "periodRealizedPnlUSD": 1500.0,
                 "periodRealizedRate": 0.62, "periodActiveTokensCount": 8, "periodWinCount": 5,
                 "fundingInfo": {"date": "2026-05-08T14:15:41.000Z", "chainId": "evm:42161", "formattedAmount": 0.5,
                                 "currency": {"symbol": "ETH"}}},
        "winRateDistribution": {">500%": 1, "0%-50%": 4, "<-50%": 0},
        "marketCapDistribution": {"<100k": 3, "10M-100M": 2},
        "labels": ["memecoin trader"]})
    card = mobula_wallet.analysis(f"is {WALLET} profitable")
    assert "**PnL over the period**: $2.10K total, $1.50K realized" in card
    assert "**Tokens traded**: 8 · **Winners**: 5" in card and "**Realized rate**: 62.0%" in card
    assert "| >500% | 1 |" in card and "| <100k | 3 |" in card
    assert "**First funded**: 2026-05-08 with 0.5 ETH on `evm:42161`" in card
    assert "**Labels**: memecoin trader" in card
    assert "| <-50% | 0 |" not in card, "empty bands are noise"


# --- meme-token rug risk ------------------------------------------------------

SECURITY = {
    "liquidityAnalysis": [
        {"poolAddress": "Bzc9NZfMqkXR6fz1DBph7BDf9BroyEf6pnzESP7v5iiw", "poolType": "raydium", "burnedPercentage": 0,
         "lockedPercentage": 99.78, "contractPercentage": 0, "unlockedPercentage": 0.21,
         "topHolders": [{"address": "Bzc9", "percentage": 99.78, "type": "locked", "protocol": "raydium permanent liquidity"}]},
        {"poolAddress": "Tuy6gMupGQ", "poolType": "orca", "burnedPercentage": 0, "lockedPercentage": 0,
         "contractPercentage": 0, "unlockedPercentage": 100, "topHolders": []},
    ],
    "top10HoldingsPercentage": 11.29, "top50HoldingsPercentage": 25.94, "top100HoldingsPercentage": 30.49,
    "burnedHoldingsPercentage": 0.0, "contractHoldingsPercentage": 1.2, "proTraderVolume24hPercentage": 0.52,
    "isHoneypot": None, "isMintable": False, "isFreezable": False, "transferPausable": False, "renounced": None,
    "balanceMutable": False, "buyFeePercentage": 0.25, "sellFeePercentage": 0.25, "transferFeePercentage": 0,
    "isLaunchpadToken": True,
}


@pytest.mark.parametrize("request_text,address,chain", [
    (f"is {FARTCOIN} safe on solana", FARTCOIN, "solana"),
    (f"{FARTCOIN} rug check", FARTCOIN, "solana"),
    ("0x6982508145454ce325ddbe47a25d4ec3d2311933 on ethereum", "0x6982508145454ce325ddbe47a25d4ec3d2311933", "ethereum"),
    ("0x6982508145454ce325ddbe47a25d4ec3d2311933 on bsc", "0x6982508145454ce325ddbe47a25d4ec3d2311933", "bnb smart chain (bep20)"),
])
def test_the_subject_and_chain_it_asks_about(request_text, address, chain):
    assert mobula_security._subject(request_text) == (address, chain)


def test_the_security_card_names_the_pullable_pool(monkeypatch):
    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": SECURITY}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            assert params["blockchain"] == "solana" and params["address"] == FARTCOIN
            return Response()

    monkeypatch.setattr(mobula_security.httpx, "Client", Client)
    card = _REAL_SECURITY(f"is {FARTCOIN} safe on solana")
    assert "| raydium | 0.00% | 99.78% | 0.00% | 0.21% | locked (raydium permanent liquidity) |" in card
    assert "| orca | 0.00% | 0.00% | 0.00% | 100.00% | **pullable** |" in card
    assert "Worst pool here is 100.00% unlocked" in card
    assert "**Top 10**: 11.29% of supply" in card and "**Launchpad token**: yes" in card
    assert "**Mintable**: no" in card and "**Honeypot**: not reported" in card
    assert "risk read, not an audit" in card


@pytest.mark.parametrize("pool,verdict", [
    ({"burnedPercentage": 99.8, "lockedPercentage": 0, "unlockedPercentage": 0.2, "contractPercentage": 0}, "burned or locked"),
    ({"burnedPercentage": 0, "lockedPercentage": 99.8, "unlockedPercentage": 0.2, "contractPercentage": 0}, "locked"),
    ({"burnedPercentage": 0, "lockedPercentage": 0, "unlockedPercentage": 100, "contractPercentage": 0}, "**pullable**"),
    ({"burnedPercentage": 0, "lockedPercentage": 0, "unlockedPercentage": 10, "contractPercentage": 90}, "held by an unidentified contract"),
    ({"burnedPercentage": 30, "lockedPercentage": 20, "unlockedPercentage": 45, "contractPercentage": 5}, "mixed"),
])
def test_how_a_pool_is_read(pool, verdict):
    assert mobula_security._pool_verdict(pool) == verdict
