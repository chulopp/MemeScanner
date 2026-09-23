"""
Portfolio Simulator — Tracks equity, position sizing, and PnL across all trades.

Starting capital: $100.00 (fixed).
Position sizing: dynamic, based on current equity * position_risk_pct / 100.
Positions are allocated sequentially in chronological order (as they happened in live paper trade).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from src.simulator.config import SimulatorConfig
from src.simulator.exit_engine import TradeResult, simulate_trade


STARTING_CAPITAL = 100.0


@dataclass
class PortfolioStats:
    """Aggregated statistics for a complete simulation run."""
    starting_capital: float = STARTING_CAPITAL
    final_equity: float = STARTING_CAPITAL
    realized_pnl_usd: float = 0.0
    roi_pct: float = 0.0
    max_drawdown_pct: float = 0.0       # Largest peak-to-trough equity drop (%)
    win_count: int = 0
    loss_count: int = 0
    skip_count: int = 0                 # Trades skipped by threshold filter
    total_trades_evaluated: int = 0
    win_rate: float = 0.0               # win_count / (win_count + loss_count)
    avg_return_pct: float = 0.0
    avg_mfe_captured_pct: float = 0.0   # avg(simulated_return / mfe) for entered trades
    equity_curve: list[float] = field(default_factory=list)   # Equity at each trade close
    trade_results: list[TradeResult] = field(default_factory=list)


def simulate_portfolio(
    trades: list[dict],
    config: SimulatorConfig,
) -> PortfolioStats:
    """
    Run a full portfolio simulation over the provided trade dataset.

    Trades are processed in order (chronological). Each trade uses
    `config.position_risk_pct % of current equity` as position size.

    Args:
        trades:  List of trade dicts from paper_trade_positions.
        config:  SimulatorConfig with all parameters.

    Returns:
        PortfolioStats with full equity curve and per-trade results.
    """
    equity = STARTING_CAPITAL
    peak_equity = STARTING_CAPITAL
    max_drawdown_pct = 0.0

    results: list[TradeResult] = []
    equity_curve: list[float] = [equity]
    returns: list[float] = []
    captured_ratios: list[float] = []

    for trade in trades:
        # Dynamic position sizing based on current equity
        pos_size = equity * (config.position_risk_pct / 100.0)
        # Clamp minimum to prevent micro positions
        pos_size = max(pos_size, 0.01)

        result = simulate_trade(trade, config, pos_size)
        results.append(result)

        if not result.entered:
            continue

        # Update equity
        equity += result.pnl_usd
        equity_curve.append(round(equity, 4))
        returns.append(result.simulated_return_pct)

        # Track max drawdown
        if equity > peak_equity:
            peak_equity = equity
        drawdown = ((peak_equity - equity) / peak_equity) * 100.0
        if drawdown > max_drawdown_pct:
            max_drawdown_pct = drawdown

        # MFE capture ratio (only meaningful when MFE > 0 and position had upside)
        if result.mfe_pct > 5.0:  # Exclude near-flat tokens (MFE < 5%)
            captured = result.simulated_return_pct / result.mfe_pct
            # Clamp to [-1, 1] to prevent extreme values from 0-MFE tokens
            captured = max(-1.0, min(1.0, captured))
            captured_ratios.append(captured)

    # Aggregate stats
    entered = [r for r in results if r.entered]
    wins = [r for r in entered if r.simulated_return_pct > 0]
    losses = [r for r in entered if r.simulated_return_pct <= 0]
    skipped = [r for r in results if not r.entered]

    win_rate = len(wins) / len(entered) if entered else 0.0
    avg_return = sum(returns) / len(returns) if returns else 0.0
    avg_captured = sum(captured_ratios) / len(captured_ratios) if captured_ratios else 0.0

    realized_pnl = equity - STARTING_CAPITAL
    roi_pct = (realized_pnl / STARTING_CAPITAL) * 100.0

    return PortfolioStats(
        starting_capital=STARTING_CAPITAL,
        final_equity=round(equity, 4),
        realized_pnl_usd=round(realized_pnl, 4),
        roi_pct=round(roi_pct, 4),
        max_drawdown_pct=round(max_drawdown_pct, 2),
        win_count=len(wins),
        loss_count=len(losses),
        skip_count=len(skipped),
        total_trades_evaluated=len(results),
        win_rate=round(win_rate, 4),
        avg_return_pct=round(avg_return, 4),
        avg_mfe_captured_pct=round(avg_captured, 4),
        equity_curve=equity_curve,
        trade_results=results,
    )
