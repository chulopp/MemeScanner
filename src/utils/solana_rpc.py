import asyncio
import base64
import struct
import time
from typing import Optional, Any
import httpx
from src.config import settings
from src.utils.logger import logger


class SolanaRpcClient:
    """Async Solana RPC client with rate-limiting and robust error handling."""

    def __init__(self, rpc_url: Optional[str] = None, max_concurrency: int = 15):
        self.rpc_url = rpc_url or settings.helius_rpc_url
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def _rpc_call(self, method: str, params: list[Any]) -> Optional[dict]:
        """Generic JSON-RPC POST call with semaphore concurrency control."""
        async with self.semaphore:
            client = await self._get_client()
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": method,
                "params": params
            }
            for attempt in range(3):
                try:
                    response = await client.post(self.rpc_url, json=payload)
                    if response.status_code == 200:
                        data = response.json()
                        if "error" in data:
                            logger.debug(f"RPC Error ({method}): {data['error']}")
                            return None
                        return data.get("result")
                    elif response.status_code == 429:
                        # Rate limit backoff
                        await asyncio.sleep(0.2 * (2 ** attempt))
                    else:
                        logger.debug(f"RPC HTTP {response.status_code} for {method}")
                except Exception as e:
                    logger.debug(f"RPC Exception ({method}) attempt {attempt+1}: {e}")
                    await asyncio.sleep(0.1 * (attempt + 1))
            return None

    async def get_account_info(self, pubkey: str) -> Optional[dict]:
        """Fetch parsed account info."""
        res = await self._rpc_call(
            "getAccountInfo",
            [pubkey, {"encoding": "jsonParsed"}]
        )
        return res.get("value") if res else None

    async def get_mint_info(self, mint_address: str) -> dict:
        """
        Parses SPL Token Mint data.
        Returns: {mint_authority: str|None, freeze_authority: str|None, supply: int, decimals: int}
        """
        acc_info = await self.get_account_info(mint_address)
        if not acc_info:
            return {
                "mint_authority": None,
                "freeze_authority": None,
                "supply": 0,
                "decimals": 0,
                "exists": False
            }

        parsed = acc_info.get("data", {}).get("parsed", {})
        info = parsed.get("info", {})

        return {
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            "supply": int(info.get("supply", 0)),
            "decimals": int(info.get("decimals", 0)),
            "exists": True
        }

    async def get_token_largest_accounts(self, mint_address: str) -> list[dict]:
        """
        Returns top 20 holder accounts for a mint.
        Each item: {"address": str, "amount": str, "decimals": int, "uiAmount": float}
        """
        res = await self._rpc_call("getTokenLargestAccounts", [mint_address])
        return res.get("value", []) if res else []

    async def get_sol_balance(self, pubkey: str) -> float:
        """Returns SOL balance in SOL (float)."""
        res = await self._rpc_call("getBalance", [pubkey])
        if res and "value" in res:
            return res["value"] / 1_000_000_000.0
        return 0.0

    async def get_signatures_for_address(self, pubkey: str, limit: int = 50) -> list[dict]:
        """Returns transaction signatures list for an address."""
        res = await self._rpc_call(
            "getSignaturesForAddress",
            [pubkey, {"limit": limit}]
        )
        return res if isinstance(res, list) else []

    async def get_wallet_age_days(self, pubkey: str) -> float:
        """
        Estimates wallet age in days based on earliest available transaction blockTime.
        Returns 0.0 if fresh (<1 day) or unavailable.
        """
        signatures = await self.get_signatures_for_address(pubkey, limit=100)
        if not signatures:
            return 0.0

        # Signatures are returned newest-to-oldest. Last item is the oldest in batch.
        oldest_tx = signatures[-1]
        block_time = oldest_tx.get("blockTime")
        if not block_time:
            return 0.0

        age_seconds = time.time() - block_time
        return age_seconds / 86400.0

    async def get_recent_prioritization_fees(self) -> list[int]:
        """Returns list of recent prioritization fees in micro-lamports."""
        res = await self._rpc_call("getRecentPrioritizationFees", [])
        if res and isinstance(res, list):
            return [item.get("prioritizationFee", 0) for item in res]
        return []

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


solana_rpc = SolanaRpcClient()
