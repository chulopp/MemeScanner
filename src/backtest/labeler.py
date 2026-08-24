"""
Outcome Resolver & Labeler — Fase 4 & 5
Resolves outcomes for tokens whose 24-hour observation window has elapsed.
Fetches actual 24-hour price from DexScreener/RPC and classifies ground-truth outcomes:
  - 'runner' : return ≥ +100% (≥2x)
  - 'dead'   : return ≤ -70% or pool liquidity dried up
  - 'neutral': otherwise
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx

from src.database.client import db_manager
from src.utils.logger import logger

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
REQUEST_DELAY_SECONDS = 1.0

# Thresholds — HYPOTHESIS_INIT
RUNNER_THRESHOLD_PCT = 100.0
DEAD_THRESHOLD_PCT = -70.0


async def _fetch_current_token_price(client: httpx.AsyncClient, mint: str) -> Optional[tuple[float, float, float]]:
    """
    Fetches latest price (USD), 24h volume (USD), and liquidity (USD) from DexScreener.
    Returns (price_usd, volume_24h_usd, liquidity_usd) or None.
    """
    try:
        resp = await client.get(
            DEXSCREENER_TOKEN_URL.format(mint=mint),
            timeout=10.0
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs")
        if not pairs or not isinstance(pairs, list):
            # If no pairs exist after 24h, the token likely never graduated or rugged
            return (0.0, 0.0, 0.0)

        # Pick the pair with highest liquidity
        best_pair = sorted(
            pairs,
            key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
            reverse=True
        )[0]

        price_str = best_pair.get("priceUsd", "0") or "0"
        price_usd = float(price_str)
        vol_24h = float((best_pair.get("volume") or {}).get("h24", 0) or 0)
        liq_usd = float((best_pair.get("liquidity") or {}).get("usd", 0) or 0)

        return (price_usd, vol_24h, liq_usd)
    except Exception as e:
        logger.debug(f"DexScreener price query error for {mint[:8]}: {e}")
        return None


def assign_label(return_pct: float, liquidity_usd: float = 10_000.0) -> str:
    """Classifies token outcome based on 24h return percentage and liquidity."""
    if liquidity_usd < 500.0 or return_pct <= DEAD_THRESHOLD_PCT:
        return "dead"
    elif return_pct >= RUNNER_THRESHOLD_PCT:
        return "runner"
    else:
        return "neutral"


async def resolve_due_tokens(limit: int = 500, force_all_unresolved: bool = False) -> dict:
    """
    Finds tokens whose 24-hour observation period is complete (now >= resolution_due_at),
    fetches their 24h price, and marks them as resolved in Supabase.
    """
    now_utc = datetime.now(tz=timezone.utc)
    now_iso = now_utc.isoformat()

    logger.info(f"🔍 Checking for tokens ready for 24h resolution (now: {now_iso[:19]})...")
    await db_manager.initialize()

    # Query unresolved tokens
    filters = {"is_resolved": "eq.false"}
    if not force_all_unresolved:
        filters["resolution_due_at"] = f"lte.{now_iso}"

    rows = await db_manager.query(
        "backtest_tokens",
        filters=filters,
        limit=limit
    )

    if not rows:
        logger.info("ℹ️ No tokens are due for 24h resolution at this moment.")
        return {"total_checked": 0, "resolved": 0, "runners": 0, "dead": 0, "neutral": 0, "pending": 0}

    logger.info(f"Found {len(rows)} tokens ready for 24h outcome resolution.")
    stats = {"total_checked": len(rows), "resolved": 0, "runners": 0, "dead": 0, "neutral": 0, "pending": 0}

    async with httpx.AsyncClient(headers={"User-Agent": "MemeScanner-Resolver/1.0"}, timeout=15.0) as client:
        for row in rows:
            mint = row["token_address"]
            launch_price = row.get("launch_price_usd") or row.get("price_usd_at_listing") or 0.0

            if launch_price <= 0:
                logger.debug(f"Skipping {mint[:8]} — invalid launch price ({launch_price})")
                continue

            price_data = await _fetch_current_token_price(client, mint)
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

            if price_data is None:
                stats["pending"] += 1
                continue

            current_price, vol_24h, liq_usd = price_data
            if current_price > 0:
                return_pct = ((current_price - launch_price) / launch_price) * 100.0
            else:
                return_pct = -100.0  # 100% loss / total rug

            label = assign_label(return_pct, liquidity_usd=liq_usd)

            updates = {
                "label": label,
                "label_return_pct": round(return_pct, 4),
                "price_24h_usd": current_price,
                "price_usd_24h": current_price,
                "liquidity_usd": liq_usd,
                "volume_24h_usd": vol_24h,
                "is_resolved": True,
                "resolved_at": now_iso
            }

            try:
                await db_manager.update(
                    "backtest_tokens",
                    updates,
                    filters={"token_address": f"eq.{mint}"}
                )
                stats["resolved"] += 1
                stats[label] += 1
                logger.info(
                    f"✅ [Resolved 24h] {row.get('symbol', 'TOKEN')} ({mint[:8]}...) -> "
                    f"Label: {label.upper()} ({return_pct:+.1f}%) | Price: ${current_price:.8f}"
                )
            except Exception as e:
                logger.debug(f"Failed to update resolution for {mint[:8]}: {e}")

    logger.info(
        f"🎯 Resolution complete: {stats['resolved']} tokens resolved | "
        f"Runners: {stats['runners']} | Dead: {stats['dead']} | Neutral: {stats['neutral']}"
    )
    return stats
