import asyncio
import time
from typing import Optional
import httpx
from src.utils.logger import logger

SOL_MINT = "So11111111111111111111111111111111111111112"


class PriceFeedClient:
    """Async price feed client with in-memory caching and fallback."""

    def __init__(self, cache_ttl_seconds: int = 300, default_sol_usd: float = 180.0):
        self._cached_sol_usd: float = default_sol_usd
        self._last_fetched_ts: float = 0.0
        self._cache_ttl = cache_ttl_seconds
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=5.0)
        return self._client

    async def get_sol_price_usd(self) -> float:
        """
        Returns real-time SOL price in USD with 5-minute cache.
        Queries Jupiter Price API v2 with graceful fallback.
        """
        now = time.time()
        if (now - self._last_fetched_ts) < self._cache_ttl and self._last_fetched_ts > 0:
            return self._cached_sol_usd

        async with self._lock:
            # Double-check after acquiring lock
            now_inner = time.time()
            if (now_inner - self._last_fetched_ts) < self._cache_ttl and self._last_fetched_ts > 0:
                return self._cached_sol_usd

            try:
                client = await self._get_client()
                # 1. Primary: Jupiter Price API v2
                url = f"https://api.jup.ag/price/v2?ids={SOL_MINT}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    price_data = data.get("data", {}).get(SOL_MINT, {})
                    price_str = price_data.get("price")
                    if price_str:
                        price = float(price_str)
                        if price > 0:
                            self._cached_sol_usd = price
                            self._last_fetched_ts = now_inner
                            logger.debug(f"Updated live SOL/USD price from Jupiter: ${price:.2f}")
                            return self._cached_sol_usd

                # 2. Fallback: Binance Public Ticker
                resp_binance = await client.get("https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT")
                if resp_binance.status_code == 200:
                    b_data = resp_binance.json()
                    b_price = float(b_data.get("price", 0.0))
                    if b_price > 0:
                        self._cached_sol_usd = b_price
                        self._last_fetched_ts = now_inner
                        logger.debug(f"Updated live SOL/USD price from Binance: ${b_price:.2f}")
                        return self._cached_sol_usd

            except Exception as e:
                logger.debug(f"Price feed fetch failed: {e}. Using cached/fallback ${self._cached_sol_usd:.2f}")

            # Update timestamp to avoid hammering on failure
            self._last_fetched_ts = now_inner
            return self._cached_sol_usd

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


price_feed = PriceFeedClient()
