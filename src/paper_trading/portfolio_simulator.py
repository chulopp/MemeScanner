"""
Portfolio Simulator & Multi-Exit Strategy Optimizer — Fase 5 & 6
Simulates compounding portfolio balance ($10 starting capital, 20% position sizing)
across an extensive Take-Profit grid (+25% to +1000%) and Stop-Loss matrix (-30% to -70%).
Calculates milestone hit-rates, average hold times, and expected values (EV).
"""

from typing import Optional, Any
from dataclasses import dataclass, field

from src.backtest.cost_model import compute_trade_cost, CostModelConfig
from src.utils.logger import logger

DEFAULT_TP_GRID = [25.0, 50.0, 75.0, 100.0, 150.0, 200.0, 300.0, 500.0, 1000.0]
DEFAULT_SL_GRID = [-30.0, -50.0, -70.0]


@dataclass
class TradeSimulationRecord:
    token_address: str
    symbol: str
    tp_target_pct: float
    sl_target_pct: float
    position_size_usd: float
    gross_return_pct: float
    net_return_pct: float
    pnl_usd: float
    balance_after: float
    exit_reason: str  # 'TP_HIT' | 'SL_HIT' | 'TIMEOUT_24H' | 'DEAD'
    hold_duration_minutes: float


@dataclass
class StrategyMatrixResult:
    tp_target_pct: float
    sl_target_pct: float
    initial_balance: float
    final_balance: float
    net_pnl_usd: float
    roi_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    ev_per_trade_pct: float
    max_drawdown_pct: float
    avg_hold_minutes: float
    profit_factor: float
    trades: list[TradeSimulationRecord] = field(default_factory=list)


@dataclass
class MilestoneHitRate:
    target_pct: float
    reached_count: int
    total_signals: int
    hit_rate_pct: float
    avg_minutes_to_reach: float


