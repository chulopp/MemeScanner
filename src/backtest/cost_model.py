"""
Cost Model — Fase 4
Realistic Cost Model untuk EV calculation di backtest.

Slippage bergradasi berdasarkan likuiditas token:
  - < $50K  : 5%    (mikro, sangat illiquid)
  - $50K–$200K: 2%  (kecil-menengah)
  - > $200K : 0.5%  (cukup liquid)

Priority fee: P80 dari distribusi aktual di Supabase metric_snapshots.
Dikonversi ke USD menggunakan harga SOL rata-rata dari snapshot.

Semua threshold adalah HYPOTHESIS_INIT — dikalibrasi via Bayesian optimizer.
"""

from dataclasses import dataclass
from typing import Optional

# HYPOTHESIS_INIT slippage tiers (%)
SLIPPAGE_MICRO = 5.0     # HYPOTHESIS_INIT: likuiditas < $50K
SLIPPAGE_SMALL = 2.0     # HYPOTHESIS_INIT: likuiditas $50K–$200K
SLIPPAGE_MEDIUM = 0.5    # HYPOTHESIS_INIT: likuiditas > $200K

LIQUIDITY_MICRO_THRESHOLD = 50_000.0   # HYPOTHESIS_INIT
LIQUIDITY_SMALL_THRESHOLD = 200_000.0  # HYPOTHESIS_INIT

# Default priority fee in SOL if Supabase data unavailable
DEFAULT_PRIORITY_FEE_SOL = 0.001  # HYPOTHESIS_INIT
DEFAULT_SOL_PRICE_USD = 180.0     # HYPOTHESIS_INIT


@dataclass
class TradeCost:
    slippage_pct: float
    priority_fee_usd: float
    total_cost_pct: float  # effective cost as % of trade value


@dataclass
class CostModelConfig:
    """
    Configuration for the cost model.
    All parameters tagged HYPOTHESIS_INIT can be adjusted by the Bayesian optimizer.
    """
    sol_price_usd: float = DEFAULT_SOL_PRICE_USD         # HYPOTHESIS_INIT
    priority_fee_sol: float = DEFAULT_PRIORITY_FEE_SOL   # HYPOTHESIS_INIT
    trade_size_usd: float = 100.0                        # HYPOTHESIS_INIT: normalized to $100 per trade


def compute_trade_cost(
    liquidity_usd: float,
    config: Optional[CostModelConfig] = None
) -> TradeCost:
    """
    Compute realistic entry + exit cost for a memecoin trade.

    Args:
        liquidity_usd: Pool liquidity in USD at listing time
        config: Cost model configuration

    Returns:
        TradeCost with slippage, priority fee, and total cost pct
    """
    cfg = config or CostModelConfig()

    # Graduated slippage
    if liquidity_usd < LIQUIDITY_MICRO_THRESHOLD:
        slippage = SLIPPAGE_MICRO
    elif liquidity_usd < LIQUIDITY_SMALL_THRESHOLD:
        slippage = SLIPPAGE_SMALL
    else:
        slippage = SLIPPAGE_MEDIUM

    # Priority fee as % of trade size
    priority_fee_usd = cfg.priority_fee_sol * cfg.sol_price_usd
    priority_fee_pct = (priority_fee_usd / cfg.trade_size_usd) * 100.0

    # Total cost = entry slippage + exit slippage + priority fee (both entry & exit)
    total_cost_pct = (slippage * 2) + (priority_fee_pct * 2)

    return TradeCost(
        slippage_pct=slippage,
        priority_fee_usd=priority_fee_usd,
        total_cost_pct=round(total_cost_pct, 4)
    )


def compute_exit_cost(
    entry_liquidity_usd: float,
    exit_return_pct: float,
    config: Optional[CostModelConfig] = None,
) -> TradeCost:
    """
    Compute slippage + priority fee for a single partial exit.

    Liquidity at exit is estimated using sqrt-scaling of price multiple,
    since AMM pool TVL tends to grow as price rises (LP rebalancing).
    This is a conservative estimate — HYPOTHESIS_INIT.

    Args:
        entry_liquidity_usd: Pool liquidity at listing time (USD)
        exit_return_pct    : Return at exit vs entry price (pct, e.g. 100.0 = 2x)
        config             : Cost model config (uses defaults if None)

    Returns:
        TradeCost for this single exit event
    """
    cfg = config or CostModelConfig()

    # Estimate pool liquidity at exit time
    if exit_return_pct <= 0:
        # On the way down, pool is typically thinner
        liquidity_at_exit = entry_liquidity_usd * 0.5
    else:
        price_multiple = 1.0 + (exit_return_pct / 100.0)
        liq_multiple = max(1.0, price_multiple ** 0.5)  # HYPOTHESIS_INIT: sqrt scaling
        liquidity_at_exit = entry_liquidity_usd * liq_multiple

    # Graduated slippage on exit liquidity
    if liquidity_at_exit < LIQUIDITY_MICRO_THRESHOLD:
        slippage = SLIPPAGE_MICRO
    elif liquidity_at_exit < LIQUIDITY_SMALL_THRESHOLD:
        slippage = SLIPPAGE_SMALL
    else:
        slippage = SLIPPAGE_MEDIUM

    priority_fee_usd = cfg.priority_fee_sol * cfg.sol_price_usd
    priority_fee_pct = (priority_fee_usd / cfg.trade_size_usd) * 100.0

    # One-sided cost (exit only)
    total_cost_pct = slippage + priority_fee_pct

    return TradeCost(
        slippage_pct=slippage,
        priority_fee_usd=priority_fee_usd,
        total_cost_pct=round(total_cost_pct, 4),
    )


async def load_p80_priority_fee_from_supabase() -> float:
    """
    Load P80 priority fee from actual Supabase metric_snapshots data.
    Returns SOL value. Falls back to DEFAULT_PRIORITY_FEE_SOL if unavailable.
    """
    try:
        from src.database.client import db_manager

        rows = await db_manager.query(
            "metric_snapshots",
            select="global_priority_fees_sol",
            limit=500
        )
        if not rows:
            return DEFAULT_PRIORITY_FEE_SOL

        fees = sorted(
            r["global_priority_fees_sol"]
            for r in rows
            if r.get("global_priority_fees_sol") and r["global_priority_fees_sol"] > 0
        )
        if not fees:
            return DEFAULT_PRIORITY_FEE_SOL

        p80_idx = int(len(fees) * 0.80)
        return fees[min(p80_idx, len(fees) - 1)]

    except Exception:
        return DEFAULT_PRIORITY_FEE_SOL
