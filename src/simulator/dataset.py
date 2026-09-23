"""
Dataset Loader — loads and caches closed trades from paper_trade_positions.

Fetches all versions (v1.x, v2.0, v2.1) with relevant columns for simulation.
Results are cached in memory so sweep runs don't re-query Supabase repeatedly.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from src.database.client import db_manager
from src.utils.logger import logger

# In-memory cache: avoids repeated Supabase round-trips during sweep
_TRADE_CACHE: Optional[list[dict]] = None
_CACHE_LOCK = asyncio.Lock()

# Columns we need from paper_trade_positions for simulation
_SELECT_COLS = ",".join([
    "id",
    "symbol",
    "opportunity_score_at_entry",
    "entry_price_usd",
    "exit_price_usd",
    "mfe_pct",
    "hold_duration_minutes",
    "exit_reason",
    "position_size_usd",
    "parameter_version",
    "tp0_hit",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
    "remaining_fraction",
    "liquidity_at_entry_usd",
    "breakeven_sl_active",
    "entry_time",
    "exit_time",
])


async def load_trades(force_refresh: bool = False) -> list[dict]:
    """
    Load all closed trades from paper_trade_positions.
    Results are cached in memory; call with force_refresh=True to re-query.

    Returns list of trade dicts with all simulation-relevant columns.
    Filters: exit_reason != 'OPEN' and entry_price_usd IS NOT NULL.
    """
    global _TRADE_CACHE

    async with _CACHE_LOCK:
        if _TRADE_CACHE is not None and not force_refresh:
            logger.debug(f"[Dataset] Using cached dataset ({len(_TRADE_CACHE)} trades).")
            return _TRADE_CACHE

        logger.info("[Dataset] Loading closed trades from Supabase...")
        db = db_manager._client

        # Fetch all pages (Supabase default limit is 1000 per request)
        all_trades: list[dict] = []
        page = 0
        page_size = 1000

        while True:
            res = (
                db.table("paper_trade_positions")
                .select(_SELECT_COLS)
                .neq("exit_reason", "OPEN")
                .not_.is_("entry_price_usd", "null")
                .not_.is_("mfe_pct", "null")
                .order("entry_time", desc=False)
                .range(page * page_size, (page + 1) * page_size - 1)
                .execute()
            )
            rows = res.data or []
            all_trades.extend(rows)
            if len(rows) < page_size:
                break
            page += 1

        logger.info(f"[Dataset] Loaded {len(all_trades)} closed trades.")
        _TRADE_CACHE = all_trades
        return all_trades


def clear_cache() -> None:
    """Clear the in-memory trade cache (useful after new trades are added)."""
    global _TRADE_CACHE
    _TRADE_CACHE = None


def get_dataset_meta(trades: list[dict]) -> dict:
    """
    Compute basic metadata about the loaded dataset.
    Returns counts by version, date range, exit reason distribution, etc.
    """
    if not trades:
        return {"total": 0}

    versions: dict[str, int] = {}
    exit_reasons: dict[str, int] = {}
    scores = []
    mfes = []

    for t in trades:
        v = t.get("parameter_version") or "unknown"
        versions[v] = versions.get(v, 0) + 1

        r = t.get("exit_reason") or "UNKNOWN"
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

        score = t.get("opportunity_score_at_entry")
        if score is not None:
            scores.append(float(score))

        mfe = t.get("mfe_pct")
        if mfe is not None:
            mfes.append(float(mfe))

    return {
        "total": len(trades),
        "versions": versions,
        "exit_reasons": exit_reasons,
        "date_first": (trades[0].get("entry_time") or "")[:10],
        "date_last": (trades[-1].get("entry_time") or "")[:10],
        "avg_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
        "avg_mfe_pct": round(sum(mfes) / len(mfes), 2) if mfes else 0.0,
        "max_mfe_pct": round(max(mfes), 2) if mfes else 0.0,
    }
