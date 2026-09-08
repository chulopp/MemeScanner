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

ALLOWED_DEX_IDS = {"pumpfun", "pumpswap", "raydium", "meteora", "orca", "whirlpool"}
ALLOWED_QUOTE_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT"}

# DexScreener rate limit state — when 429 is received, cool down for 30s
import time as _time
_dex_rate_limited_until: float = 0.0
DEX_RATE_LIMIT_COOLDOWN = 30.0  # seconds to wait after 429

# Last-known-good DexScreener cache — serves stale data during rate limit window
# Format: {mint: PriceSnapshot}
_dex_last_good: dict[str, "PriceSnapshot"] = {}


def format_mcap(mcap: float) -> str:
    """Format market cap into readable string, e.g. $43.2K or $1.25M."""
    if not mcap or mcap <= 0:
        return "N/A"
    if mcap >= 1_000_000_000:
        return f"${mcap / 1_000_000_000:.2f}B"
    if mcap >= 1_000_000:
        return f"${mcap / 1_000_000:.2f}M"
    if mcap >= 1_000:
        return f"${mcap / 1_000:.1f}K"
    return f"${mcap:.0f}"


@dataclass
class PriceSnapshot:
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    source: str  # 'bonding_curve' | 'dexscreener' | 'helius' | 'rpc'
    market_cap_usd: float = 0.0


async def _fetch_dexscreener(client: httpx.AsyncClient, mint: str) -> Optional["PriceSnapshot"]:
    """Tier 1: DexScreener public API with strict DEX whitelist and sanity checks.
    Handles 429 rate limits with 30s cooldown; serves last-known-good cache during cooldown."""
    global _dex_rate_limited_until

    # Check if we're in a rate limit cooldown window
    now = _time.time()
    if now < _dex_rate_limited_until:
        remaining = _dex_rate_limited_until - now
        logger.debug(f"DexScreener rate limit cooldown active ({remaining:.0f}s remaining) for {mint[:8]}")
        # Return last-known-good price if available during cooldown
        cached = _dex_last_good.get(mint)
        if cached:
            logger.debug(f"Serving stale DexScreener cache for {mint[:8]} (source: dexscreener_cached)")
            return PriceSnapshot(
                price_usd=cached.price_usd,
                liquidity_usd=cached.liquidity_usd,
                volume_24h_usd=cached.volume_24h_usd,
                source="dexscreener_cached",
                market_cap_usd=cached.market_cap_usd,
            )
        return None

    try:
        resp = await client.get(DEXSCREENER_TOKEN_URL.format(mint=mint), timeout=8.0)
        if resp.status_code == 429:
            _dex_rate_limited_until = _time.time() + DEX_RATE_LIMIT_COOLDOWN
            logger.warning(
                f"⚠️ DexScreener 429 rate limit hit for {mint[:8]}. "
                f"Cooling down for {DEX_RATE_LIMIT_COOLDOWN:.0f}s."
            )
            # Serve last-known-good if available
            cached = _dex_last_good.get(mint)
            if cached:
                return PriceSnapshot(
                    price_usd=cached.price_usd,
                    liquidity_usd=cached.liquidity_usd,
                    volume_24h_usd=cached.volume_24h_usd,
                    source="dexscreener_cached",
                    market_cap_usd=cached.market_cap_usd,
                )
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs")
        if not pairs or not isinstance(pairs, list):
            return None

        valid_pairs = []
        for p in pairs:
            dex_id = (p.get("dexId") or "").lower()
            if dex_id not in ALLOWED_DEX_IDS:
                continue
            quote_sym = (p.get("quoteToken", {}).get("symbol") or "").upper()
            if quote_sym not in ALLOWED_QUOTE_SYMBOLS:
                continue
            valid_pairs.append(p)

        if not valid_pairs:
            return None

        is_pump = mint.endswith("pump")
        selected_pair = None

        if is_pump:
            # Raydium / Meteora / PumpSwap pool with meaningful liquidity indicates graduation
            graduated_pairs = [
                p for p in valid_pairs
                if p.get("dexId") in ("raydium", "meteora", "pumpswap")
                and float((p.get("liquidity") or {}).get("usd", 0) or 0) > 1000
            ]
            if graduated_pairs:
                selected_pair = sorted(
                    graduated_pairs,
                    key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
                    reverse=True
                )[0]
            else:
                # If ungraduated or no high liquidity graduated pair, sort by liquidity descending, then volume
                selected_pair = sorted(
                    valid_pairs,
                    key=lambda p: (
                        float((p.get("liquidity") or {}).get("usd", 0) or 0),
                        float((p.get("volume") or {}).get("h24", 0) or 0)
                    ),
                    reverse=True
                )[0]

        if not selected_pair:
            selected_pair = sorted(
                valid_pairs,
                key=lambda p: (
                    float((p.get("liquidity") or {}).get("usd", 0) or 0),
                    float((p.get("volume") or {}).get("h24", 0) or 0)
                ),
                reverse=True
            )[0]

        price = float(selected_pair.get("priceUsd", "0") or "0")
        liq = float((selected_pair.get("liquidity") or {}).get("usd", 0) or 0)
        vol = float((selected_pair.get("volume") or {}).get("h24", 0) or 0)
        fdv = float(selected_pair.get("fdv") or selected_pair.get("marketCap") or 0.0)
        if fdv <= 0 and price > 0 and is_pump:
            fdv = price * 1_000_000_000

        if price > 0:
            result = PriceSnapshot(
                price_usd=price,
                liquidity_usd=liq,
                volume_24h_usd=vol,
                source="dexscreener",
                market_cap_usd=fdv,
            )
            # Cache the good result for rate limit recovery
            _dex_last_good[mint] = result
            return result
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
            p_val = float(price)
            mcap = p_val * 1_000_000_000 if mint.endswith("pump") else 0.0
            return PriceSnapshot(
                price_usd=p_val,
                liquidity_usd=0.0,  # DAS doesn't provide liquidity directly
                volume_24h_usd=0.0,
                source="helius",
                market_cap_usd=mcap,
            )
        return None
    except Exception as e:
        logger.debug(f"Helius DAS price fetch error for {mint[:8]}: {e}")
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


