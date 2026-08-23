"""
Labeler — Fase 4
Mengambil harga saat ini dari DexScreener untuk token yang sudah dikumpulkan,
menghitung return % vs harga saat listing, dan assign label:
  - 'runner': return ≥ +100% (≥2x)
  - 'dead'  : return ≤ -70%
  - 'neutral': sisanya

Hanya token yang price_usd_at_listing > 0 yang bisa dilabeli.
"""

import asyncio
from typing import Optional

import httpx

from src.database.client import db_manager
from src.utils.logger import logger

DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
REQUEST_DELAY_SECONDS = 1.2

# Thresholds — HYPOTHESIS_INIT (dapat dikalibrasi di Fase 5)
RUNNER_THRESHOLD_PCT = 100.0   # HYPOTHESIS_INIT: ≥2x
DEAD_THRESHOLD_PCT = -70.0     # HYPOTHESIS_INIT: ≤-70%


async def _get_current_price(client: httpx.AsyncClient, mint: str) -> Optional[float]:
    """Fetch current price for a token from DexScreener."""
    try:
        resp = await client.get(
            DEXSCREENER_TOKEN_URL.format(mint=mint),
            timeout=10.0
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        pairs = data.get("pairs")
        if not pairs:
            return None
        # Best pair by liquidity
        best = sorted(
            pairs,
            key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0),
            reverse=True
        )[0]
        price_str = best.get("priceUsd", "0") or "0"
        return float(price_str)
    except Exception as e:
        logger.debug(f"Price fetch error for {mint[:8]}: {e}")
        return None


def _assign_label(return_pct: float) -> str:
    """Classify token outcome based on return from listing price."""
    if return_pct >= RUNNER_THRESHOLD_PCT:
        return "runner"
    elif return_pct <= DEAD_THRESHOLD_PCT:
        return "dead"
    else:
        return "neutral"


async def label_backtest_tokens(limit: int = 500) -> dict:
    """
    Fetch current price for unlabeled backtest_tokens and assign labels.
    Returns summary dict: {total, labeled, skipped, runners, dead, neutral}
    """
    logger.info("🏷️  Starting backtest token labeling via DexScreener...")

    # Fetch unlabeled tokens from Supabase
    rows = await db_manager.query(
        "backtest_tokens",
        filters={"label": "is.null"},
        limit=limit
    )

    if not rows:
        logger.info("No unlabeled tokens found.")
        return {"total": 0, "labeled": 0, "skipped": 0, "runners": 0, "dead": 0, "neutral": 0}

    logger.info(f"Found {len(rows)} unlabeled tokens")

    stats = {"total": len(rows), "labeled": 0, "skipped": 0, "runners": 0, "dead": 0, "neutral": 0}

    async with httpx.AsyncClient(
        headers={"User-Agent": "MemeScanner-Backtest/1.0"},
        timeout=15.0
    ) as client:
        for row in rows:
            mint = row["token_address"]
            price_at_listing = row.get("price_usd_at_listing") or 0.0

            if price_at_listing <= 0:
                # Cannot compute return without a baseline price
                stats["skipped"] += 1
                logger.debug(f"Skipping {mint[:8]} — no listing price")
                continue

            current_price = await _get_current_price(client, mint)
            await asyncio.sleep(REQUEST_DELAY_SECONDS)

            if current_price is None or current_price <= 0:
                stats["skipped"] += 1
                continue

            return_pct = ((current_price - price_at_listing) / price_at_listing) * 100.0
            label = _assign_label(return_pct)

            # Update Supabase row
            try:
                await db_manager.update(
                    "backtest_tokens",
                    {"label": label, "label_return_pct": round(return_pct, 4), "price_usd_24h": current_price},
                    filters={"token_address": f"eq.{mint}"}
                )
                stats["labeled"] += 1
                stats[label] += 1

                if stats["labeled"] % 25 == 0:
                    logger.info(
                        f"🏷️  Labeled {stats['labeled']} tokens "
                        f"(runners: {stats['runners']}, dead: {stats['dead']}, "
                        f"neutral: {stats['neutral']})"
                    )
            except Exception as e:
                logger.debug(f"Update error for {mint[:8]}: {e}")
                stats["skipped"] += 1

    logger.info(
        f"✅ Labeling complete: {stats['labeled']} labeled, {stats['skipped']} skipped | "
        f"Runners: {stats['runners']} | Dead: {stats['dead']} | Neutral: {stats['neutral']}"
    )
    return stats
