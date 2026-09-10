from app.wallet_insights import portfolio_scenario, wallet_health


SNAPSHOT = {
    "wallet": "wallet",
    "sol": {"amount": 0.001, "usd_value": 1.0, "allocation_pct": 10},
    "holdings": [
        {"symbol": "ANSEM", "usd_value": 9.0, "allocation_pct": 90, "verified": False},
    ],
    "total_usd_value": 10.0,
    "unpriced_holdings": 0,
}


def test_wallet_health_finds_gas_verification_and_concentration():
    result = wallet_health(SNAPSHOT)
    assert result["status"] == "attention"
    assert {item["title"] for item in result["findings"]} == {"Low Solana gas", "Unverified assets", "Concentrated portfolio"}


def test_portfolio_scenario_keeps_quantities_and_shocks_selected_asset():
    result = portfolio_scenario(SNAPSHOT, -50, "ANSEM")
    assert result["current_total_usd"] == 10
    assert result["projected_total_usd"] == 5.5
    assert result["portfolio_change_usd"] == -4.5
