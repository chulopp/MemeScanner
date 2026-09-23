"""
Price Model — Simulates trigger evaluation using the entry→MFE→exit price path.

Since we don't have tick-level candles, we use the following assumption:
  Phase 1 (Ascending):  price moves from entry up to MFE linearly
  Phase 2 (Descending): price moves from MFE down to exit_price

This allows us to:
  - Detect which TP tiers would have been hit (ascending phase)
  - Detect trailing stop, breakeven SL, or base SL hit (descending phase)
  - Evaluate time-decay based on hold_duration_minutes from actual data

Limitation: This model cannot distinguish "went up to +300%, came back to +50%,
then went up to MFE +500%". For the majority of meme token price paths (spike
and dump), the ascending-then-descending model is a reasonable approximation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PricePoint:
    """A resolved price evaluation point during replay."""
    return_pct: float       # Return % from entry at this point
    price_usd: float        # Absolute price at this point
    phase: str              # 'ASCENDING' | 'DESCENDING'


@dataclass
class TriggerResult:
    """Result of checking one trigger condition against the price model."""
    triggered: bool
    trigger_return_pct: float  # Return % at which the trigger fired (or 0 if not triggered)
    note: str = ""


def did_reach(mfe_pct: float, target_pct: float) -> bool:
    """Returns True if the token's MFE (max return) reached or exceeded the target."""
    return mfe_pct >= target_pct


def exit_return_on_descend(
    mfe_pct: float,
    exit_return_pct: float,
    trigger_pct: float,
) -> float:
    """
    Returns the return_pct at which a descending trigger would fire.

    Assumes price descends linearly from MFE to actual exit.
    If the trigger level is between MFE and exit, it fires there.
    Otherwise returns None (trigger was not hit).

    Args:
        mfe_pct:        Peak return (%) reached during the trade.
        exit_return_pct: Actual final return (%) when trade was closed.
        trigger_pct:     The threshold to check (e.g., SL at -30%).

    Returns:
        trigger_pct if trigger is between exit and MFE (i.e., hit during descent),
        else exit_return_pct (trigger not hit, trade exited at actual price).
    """
    # On descent, price drops from mfe_pct toward exit_return_pct
    # Trigger fires if trigger_pct is above exit_return_pct (i.e., we crossed it on the way down)
    if trigger_pct > exit_return_pct:
        return trigger_pct
    return exit_return_pct


def compute_trailing_stop_price(
    ath_return_pct: float,
    trail_pct_from_ath: float,
) -> float:
    """
    Computes the trailing stop trigger level (return %) given ATH and trail %.

    Example: ATH at +200%, trail 25% → stop fires if price drops 25% from ATH level.
    ATH price = entry * (1 + 2.00) = 3x entry
    Stop = 3x * (1 - 0.25) = 2.25x entry → return = +125%
    """
    ath_multiplier = 1.0 + (ath_return_pct / 100.0)
    stop_multiplier = ath_multiplier * (1.0 - trail_pct_from_ath / 100.0)
    return (stop_multiplier - 1.0) * 100.0


def get_trailing_pct(ath_return_pct: float,
                     tier1_pct: float,
                     tier2_pct: float,
                     tier3_pct: float) -> float:
    """
    Returns the applicable trailing % given the current ATH return.
    Tier boundaries:
      ATH < 200%  → tier1_pct (loosest)
      200-500%    → tier2_pct
      > 500%      → tier3_pct (tightest)
    """
    if ath_return_pct >= 500.0:
        return tier3_pct
    elif ath_return_pct >= 200.0:
        return tier2_pct
    else:
        return tier1_pct
