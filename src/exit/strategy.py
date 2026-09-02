"""
Exit Strategy Engine — Fase 1
Multi-tier Take Profit + Moonbag Adaptive Trailing Stop + Stop Loss.

Design decisions (Grill Session 2026-09-01):
  - Split posisi: 80% tiered TP + 20% moonbag
  - Tiered TP   : 30% @2x (100%), 30% @4x (300%), 20% @6x (500%)
  - Moonbag exit: adaptive trailing stop dari ATH
  - Trailing    : <2x ATH → trail 40%, 2–5x ATH → 50%, >10x ATH → 60%
  - Stop Loss   : fixed -50% dari entry price
  - Cost model  : setiap partial exit incur slippage terpisah

HYPOTHESIS_INIT — semua angka dapat dikalibrasi via Bayesian optimizer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────
#  Data classes
# ─────────────────────────────────────────────────────────────────

@dataclass
class TpTier:
    """One take-profit tier."""
    sell_fraction: float      # Fraction of TOTAL position to sell (0–1)
    target_return_pct: float  # Trigger when return from entry >= this


@dataclass
class TrailingTier:
    """Adaptive trailing stop tier."""
    multiplier_threshold: float   # ATH multiplier from entry >= this triggers the tier
    trail_pct_from_ath: float     # Trail this % below ATH (e.g. 40 → exit if price drops 40% from ATH)


@dataclass
class ExitStrategyConfig:
    """
    All parameters tagged HYPOTHESIS_INIT — adjustable by Bayesian optimizer.

    Position split:
        tp_tiers fractions + moonbag_fraction must sum to 1.0
        Default: 0.30 + 0.30 + 0.20 + 0.20 (moonbag) = 1.0
    """
    # Tiered take-profits  — HYPOTHESIS_INIT
    tp_tiers: list[TpTier] = field(default_factory=lambda: [
        TpTier(sell_fraction=0.30, target_return_pct=100.0),   # 30% position @ 2x
        TpTier(sell_fraction=0.30, target_return_pct=300.0),   # 30% position @ 4x
        TpTier(sell_fraction=0.20, target_return_pct=500.0),   # 20% position @ 6x
    ])

    # Moonbag fraction (remainder after all TP tiers) — HYPOTHESIS_INIT
    moonbag_fraction: float = 0.20

    # Stop-loss from entry price (negative pct) — HYPOTHESIS_INIT
    sl_pct: float = -50.0

    # Adaptive trailing stop tiers for moonbag — HYPOTHESIS_INIT
    trailing_tiers: list[TrailingTier] = field(default_factory=lambda: [
        TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=60.0),  # >10x: trail 60%
        TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=50.0),  # 5–10x: trail 50%
        TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=40.0),  # 2–5x: trail 40%
        # Below 2x: no adaptive trailing — moonbag held until timeout/SL
    ])


DEFAULT_EXIT_CONFIG = ExitStrategyConfig()


@dataclass
class PartialExitEvent:
    """Record of a single partial exit (one TP tier, SL, or moonbag trailing)."""
    fraction_sold: float        # Fraction of original position sold
    exit_return_pct: float      # Return at exit vs entry price
    exit_reason: str            # 'TP1'|'TP2'|'TP3'|'SL'|'TRAILING_STOP'|'TIMEOUT_24H'
    slippage_pct: float         # Slippage incurred at this exit


@dataclass
class ExitResult:
    """Complete exit simulation result for one token."""
    # Weighted realized return across all exit events (entry cost already deducted)
    realized_return_pct: float

    # Breakdown
    partial_exits: list[PartialExitEvent] = field(default_factory=list)
    moonbag_return_pct: Optional[float] = None       # Return on moonbag portion (None if no moonbag)
    moonbag_exit_reason: str = "STILL_OPEN"          # 'TRAILING_STOP'|'TIMEOUT_24H'|'STILL_OPEN'|'SL'

    # Cost transparency
    total_entry_cost_pct: float = 0.0                # Entry slippage + fee
    total_exit_cost_pct: float = 0.0                 # Sum of all exit slippages + fees
    total_cost_pct: float = 0.0                      # total_entry_cost + total_exit_cost

    # Flags
    sl_triggered: bool = False
    tp_tiers_hit: int = 0                            # How many TP tiers fired


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────

def _slippage_for_liquidity(liquidity_usd: float) -> float:
    """
    Graduated slippage estimate.
    Mirrors CostModel tiers — HYPOTHESIS_INIT.
    """
    if liquidity_usd < 50_000.0:
        return 5.0
    elif liquidity_usd < 200_000.0:
        return 2.0
    else:
        return 0.5


def _estimate_exit_liquidity(entry_liquidity_usd: float, return_pct: float) -> float:
    """
    Roughly estimate pool liquidity at a given return level.
    As price rises, liquidity in the pool also tends to increase.
    Use a conservative multiplier (sqrt of price multiple) to avoid over-optimism.

    HYPOTHESIS_INIT — can be improved with empirical data from backtest.
    """
    if return_pct <= 0:
        return entry_liquidity_usd * 0.5   # If price fell, pool often thinner
    price_multiple = 1.0 + (return_pct / 100.0)
    # Liquidity scales roughly with sqrt of price multiple (LP rebalancing)
    liquidity_multiple = max(1.0, price_multiple ** 0.5)
    return entry_liquidity_usd * liquidity_multiple


def _get_trailing_pct(ath_multiplier: float, trailing_tiers: list[TrailingTier]) -> Optional[float]:
    """
    Return the applicable trailing stop % from ATH based on ATH multiplier.
    Returns None if no trailing tier applies (moonbag held until timeout).
    Tiers should be ordered from highest to lowest threshold.
    """
    for tier in sorted(trailing_tiers, key=lambda t: t.multiplier_threshold, reverse=True):
        if ath_multiplier >= tier.multiplier_threshold:
            return tier.trail_pct_from_ath
    return None


def _moonbag_is_stopped(current_price: float, ath_price: float, trail_pct: float) -> bool:
    """Returns True if moonbag trailing stop is triggered."""
    if ath_price <= 0:
        return False
    drawdown_from_ath = ((ath_price - current_price) / ath_price) * 100.0
    return drawdown_from_ath >= trail_pct


# ─────────────────────────────────────────────────────────────────
#  Price trajectory builder
# ─────────────────────────────────────────────────────────────────

def _build_price_points(
    entry_price: float,
    price_changes: dict[str, float],
) -> list[tuple[float, float]]:
    """
    Build a time-ordered list of (minutes_from_entry, price) price points.

    Args:
        entry_price: price at T+2 entry (USD)
        price_changes: dict of window -> return_pct.
                       Keys: 'm5', 'h1', 'h6', 'h24' (from DexScreener)

    Returns:
        Sorted list of (minutes, price) tuples
    """
    window_minutes = {
        "m5": 5.0,
        "h1": 60.0,
        "h6": 360.0,
        "h24": 1440.0,
    }
    points = [(0.0, entry_price)]
    for key, minutes in window_minutes.items():
        pct = price_changes.get(key)
        if pct is not None:
            price = entry_price * (1.0 + pct / 100.0)
            points.append((minutes, max(price, 0.0)))
    return sorted(points, key=lambda x: x[0])


def _interpolate_price_at_return(
    target_return_pct: float,
    price_points: list[tuple[float, float]],
    entry_price: float,
) -> Optional[tuple[float, float]]:
    """
    Find earliest time + price at which target_return_pct was first crossed.
    Returns (minutes, price) or None if target never reached.
    Linear interpolation between known price points.
    """
    target_price = entry_price * (1.0 + target_return_pct / 100.0)
    for i in range(len(price_points) - 1):
        t0, p0 = price_points[i]
        t1, p1 = price_points[i + 1]
        if p0 <= target_price <= p1 or p0 >= target_price >= p1:
            # Linear interpolation
            if abs(p1 - p0) < 1e-12:
                frac = 0.0
            else:
                frac = (target_price - p0) / (p1 - p0)
            t_cross = t0 + frac * (t1 - t0)
            return (t_cross, target_price)
    # Check if last point already exceeds target
    if price_points and price_points[-1][1] >= target_price:
        return (price_points[-1][0], price_points[-1][1])
    return None


# ─────────────────────────────────────────────────────────────────
#  Core simulate_exit function
# ─────────────────────────────────────────────────────────────────

def simulate_exit(
    entry_price: float,
    price_changes: dict[str, float],
    entry_liquidity_usd: float = 5_000.0,
    config: Optional[ExitStrategyConfig] = None,
    entry_cost_pct: float = 0.0,
) -> ExitResult:
    """
    Simulate exit strategy on a single token using historical price trajectory.

    Args:
        entry_price      : Token price at T+2 (USD). Must be > 0.
        price_changes    : Dict of {window: return_pct} from DexScreener.
                           e.g. {"m5": 30.0, "h1": -10.0, "h6": 150.0, "h24": 80.0}
        entry_liquidity_usd: Pool liquidity at listing (USD). Used for slippage estimate.
        config           : ExitStrategyConfig. Defaults to DEFAULT_EXIT_CONFIG.
        entry_cost_pct   : Entry slippage + fee already paid (from CostModel). Pure cost.

    Returns:
        ExitResult with realized_return_pct and breakdown.
    """
    cfg = config or DEFAULT_EXIT_CONFIG

    if entry_price <= 0:
        return ExitResult(realized_return_pct=0.0, total_entry_cost_pct=entry_cost_pct, total_cost_pct=entry_cost_pct)

    price_points = _build_price_points(entry_price, price_changes)

    # ── Track state ──────────────────────────────────────────────
    partial_exits: list[PartialExitEvent] = []
    remaining_fraction = 1.0          # fraction of total position still held
    tp_tiers_hit = 0
    sl_triggered = False
    total_exit_cost_pct = 0.0

    # ── Get price extremes ───────────────────────────────────────
    max_price = max(p for _, p in price_points) if price_points else entry_price
    min_price = min(p for _, p in price_points) if price_points else entry_price
    final_price = price_points[-1][1] if price_points else entry_price

    ath_return_pct = ((max_price - entry_price) / entry_price) * 100.0
    min_return_pct = ((min_price - entry_price) / entry_price) * 100.0

    # ── Step 1: Check Stop Loss ──────────────────────────────────
    # If price ever fell to SL level before any TP, cut the whole position
    sl_price = entry_price * (1.0 + cfg.sl_pct / 100.0)

    # Determine if SL was hit BEFORE the first TP
    first_tp_return = cfg.tp_tiers[0].target_return_pct if cfg.tp_tiers else float("inf")
    sl_time = None
    first_tp_time = None

    # Find when SL was first hit
    if min_price <= sl_price:
        result = _interpolate_price_at_return(cfg.sl_pct, price_points, entry_price)
        if result:
            sl_time = result[0]

    # Find when first TP was hit
    if ath_return_pct >= first_tp_return:
        result = _interpolate_price_at_return(first_tp_return, price_points, entry_price)
        if result:
            first_tp_time = result[0]

    # If SL hit before any TP → exit everything at SL
    if sl_time is not None and (first_tp_time is None or sl_time <= first_tp_time):
        sl_slippage = _slippage_for_liquidity(entry_liquidity_usd * 0.5)  # Pool thinned on the way down
        exit_cost = sl_slippage
        partial_exits.append(PartialExitEvent(
            fraction_sold=1.0,
            exit_return_pct=cfg.sl_pct,
            exit_reason="SL",
            slippage_pct=sl_slippage,
        ))
        total_exit_cost_pct += exit_cost
        sl_triggered = True

        realized = cfg.sl_pct - entry_cost_pct - total_exit_cost_pct
        return ExitResult(
            realized_return_pct=realized,
            partial_exits=partial_exits,
            moonbag_exit_reason="SL",
            total_entry_cost_pct=entry_cost_pct,
            total_exit_cost_pct=total_exit_cost_pct,
            total_cost_pct=entry_cost_pct + total_exit_cost_pct,
            sl_triggered=True,
            tp_tiers_hit=0,
        )

    # ── Step 2: Fire TP tiers ────────────────────────────────────
    # Only tiers whose target was actually reached
    weighted_return_sum = 0.0   # fraction * return contribution
    ath_price = entry_price

    for tier in cfg.tp_tiers:
        if ath_return_pct < tier.target_return_pct:
            break  # This tier and all subsequent tiers not reached
        if remaining_fraction <= 0:
            break

        frac = min(tier.sell_fraction, remaining_fraction)
        exit_liq = _estimate_exit_liquidity(entry_liquidity_usd, tier.target_return_pct)
        slip = _slippage_for_liquidity(exit_liq)

        partial_exits.append(PartialExitEvent(
            fraction_sold=frac,
            exit_return_pct=tier.target_return_pct,
            exit_reason=f"TP{tp_tiers_hit + 1}",
            slippage_pct=slip,
        ))

        weighted_return_sum += frac * (tier.target_return_pct - slip)
        total_exit_cost_pct += frac * slip
        remaining_fraction -= frac
        remaining_fraction = max(remaining_fraction, 0.0)
        tp_tiers_hit += 1

        # Update ATH tracker
        tp_price = entry_price * (1.0 + tier.target_return_pct / 100.0)
        if tp_price > ath_price:
            ath_price = tp_price

    # ── Step 3: Moonbag exit ─────────────────────────────────────
    moonbag_return_pct: Optional[float] = None
    moonbag_exit_reason = "TIMEOUT_24H"

    moonbag_fraction = remaining_fraction  # Whatever's left

    if moonbag_fraction > 1e-6:
        # Update ATH to actual maximum
        ath_price = max(ath_price, max_price)
        ath_multiplier = ath_price / entry_price

        trail_pct = _get_trailing_pct(ath_multiplier, cfg.trailing_tiers)

        # Check if trailing stop was triggered
        if trail_pct is not None and _moonbag_is_stopped(final_price, ath_price, trail_pct):
            # Trailing stop triggered — exit moonbag at (ATH * (1 - trail_pct/100))
            trail_exit_price = ath_price * (1.0 - trail_pct / 100.0)
            trail_return_pct = ((trail_exit_price - entry_price) / entry_price) * 100.0
            trail_liq = _estimate_exit_liquidity(entry_liquidity_usd, trail_return_pct)
            trail_slip = _slippage_for_liquidity(trail_liq)

            partial_exits.append(PartialExitEvent(
                fraction_sold=moonbag_fraction,
                exit_return_pct=trail_return_pct,
                exit_reason="TRAILING_STOP",
                slippage_pct=trail_slip,
            ))
            weighted_return_sum += moonbag_fraction * (trail_return_pct - trail_slip)
            total_exit_cost_pct += moonbag_fraction * trail_slip
            moonbag_return_pct = trail_return_pct
            moonbag_exit_reason = "TRAILING_STOP"

        else:
            # Moonbag held to end of observation window (24h)
            final_return_pct = ((final_price - entry_price) / entry_price) * 100.0
            final_liq = _estimate_exit_liquidity(entry_liquidity_usd, final_return_pct)
            final_slip = _slippage_for_liquidity(final_liq)

            partial_exits.append(PartialExitEvent(
                fraction_sold=moonbag_fraction,
                exit_return_pct=final_return_pct,
                exit_reason="TIMEOUT_24H",
                slippage_pct=final_slip,
            ))
            weighted_return_sum += moonbag_fraction * (final_return_pct - final_slip)
            total_exit_cost_pct += moonbag_fraction * final_slip
            moonbag_return_pct = final_return_pct
            moonbag_exit_reason = "TIMEOUT_24H"

    # ── Step 4: Final realized return ────────────────────────────
    # Subtract entry cost proportionally
    realized = weighted_return_sum - entry_cost_pct

    return ExitResult(
        realized_return_pct=round(realized, 4),
        partial_exits=partial_exits,
        moonbag_return_pct=moonbag_return_pct,
        moonbag_exit_reason=moonbag_exit_reason,
        total_entry_cost_pct=entry_cost_pct,
        total_exit_cost_pct=round(total_exit_cost_pct, 4),
        total_cost_pct=round(entry_cost_pct + total_exit_cost_pct, 4),
        sl_triggered=sl_triggered,
        tp_tiers_hit=tp_tiers_hit,
    )
