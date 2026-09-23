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


TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


async def get_token_accounts_2022(wallet_address: str) -> dict:
    """Parsed Token-2022 accounts for a wallet (pump.fun-era mints live here)."""
    return await rpc("getTokenAccountsByOwner", [wallet_address, {"programId": TOKEN_2022_PROGRAM}, {"encoding": "jsonParsed", "commitment": "confirmed"}])


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


async def get_token_largest_accounts(mint: str) -> list[dict]:
    """The 20 largest token accounts for a mint (`getTokenLargestAccounts`).
    Read-only, keyless; entries carry `address`, `amount`, `uiAmount`."""
    result = await rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
    return list(result.get("value") or [])


async def get_token_supply(mint: str) -> dict:
    """Total supply for a mint (`getTokenSupply`): `amount`, `decimals`, `uiAmount`."""
    result = await rpc("getTokenSupply", [mint, {"commitment": "confirmed"}])
    return dict(result.get("value") or {})


async def get_account_owners(token_accounts: list[str]) -> dict[str, str]:
    """token account -> owner wallet, via one `getMultipleAccounts` (jsonParsed).
    Accounts that fail to parse are simply absent from the result."""
    if not token_accounts:
        return {}
    result = await rpc(
        "getMultipleAccounts",
        [token_accounts[:100], {"encoding": "jsonParsed", "commitment": "confirmed"}],
    )
    owners: dict[str, str] = {}
    for address, account in zip(token_accounts, result.get("value") or []):
        try:
            owners[address] = account["data"]["parsed"]["info"]["owner"]
        except (KeyError, TypeError):
            continue
    return owners
