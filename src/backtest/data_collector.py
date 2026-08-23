"""
Data Collector — Fase 4
Mengumpulkan token historis Pump.fun dari DexScreener public API.
Menyimpan hasil ke tabel Supabase `backtest_tokens`.

Rate-limit policy: 1 req/sec (DexScreener free tier).
Filter RPC-dependent (deployer history, ATA resolution) di-skip pada
mode offline ini — dicatat sebagai limitasi di laporan akhir.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.database.client import db_manager
from src.utils.logger import logger

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
REQUEST_DELAY_SECONDS = 1.2  # respect free-tier rate limits


async def _fetch_dexscreener_profiles(
    client: httpx.AsyncClient,
    offset: int = 0
) -> list[dict]:
    """Fetch token profile page from DexScreener (latest Solana listings)."""
    try:
        resp = await client.get(
            DEXSCREENER_SEARCH_URL,
            params={"chainId": "solana"},
            timeout=10.0
        )
        if resp.status_code != 200:
            logger.warning(f"DexScreener profiles HTTP {resp.status_code}")
            return []
        data = resp.json()
        if isinstance(data, list):
            return data
        return []
    except Exception as e:
        logger.debug(f"DexScreener profiles fetch error: {e}")
        return []


async def _fetch_token_detail(
    client: httpx.AsyncClient,
    mint_address: str
) -> Optional[dict]:
    """Fetch pair detail for a specific token mint from DexScreener."""
    try:
        resp = await client.get(
            DEXSCREENER_TOKEN_URL.format(mint=mint_address),
            timeout=10.0
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs")
        if not pairs or not isinstance(pairs, list):
            return None
        # Pick the pair with highest liquidity
        return sorted(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0), reverse=True)[0]
    except Exception as e:
        logger.debug(f"DexScreener token detail error for {mint_address[:8]}: {e}")
        return None


async def collect_historical_tokens(target: int = 300) -> int:
    """
    Main collection function. Fetches up to `target` Pump.fun token profiles
    from DexScreener and stores them in Supabase `backtest_tokens`.

    Returns the number of tokens successfully stored.
    """
    logger.info(f"🔍 Starting historical data collection — target: {target} tokens")
    stored = 0
    seen_addresses: set[str] = set()

    async with httpx.AsyncClient(
        headers={"User-Agent": "MemeScanner-Backtest/1.0"},
        timeout=15.0
    ) as client:
        # DexScreener latest endpoint returns ~30 tokens per call
        # We loop collecting distinct tokens until we reach our target
        consecutive_empty = 0
        while stored < target and consecutive_empty < 5:
            profiles = await _fetch_dexscreener_profiles(client)
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

            if not profiles:
                consecutive_empty += 1
                logger.warning(f"Empty profile page ({consecutive_empty}/5 consecutive)")
                await asyncio.sleep(5.0)
                continue

            consecutive_empty = 0
            new_this_round = 0

            for profile in profiles:
                if stored >= target:
                    break

                mint = profile.get("tokenAddress", "")
                chain = profile.get("chainId", "")
                if not mint or chain != "solana" or mint in seen_addresses:
                    continue

                seen_addresses.add(mint)

                # Fetch per-token pair data
                pair = await _fetch_token_detail(client, mint)
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

                if not pair:
                    continue

                # Build record
                base_token = pair.get("baseToken", {})
                symbol = base_token.get("symbol") or profile.get("description", "")[:20]
                name = base_token.get("name") or symbol

                listed_at_ms = pair.get("pairCreatedAt")
                listed_at = (
                    datetime.fromtimestamp(listed_at_ms / 1000, tz=timezone.utc).isoformat()
                    if listed_at_ms else None
                )

                price_usd_str = pair.get("priceUsd", "0") or "0"
                try:
                    price_usd = float(price_usd_str)
                except (ValueError, TypeError):
                    price_usd = 0.0

                liquidity = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                vol_24h = float((pair.get("volume") or {}).get("h24", 0) or 0)

                record = {
                    "token_address": mint,
                    "symbol": symbol[:20] if symbol else None,
                    "name": name[:60] if name else None,
                    "launch_venue": "pump_fun",
                    "listed_at": listed_at,
                    "price_usd_at_listing": price_usd,
                    "price_usd_24h": None,      # filled by labeler
                    "liquidity_usd": liquidity,
                    "volume_24h_usd": vol_24h,
                    "label": None,              # filled by labeler
                    "label_return_pct": None,   # filled by labeler
                    "raw_dexscreener": pair,
                }

                try:
                    await db_manager.upsert("backtest_tokens", record, on_conflict="token_address")
                    stored += 1
                    new_this_round += 1
                    if stored % 25 == 0:
                        logger.info(f"📦 Collected {stored}/{target} backtest tokens")
                except Exception as e:
                    logger.debug(f"Failed to upsert {mint[:8]}: {e}")

            if new_this_round == 0:
                consecutive_empty += 1
                logger.warning("No new unique tokens this round, backing off...")
                await asyncio.sleep(10.0)

    logger.info(f"✅ Collection complete: {stored} tokens stored in backtest_tokens")
    return stored