class PortfolioSimulator:
    """
    Simulates compounding balance with realistic slippage cost models
    and evaluates multi-exit strategies to identify the optimal profit sweet spot.
    """

    def __init__(self, cost_config: Optional[CostModelConfig] = None):
        self.cost_config = cost_config or CostModelConfig()

    def _estimate_time_to_milestone(self, trajectory: dict[str, Any], target_return: float) -> float:
        """
        Estimates the earliest timeframe window in minutes where return >= target_return.
        Windows: 5m (5 min), 15m (15 min), 1h (60 min), 4h (240 min), 24h (1440 min).
        """
        windows = [
            ("5m", 5.0),
            ("15m", 15.0),
            ("1h", 60.0),
            ("4h", 240.0),
            ("24h", 1440.0)
        ]
        for w_name, w_mins in windows:
            peak = trajectory.get(f"peak_{w_name}") or trajectory.get(f"return_{w_name}") or 0.0
            if peak >= target_return:
                return w_mins
        return 1440.0

    def calculate_milestones(self, signals: list[dict[str, Any]]) -> list[MilestoneHitRate]:
        """
        Calculates hit-rates and average time to reach each Take-Profit milestone (+25% to +1000%).
        """
        if not signals:
            return []

        total_signals = len(signals)
        results: list[MilestoneHitRate] = []

        for target_pct in DEFAULT_TP_GRID:
            reached_count = 0
            durations: list[float] = []

            for sig in signals:
                ath_pct = sig.get("ath_return_pct") or sig.get("max_multiplier_pct") or sig.get("peak_24h") or sig.get("label_return_pct") or 0.0
                if ath_pct >= target_pct:
                    reached_count += 1
                    dur = self._estimate_time_to_milestone(sig, target_pct)
                    durations.append(dur)

            hit_rate = (reached_count / total_signals) * 100.0 if total_signals > 0 else 0.0
            avg_dur = sum(durations) / len(durations) if durations else 0.0

            results.append(MilestoneHitRate(
                target_pct=target_pct,
                reached_count=reached_count,
                total_signals=total_signals,
                hit_rate_pct=round(hit_rate, 1),
                avg_minutes_to_reach=round(avg_dur, 1)
            ))

        return results

    def simulate_strategy(
        self,
        signals: list[dict[str, Any]],
        tp_target_pct: float,
        sl_target_pct: float,
        initial_balance: float = 10.0,
        position_risk_pct: float = 20.0
    ) -> StrategyMatrixResult:
        """
        Simulates a specific (TP, SL) strategy on chronological signals with 20% compounding balance.
        """
        balance = initial_balance
        peak_balance = initial_balance
        max_drawdown_pct = 0.0

        trades: list[TradeSimulationRecord] = []
        winning_trades = 0
        losing_trades = 0
        total_profit_usd = 0.0
        total_loss_usd = 0.0

        for sig in signals:
            if balance <= 0.01:
                break  # Capital wiped out

            position_size = balance * (position_risk_pct / 100.0)
            liquidity_usd = sig.get("entry_liquidity_usd") or sig.get("liquidity_usd") or 5000.0
            ath_pct = sig.get("ath_return_pct") or sig.get("max_multiplier_pct") or sig.get("peak_24h") or sig.get("label_return_pct") or 0.0
            mae_pct = sig.get("mae_pct") or sig.get("max_drawdown_pct") or 0.0
            final_24h_pct = sig.get("return_24h") or sig.get("label_return_pct") or 0.0

            # Calculate trading cost (slippage + fee)
            cost_info = compute_trade_cost(liquidity_usd, self.cost_config)
            total_cost_pct = cost_info.total_cost_pct

            exit_reason = "TIMEOUT_24H"
            gross_return = final_24h_pct
            hold_mins = 1440.0

            # Determine trade outcome
            if ath_pct >= tp_target_pct:
                gross_return = tp_target_pct
                exit_reason = "TP_HIT"
                hold_mins = self._estimate_time_to_milestone(sig, tp_target_pct)
            elif mae_pct <= sl_target_pct or final_24h_pct <= sl_target_pct:
                gross_return = sl_target_pct
                exit_reason = "SL_HIT"
                hold_mins = 60.0  # Assumed mid-session drawdown stop
            elif sig.get("status") == "dead" or final_24h_pct <= -70.0:
                gross_return = -70.0
                exit_reason = "DEAD"
                hold_mins = 30.0

            # Net return after cost
            net_return_pct = gross_return - total_cost_pct
            pnl_usd = position_size * (net_return_pct / 100.0)

            balance += pnl_usd
            balance = max(balance, 0.0)

            if pnl_usd > 0:
                winning_trades += 1
                total_profit_usd += pnl_usd
            else:
                losing_trades += 1
                total_loss_usd += abs(pnl_usd)

            # Track peak balance & Max Drawdown
            if balance > peak_balance:
                peak_balance = balance
            drawdown = ((peak_balance - balance) / peak_balance) * 100.0 if peak_balance > 0 else 0.0
            if drawdown > max_drawdown_pct:
                max_drawdown_pct = drawdown

            trades.append(TradeSimulationRecord(
                token_address=sig.get("token_address", "UNKNOWN"),
                symbol=sig.get("symbol", "UNKNOWN"),
                tp_target_pct=tp_target_pct,
                sl_target_pct=sl_target_pct,
                position_size_usd=round(position_size, 4),
                gross_return_pct=round(gross_return, 2),
                net_return_pct=round(net_return_pct, 2),
                pnl_usd=round(pnl_usd, 4),
                balance_after=round(balance, 4),
                exit_reason=exit_reason,
                hold_duration_minutes=round(hold_mins, 1)
            ))

        total_trades = len(trades)
        win_rate = (winning_trades / total_trades) * 100.0 if total_trades > 0 else 0.0
        roi_pct = ((balance - initial_balance) / initial_balance) * 100.0 if initial_balance > 0 else 0.0
        profit_factor = (total_profit_usd / total_loss_usd) if total_loss_usd > 0 else (99.0 if total_profit_usd > 0 else 0.0)
        avg_hold = sum(t.hold_duration_minutes for t in trades) / total_trades if total_trades > 0 else 0.0

        ev_per_trade = sum(t.net_return_pct for t in trades) / total_trades if total_trades > 0 else 0.0

        return StrategyMatrixResult(
            tp_target_pct=tp_target_pct,
            sl_target_pct=sl_target_pct,
            initial_balance=initial_balance,
            final_balance=round(balance, 2),
            net_pnl_usd=round(balance - initial_balance, 2),
            roi_pct=round(roi_pct, 2),
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate_pct=round(win_rate, 1),
            ev_per_trade_pct=round(ev_per_trade, 2),
            max_drawdown_pct=round(max_drawdown_pct, 1),
            avg_hold_minutes=round(avg_hold, 1),
            profit_factor=round(profit_factor, 2),
            trades=trades
        )

    def run_matrix_simulation(
        self,
        signals: list[dict[str, Any]],
        initial_balance: float = 10.0,
        position_risk_pct: float = 20.0,
        tp_grid: Optional[list[float]] = None,
        sl_grid: Optional[list[float]] = None
    ) -> list[StrategyMatrixResult]:
        """
        Executes all combinations in (TP Grid x SL Grid) and returns them ranked by final balance.
        """
        tp_levels = tp_grid or DEFAULT_TP_GRID
        sl_levels = sl_grid or DEFAULT_SL_GRID
        matrix_results: list[StrategyMatrixResult] = []

        for tp in tp_levels:
            for sl in sl_levels:
                res = self.simulate_strategy(
                    signals=signals,
                    tp_target_pct=tp,
                    sl_target_pct=sl,
                    initial_balance=initial_balance,
                    position_risk_pct=position_risk_pct
                )
                matrix_results.append(res)

        # Rank by final balance descending
        matrix_results.sort(key=lambda x: x.final_balance, reverse=True)
        return matrix_results

    def render_cli_report(
        self,
        matrix_results: list[StrategyMatrixResult],
        milestones: list[MilestoneHitRate],
        initial_balance: float = 10.0,
        position_risk_pct: float = 20.0
    ) -> str:
        """
        Generates a formatted ASCII terminal report for CLI visualization.
        """
        lines = []
        lines.append("=" * 82)
        lines.append("📊 MEMESCANNER — VIRTUAL PORTFOLIO & MULTI-EXIT SIMULATION REPORT")
        lines.append(f"💰 Initial Capital: ${initial_balance:.2f} | Position Sizing: {position_risk_pct:.0f}% Compounding")
        lines.append("=" * 82)

        # 1. Milestone Distribution Section
        lines.append("\n🎯 [1] MILESTONE HIT-RATE DISTRIBUTION (% Token Mencapai Target)")
        lines.append("-" * 65)
        lines.append(f"{'Target TP':<12} | {'Hit Count':<10} | {'Hit Rate %':<12} | {'Avg Time to Reach':<18}")
        lines.append("-" * 65)
        for m in milestones:
            time_str = f"{m.avg_minutes_to_reach:.0f} menit" if m.avg_minutes_to_reach < 60 else f"{m.avg_minutes_to_reach/60:.1f} jam"
            lines.append(f"{'+'+str(int(m.target_pct))+'%':<12} | {m.reached_count:>3}/{m.total_signals:<6} | {m.hit_rate_pct:>9.1f}% | {time_str:>16}")
        lines.append("-" * 65)

        # 2. Top Strategy Matrix Ranking Section
        lines.append("\n🏆 [2] TOP STRATEGY RANKING (TP & SL Matrix Combinations)")
        lines.append("-" * 82)
        lines.append(f"{'Rank':<5} | {'Strategy (TP / SL)':<20} | {'Final $':<10} | {'ROI %':<10} | {'WinRate':<8} | {'EV/Trade':<9} | {'MaxDD':<7}")
        lines.append("-" * 82)

        for idx, s in enumerate(matrix_results[:10], 1):
            strat_label = f"+{int(s.tp_target_pct)}% TP / {int(s.sl_target_pct)}% SL"
            lines.append(
                f"#{idx:<4} | {strat_label:<20} | ${s.final_balance:>8.2f} | {s.roi_pct:>+8.1f}% | "
                f"{s.win_rate_pct:>6.1f}% | {s.ev_per_trade_pct:>+7.1f}% | {s.max_drawdown_pct:>5.1f}%"
            )
        lines.append("-" * 82)

        # 3. Best Strategy Summary
        if matrix_results:
            best = matrix_results[0]
            lines.append(f"\n💡 STRATEGI REKOMENDASI TERBAIK:")
            lines.append(f"   • Take-Profit Target : +{int(best.tp_target_pct)}%")
            lines.append(f"   • Stop-Loss Target   : {int(best.sl_target_pct)}%")
            lines.append(f"   • Proyeksi Saldo     : ${initial_balance:.2f} ➔ ${best.final_balance:.2f} ({best.roi_pct:+.1f}%)")
            lines.append(f"   • Win-Rate / EV      : {best.win_rate_pct:.1f}% Win Rate | {best.ev_per_trade_pct:+.1f}% EV per trade")
            lines.append(f"   • Rata-rata Durasi   : {best.avg_hold_minutes:.0f} menit per trade")

        lines.append("=" * 82)
        return "\n".join(lines)


portfolio_simulator = PortfolioSimulator()
