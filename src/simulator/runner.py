"""
Scenario Runner — Runs one SimulatorConfig against the full dataset.

Combines dataset loading, portfolio simulation, and result packaging
into a single async call: run_scenario().
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict
from typing import Optional

from src.simulator.config import SimulatorConfig
from src.simulator.dataset import load_trades, get_dataset_meta
from src.simulator.portfolio import simulate_portfolio, PortfolioStats
from src.utils.logger import logger


@dataclass
class ScenarioResult:
    """Complete result of running one scenario."""
    config_label: str
    config: dict                     # SimulatorConfig as dict (for API serialization)
    stats: PortfolioStats
    dataset_total: int               # Total trades in dataset (before filtering)
    runtime_ms: float                # How long the simulation took

    def to_api_dict(self, include_trades: bool = False) -> dict:
        """Serialize for API response."""
        trade_details = []
        if include_trades:
            for r in self.stats.trade_results:
                if not r.entered:
                    continue
                trade_details.append({
                    "id": r.trade_id,
                    "symbol": r.symbol,
                    "score": r.score,
                    "mfe_pct": r.mfe_pct,
                    "hold_minutes": r.hold_minutes,
                    "simulated_return_pct": r.simulated_return_pct,
                    "simulated_exit_reason": r.simulated_exit_reason,
                    "actual_return_pct": r.actual_return_pct,
                    "actual_exit_reason": r.actual_exit_reason,
                    "pnl_usd": r.pnl_usd,
                    "position_size_usd": r.position_size_usd,
                    "partial_exits": [
                        {"fraction": p.fraction, "return_pct": p.return_pct, "reason": p.reason}
                        for p in r.partial_exits
                    ],
                })

        return {
            "label": self.config_label,
            "config": self.config,
            "roi_pct": self.stats.roi_pct,
            "final_equity": self.stats.final_equity,
            "realized_pnl_usd": self.stats.realized_pnl_usd,
            "win_rate": self.stats.win_rate,
            "win_count": self.stats.win_count,
            "loss_count": self.stats.loss_count,
            "skip_count": self.stats.skip_count,
            "total_entered": self.stats.win_count + self.stats.loss_count,
            "max_drawdown_pct": self.stats.max_drawdown_pct,
            "avg_return_pct": self.stats.avg_return_pct,
            "avg_mfe_captured_pct": self.stats.avg_mfe_captured_pct,
            "equity_curve": self.stats.equity_curve,
            "dataset_total": self.dataset_total,
            "runtime_ms": self.runtime_ms,
            "trades": trade_details,
        }


async def run_scenario(
    config: Optional[SimulatorConfig] = None,
    trades: Optional[list[dict]] = None,
    include_trades: bool = True,
) -> ScenarioResult:
    """
    Run a single scenario simulation.

    Args:
        config:         SimulatorConfig to use. Defaults to v2.1 baseline.
        trades:         Pre-loaded trade list (optional). If None, loads from Supabase.
        include_trades: Include per-trade detail in result.

    Returns:
        ScenarioResult with full stats and optional per-trade breakdown.
    """
    if config is None:
        config = SimulatorConfig.v21_baseline()

    if trades is None:
        trades = await load_trades()

    t0 = time.perf_counter()
    stats = simulate_portfolio(trades, config)
    runtime_ms = (time.perf_counter() - t0) * 1000

    label = config.label or config.to_label()
    logger.info(
        f"[Simulator] '{label}' → ROI: {stats.roi_pct:+.2f}% | "
        f"WinRate: {stats.win_rate:.1%} | "
        f"Trades: {stats.win_count + stats.loss_count} entered / {stats.skip_count} skipped | "
        f"Runtime: {runtime_ms:.1f}ms"
    )

    return ScenarioResult(
        config_label=label,
        config=config.to_dict(),
        stats=stats,
        dataset_total=len(trades),
        runtime_ms=round(runtime_ms, 2),
    )
