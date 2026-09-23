"""
Exit Engine — Core trade simulation for the offline backtester.

Simulates the full lifecycle of a single trade using the price path model
(entry → MFE → exit) and a given SimulatorConfig.

The engine re-derives ALL exit decisions from scratch based on the config,
ignoring the actual exit_reason stored in the database. This allows testing
any combination of parameters against historical price behavior.

Exit priority (evaluated in order):
  1. Entry filter: skip if opportunity_score < threshold
  2. Ascending phase: collect TP partial exits (tp0, tp1, tp2, tp3)
  3. Trailing activation: if return >= trailing_start_return_pct after TP hits
  4. Time-decay: if hold_minutes >= exit_minutes AND MFE < mfe_threshold → TIME_DECAY
  5. Descending phase: check trailing stop → breakeven SL → base SL → timeout
  6. Blended return: weighted average across all partial exits
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.simulator.config import SimulatorConfig
from src.simulator.price_model import (
    did_reach,
    exit_return_on_descend,
    compute_trailing_stop_price,
    get_trailing_pct,
)


@dataclass
class PartialExit:
    """One partial exit event (a TP tier hit or final exit)."""
    fraction: float          # Fraction of TOTAL original position sold
    return_pct: float        # Return % at which this exit happened
    reason: str              # 'TP0'|'TP1'|'TP2'|'TP3'|'SL'|'TRAILING'|'TIME_DECAY'|'TIMEOUT'|'BREAKEVEN_SL'


@dataclass
class TradeResult:
    """Full simulation result for one trade."""
    trade_id: str
    symbol: str
    score: float
    mfe_pct: float
    hold_minutes: float

    # Was this trade entered in this scenario?
    entered: bool = True
    skip_reason: str = ""

    # Exit details
    partial_exits: list[PartialExit] = field(default_factory=list)
    simulated_return_pct: float = 0.0    # Blended weighted return
    simulated_exit_reason: str = ""

    # For comparison
    actual_return_pct: float = 0.0
    actual_exit_reason: str = ""

    # Position size used (after portfolio accounting)
    position_size_usd: float = 0.0
    pnl_usd: float = 0.0


def simulate_trade(
    trade: dict,
    config: SimulatorConfig,
    position_size_usd: float,
) -> TradeResult:
    """
    Simulate a single trade against the price path model using the given config.

    Args:
        trade:             Raw dict from paper_trade_positions.
        config:            SimulatorConfig with parameters to test.
        position_size_usd: Dollar size of this simulated position.

    Returns:
        TradeResult with blended simulated_return_pct and pnl_usd.
    """
    trade_id = trade.get("id") or ""
    symbol = trade.get("symbol") or "?"
    score = float(trade.get("opportunity_score_at_entry") or 0.0)
    mfe_pct = float(trade.get("mfe_pct") or 0.0)
    hold_minutes = float(trade.get("hold_duration_minutes") or 0.0)
    actual_return_pct = float(trade.get("exit_price_usd") or 0.0)
    actual_exit_reason = trade.get("exit_reason") or "UNKNOWN"

    # Compute actual return_pct from entry/exit prices
    entry_price = float(trade.get("entry_price_usd") or 0.0)
    exit_price = float(trade.get("exit_price_usd") or 0.0)
    if entry_price > 0:
        actual_return_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:
        actual_return_pct = 0.0

    result = TradeResult(
        trade_id=trade_id,
        symbol=symbol,
        score=score,
        mfe_pct=mfe_pct,
        hold_minutes=hold_minutes,
        actual_return_pct=actual_return_pct,
        actual_exit_reason=actual_exit_reason,
        position_size_usd=position_size_usd,
    )

    # ── 1. Entry filter ──────────────────────────────────────────
    if score < config.opportunity_threshold:
        result.entered = False
        result.skip_reason = f"score {score:.1f} < threshold {config.opportunity_threshold:.1f}"
        return result

    # ── State tracking ───────────────────────────────────────────
    remaining_fraction = 1.0    # Fraction of position still held
    partial_exits: list[PartialExit] = []
    breakeven_active = False
    trailing_active = False
    ath_return = mfe_pct        # The MFE is the highest price seen

    # ── 2. Ascending phase: collect TP hits ──────────────────────
    tp_tiers = [
        ("TP0", config.tp0_pct, config.tp0_sell_fraction),
        ("TP1", config.tp1_pct, config.tp1_sell_fraction),
        ("TP2", config.tp2_pct, config.tp2_sell_fraction),
        ("TP3", config.tp3_pct, config.tp3_sell_fraction),
    ]

    for label, tp_target, sell_frac in tp_tiers:
        if remaining_fraction <= 0:
            break
        if did_reach(mfe_pct, tp_target):
            actual_sell = min(sell_frac, remaining_fraction)
            partial_exits.append(PartialExit(
                fraction=actual_sell,
                return_pct=tp_target,
                reason=label,
            ))
            remaining_fraction -= actual_sell

            # Activate breakeven SL once TP0 >= breakeven_trigger
            if tp_target >= config.breakeven_trigger_pct:
                breakeven_active = True

            # Activate trailing if return crossed trailing_start_return_pct
            if tp_target >= config.trailing_start_return_pct:
                trailing_active = True

    # Also activate trailing if MFE alone crossed the threshold (e.g., no TP3 hit but MFE > 500%)
    if mfe_pct >= config.trailing_start_return_pct:
        trailing_active = True

    # ── 3. Time-decay override (highest priority after TPs) ──────
    if (hold_minutes >= config.time_decay_exit_minutes
            and mfe_pct < config.time_decay_mfe_threshold_pct
            and remaining_fraction > 0):
        # Time-decay fires: exit at actual_return_pct (real market data)
        partial_exits.append(PartialExit(
            fraction=remaining_fraction,
            return_pct=actual_return_pct,
            reason="TIME_DECAY",
        ))
        remaining_fraction = 0.0
        _finalize(result, partial_exits, position_size_usd, "TIME_DECAY")
        return result

    # ── 4. Descending phase: find first trigger hit ───────────────
    if remaining_fraction > 0:
        # Determine effective SL for remaining position
        if breakeven_active:
            effective_sl = config.breakeven_sl_pct
        elif hold_minutes >= config.time_decay_tighten_minutes and mfe_pct < config.time_decay_mfe_threshold_pct:
            effective_sl = config.time_decay_sl_tighten_pct
        else:
            effective_sl = config.stop_loss_pct

        final_return = actual_return_pct
        final_reason = "TIMEOUT" if hold_minutes >= config.max_hold_hours * 60 else "SL"

        if trailing_active:
            # Compute trailing stop level from ATH (MFE)
            trail_pct = get_trailing_pct(
                ath_return,
                config.trailing_tier1_pct,
                config.trailing_tier2_pct,
                config.trailing_tier3_pct,
            )
            trailing_level = compute_trailing_stop_price(ath_return, trail_pct)

            # Trailing fires if trailing_level > exit_return (i.e., price would have hit trailing first)
            if trailing_level > actual_return_pct and trailing_level > effective_sl:
                final_return = trailing_level
                final_reason = "TRAILING"
            elif effective_sl > actual_return_pct:
                final_return = effective_sl
                final_reason = "SL" if not breakeven_active else "BREAKEVEN_SL"
            else:
                final_return = actual_return_pct
                final_reason = actual_exit_reason  # Timeout or actual exit

        elif effective_sl > actual_return_pct:
            # SL fires: cap at effective SL (mirrors SL capping in v2.1)
            final_return = effective_sl
            final_reason = "SL" if not breakeven_active else "BREAKEVEN_SL"
        else:
            # No SL hit — exited at actual price (timeout or actual reason)
            final_return = actual_return_pct
            final_reason = actual_exit_reason

        partial_exits.append(PartialExit(
            fraction=remaining_fraction,
            return_pct=final_return,
            reason=final_reason,
        ))
        remaining_fraction = 0.0
        _finalize(result, partial_exits, position_size_usd, final_reason)

    return result


def _finalize(
    result: TradeResult,
    partial_exits: list[PartialExit],
    position_size_usd: float,
    primary_exit_reason: str,
) -> None:
    """Compute blended weighted return and finalize the TradeResult in-place."""
    result.partial_exits = partial_exits

    # Blended return = sum(fraction_i * return_i)
    blended = sum(p.fraction * p.return_pct for p in partial_exits)
    result.simulated_return_pct = round(blended, 4)
    result.simulated_exit_reason = primary_exit_reason
    result.pnl_usd = round(position_size_usd * (blended / 100.0), 4)
