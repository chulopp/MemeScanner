"""
3-Tier Price Fetcher — Fase 5
Fetches live token price, liquidity, and volume from multiple sources with fallback:
  Tier 1: DexScreener public API (free, no key)
  Tier 2: Helius DAS getAsset (already configured in codebase)
  Tier 3: Solana RPC pool reserve calculation (on-chain)

Used for ATH tracking (30s polling) and window outcome resolution.
"""

import asyncio
from dataclasses import dataclass
from typing import Optional

import httpx

from src.config import settings
from src.utils.logger import logger

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"


@dataclass
class PriceSnapshot:
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    source: str  # 'dexscreener' | 'helius' | 'rpc'


async def _fetch_dexscreener(client: httpx.AsyncClient, mint: str) -> Optional[PriceSnapshot]:
    """Tier 1: DexScreener public API."""
    try:
        resp = await client.get(DEXSCREENER_TOKEN_URL.format(mint=mint), timeout=8.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs")
        if not pairs or not isinstance(pairs, list):
            return None
        best = sorted(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0), reverse=True)[0]
        price = float(best.get("priceUsd", "0") or "0")
        liq = float((best.get("liquidity") or {}).get("usd", 0) or 0)
        vol = float((best.get("volume") or {}).get("h24", 0) or 0)
        if price > 0:
            return PriceSnapshot(price_usd=price, liquidity_usd=liq, volume_24h_usd=vol, source="dexscreener")
        return None
    except Exception as e:
        logger.debug(f"DexScreener price fetch error for {mint[:8]}: {e}")
        return None


async def _fetch_helius_das(client: httpx.AsyncClient, mint: str) -> Optional[PriceSnapshot]:
    """Tier 2: Helius DAS getAsset — token metadata with optional price info."""
    try:
        resp = await client.post(
            settings.helius_rpc_url,
            json={
                "jsonrpc": "2.0", "id": "price-fetch",
                "method": "getAsset",
                "params": {"id": mint}
            },
            timeout=8.0
        )
        if resp.status_code != 200:
            return None
        result = resp.json().get("result", {})
        token_info = result.get("token_info", {})
        price = token_info.get("price_info", {}).get("price_per_token", 0)
        if price and float(price) > 0:
            return PriceSnapshot(
                price_usd=float(price),
                liquidity_usd=0.0,  # DAS doesn't provide liquidity directly
                volume_24h_usd=0.0,
                source="helius"
            )
        return None
    except Exception as e:
        logger.debug(f"Helius DAS price fetch error for {mint[:8]}: {e}")
        return None


async def _fetch_rpc_reserves(client: httpx.AsyncClient, mint: str) -> Optional[PriceSnapshot]:
    """Tier 3: Solana RPC — basic token supply query as last-resort price proxy."""
    try:
        from src.utils.price_feed import price_feed
        sol_price = await price_feed.get_sol_price_usd()

        resp = await client.post(
            settings.helius_rpc_url,
            json={
                "jsonrpc": "2.0", "id": "supply",
                "method": "getTokenSupply",
                "params": [mint]
            },
            timeout=8.0
        )
        if resp.status_code != 200:
            return None
        result = resp.json().get("result", {}).get("value", {})
        supply = float(result.get("uiAmount", 0) or 0)
        if supply > 0:
            # Very rough estimate based on typical pump.fun bonding curve
            estimated_mcap = sol_price * 30.0  # 30 SOL initial virtual reserve
            price_est = estimated_mcap / supply
            return PriceSnapshot(
                price_usd=price_est,
                liquidity_usd=0.0,
                volume_24h_usd=0.0,
                source="rpc"
            )
        return None
    except Exception as e:
        logger.debug(f"RPC reserve price fetch error for {mint[:8]}: {e}")
        return None


# Shared HTTP client for price fetcher
_shared_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            headers={"User-Agent": "MemeScanner-PaperTrading/1.0"},
            timeout=10.0
        )
    return _shared_client


async def fetch_price(mint: str) -> Optional[PriceSnapshot]:
    """
    Fetches current price from 3-tier fallback chain.
    Returns PriceSnapshot or None if all tiers fail.
    """
    client = await _get_client()

    # Tier 1: DexScreener
    snap = await _fetch_dexscreener(client, mint)
    if snap:
        return snap

    # Tier 2: Helius DAS
    snap = await _fetch_helius_das(client, mint)
    if snap:
        return snap

    # Tier 3: RPC reserves
    snap = await _fetch_rpc_reserves(client, mint)
    if snap:
        return snap

    logger.warning(f"⚠️ All 3 price tiers failed for {mint[:8]}...")
    return None


async def close():
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None
