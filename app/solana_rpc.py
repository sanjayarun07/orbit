import httpx

from app.settings import settings


async def rpc(method: str, params: list) -> dict:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(settings.solana_rpc_url, json=body)
        response.raise_for_status()
        result = response.json()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]


async def get_sol_balance(wallet_address: str) -> dict:
    """Return the wallet's SOL balance. This is read-only."""
    result = await rpc("getBalance", [wallet_address, {"commitment": "confirmed"}])
    lamports = int(result["value"])
    return {"wallet": wallet_address, "lamports": lamports, "sol": lamports / 1_000_000_000}


async def get_token_accounts(wallet_address: str) -> dict:
    """Return parsed SPL token accounts and balances for a wallet. This is read-only."""
    return await rpc(
        "getTokenAccountsByOwner",
        [
            wallet_address,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed", "commitment": "confirmed"},
        ],
    )


async def simulate_transaction(transaction_base64: str) -> dict:
    """Simulate a serialized transaction without signature verification."""
    result = await rpc(
        "simulateTransaction",
        [
            transaction_base64,
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
            },
        ],
    )
    value = result.get("value", {})
    return {
        "ok": value.get("err") is None,
        "error": value.get("err"),
        "units_consumed": value.get("unitsConsumed"),
        "logs": value.get("logs", [])[-12:],
    }