async def fetch_price(mint: str, bonding_curve_address: Optional[str] = None) -> Optional[PriceSnapshot]:
    """
    Fetches current price from multi-tier verified sources:
    - Tier 1: On-chain Pump.fun Bonding Curve account state (exact virtual reserves)
    - Tier 2: DexScreener verified pools (pumpfun, pumpswap, raydium, meteora, orca)
    - Tier 3: Helius DAS verified token price info
    Returns PriceSnapshot or None if all verified sources fail. NEVER fabricates fake prices.
    """
    client = await _get_client()

    # Tier 1: On-chain Pump.fun Bonding Curve state (instant for ungraduated tokens)
    if not bonding_curve_address and mint.endswith("pump"):
        try:
            from solders.pubkey import Pubkey
            PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
            m_pub = Pubkey.from_string(mint)
            bc_pda, _ = Pubkey.find_program_address([b"bonding-curve", bytes(m_pub)], PUMP_PROGRAM)
            bonding_curve_address = str(bc_pda)
        except Exception:
            pass

    if bonding_curve_address:
        try:
            from src.utils.solana_rpc import solana_rpc
            from src.utils.price_feed import price_feed
            bc_data = await solana_rpc.get_bonding_curve_price(bonding_curve_address)
            if bc_data and not bc_data.get("is_complete"):
                sol_usd = await price_feed.get_sol_price_usd()
                price_usd = bc_data.get("price_sol", 0.0) * sol_usd
                if price_usd > 0:
                    v_sol = bc_data.get("virtual_sol_reserves", 0) / 1e9
                    mcap = price_usd * 1_000_000_000
                    return PriceSnapshot(
                        price_usd=price_usd,
                        liquidity_usd=v_sol * sol_usd * 2,
                        volume_24h_usd=0.0,
                        source="bonding_curve",
                        market_cap_usd=mcap,
                    )
        except Exception as bc_err:
            logger.debug(f"Bonding curve price fetch failed for {mint[:8]}: {bc_err}")

    # Tier 2: DexScreener (with verified DEX pools)
    snap = await _fetch_dexscreener(client, mint)
    if snap:
        return snap

    # Tier 3: Helius DAS
    snap = await _fetch_helius_das(client, mint)
    if snap:
        return snap

    logger.warning(f"⚠️ All verified price tiers failed for {mint[:8]}...")
    return None


async def close():
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        _shared_client = None
