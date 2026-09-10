import asyncio
import threading

import httpx

from app.settings import settings

# Jupiter's public API rate-limits bursts of concurrent requests (HTTP 429).
# Cap concurrency and retry with backoff so batch lookups (e.g. portfolio
# valuation across many mints) don't silently degrade to missing data.
_REQUEST_SEMAPHORE = threading.BoundedSemaphore(4)
_MAX_RETRIES = 3
WRAPPED_SOL_MINT = "So11111111111111111111111111111111111111112"


def normalize_mint(value: str) -> str:
    """Normalize model/UI formatting without resolving arbitrary ticker symbols."""
    mint = str(value).strip().strip("`\"'").strip()
    if mint.upper() == "SOL":
        return WRAPPED_SOL_MINT
    return mint


class JupiterClient:
    def __init__(self) -> None:
        self.base_url = settings.jupiter_base_url.rstrip("/")
        self.api_root = settings.jupiter_api_root.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        return {"x-api-key": settings.jupiter_api_key} if settings.jupiter_api_key else {}

    async def _get(self, url: str, params: dict) -> httpx.Response:
        await asyncio.to_thread(_REQUEST_SEMAPHORE.acquire)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for attempt in range(_MAX_RETRIES):
                    response = await client.get(url, params=params, headers=self.headers)
                    if response.status_code != 429:
                        response.raise_for_status()
                        return response
                    await asyncio.sleep(0.5 * (2**attempt))
                response.raise_for_status()
                return response
        finally:
            _REQUEST_SEMAPHORE.release()

    async def quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int
    ) -> dict:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "restrictIntermediateTokens": "true",
        }
        response = await self._get(f"{self.base_url}/quote", params)
        return response.json()

    async def search_tokens(self, query: str) -> list[dict]:
        """Search Jupiter's token registry by symbol, name, or mint."""
        response = await self._get(f"{self.api_root}/tokens/v2/search", {"query": query})
        data = response.json()
        return data if isinstance(data, list) else []

    async def token_by_mint(self, mint: str) -> dict:
        mint = normalize_mint(mint)
        matches = await self.search_tokens(mint)
        exact = [item for item in matches if item.get("id") == mint]
        if len(exact) != 1:
            raise ValueError(f"Mint {mint} was not found uniquely in Jupiter's token registry")
        return exact[0]

    async def shield(self, mints: list[str]) -> dict:
        """Return Jupiter Ultra Shield warnings for exact mint addresses."""
        response = await self._get(f"{self.api_root}/ultra/v1/shield", {"mints": ",".join(mints)})
        return response.json()

    async def swap_transaction(self, quote: dict, wallet_address: str) -> dict:
        payload = {
            "quoteResponse": quote,
            "userPublicKey": wallet_address,
            "dynamicComputeUnitLimit": True,
            "dynamicSlippage": False,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": 1_000_000,
                    "priorityLevel": "high",
                }
            },
        }
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(f"{self.base_url}/swap", json=payload, headers=self.headers)
            response.raise_for_status()
            return response.json()


jupiter = JupiterClient()
