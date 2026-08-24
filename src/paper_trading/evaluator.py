"""
Evaluator — Fase 5
Calculates aggregate paper trading performance metrics from signal_outcomes:
1. Hit-Rate: % of signals that became runners (≥2x) within 24h.
2. Expected Value (EV): Mean net return after realistic cost model.
3. Baseline Comparison: signal hit-rate vs baseline token hit-rate.
4. ATH Analysis: Average peak return vs actual window return.

Uses DUAL cost models as agreed:
  - Cost Model A (Fase 4): Conservative (5% / 2% / 0.5% slippage tiers)
  - Cost Model B (PRD 10.3): Aggressive (17.5% / 10% / 5% round-trip slippage tiers)
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.database.client import db_manager
from src.utils.logger import logger


# --- Dual Cost Models ---

def _cost_model_a(liquidity_usd: float) -> float:
    """Fase 4 conservative cost model (round-trip)."""
    if liquidity_usd < 50_000:
        return 10.0  # 5% entry + 5% exit
    elif liquidity_usd < 200_000:
        return 4.0   # 2% entry + 2% exit
    else:
        return 1.0   # 0.5% + 0.5%

def _cost_model_b(liquidity_usd: float) -> float:
    """PRD Section 10.3 aggressive cost model (round-trip)."""
    if liquidity_usd < 10_000:
        return 17.5  # 15-20% mid-point
    elif liquidity_usd < 50_000:
        return 10.0  # 10% round-trip
    else:
        return 5.0   # 4-6% mid-point


@dataclass
class EvaluationResult:
    total_signals: int
    total_baselines: int
    signal_runners: int
    signal_dead: int
    signal_neutral: int
    baseline_runners: int
    signal_hit_rate: float           # runner% for signals
    baseline_hit_rate: float         # runner% for baselines
    margin_over_baseline: float      # signal_hit_rate - baseline_hit_rate
    ev_per_signal_a: float           # EV with cost model A (conservative)
    ev_per_signal_b: float           # EV with cost model B (PRD 10.3)
    avg_runner_return: float
    avg_loss: float
    avg_ath_return: float            # Average peak return (timing loss indicator)
    avg_mae: float                   # Average max drawdown
    observation_days: float
    meets_dod: bool                  # Meets PRD Definition of Done


async def evaluate_paper_trading(window: str = "24h") -> EvaluationResult:
    """
    Computes aggregate performance metrics from paper_signals + signal_outcomes.
    """
    await db_manager.initialize()
    logger.info(f"📊 Evaluating paper trading performance (window: {window})...")

    # Fetch all signals
    all_signals = await db_manager.query("paper_signals", limit=5000)
    if not all_signals:
        logger.warning("No signals found in paper_signals table.")
        return _empty_result()

    signals = [s for s in all_signals if not s.get("is_baseline", False)]
    baselines = [s for s in all_signals if s.get("is_baseline", False)]

    # Fetch outcomes for the specified window
    outcomes = await db_manager.query(
        "signal_outcomes",
        filters={"time_window": f"eq.{window}"},
        limit=10000
    )
    outcome_map = {o["signal_id"]: o for o in outcomes} if outcomes else {}

    # Calculate signal metrics
    signal_runners, signal_dead, signal_neutral = 0, 0, 0
    signal_returns = []
    signal_ath_returns = []
    signal_maes = []

    for sig in signals:
        sid = sig["id"]
        outcome = outcome_map.get(sid)
        if not outcome:
            continue
        status = outcome.get("status", "neutral")
        ret = outcome.get("return_pct", 0.0) or 0.0
        ath_ret = outcome.get("ath_return_pct", 0.0) or 0.0
        mae = outcome.get("mae_pct", 0.0) or 0.0
        liq = sig.get("entry_liquidity_usd", 0.0) or 0.0

        if status == "runner":
            signal_runners += 1
        elif status == "dead":
            signal_dead += 1
        else:
            signal_neutral += 1

        signal_returns.append((ret, liq))
        signal_ath_returns.append(ath_ret)
        signal_maes.append(mae)

    # Calculate baseline metrics
    baseline_runners = 0
    baseline_resolved = 0
    for base in baselines:
        bid = base["id"]
        outcome = outcome_map.get(bid)
        if not outcome:
            continue
        baseline_resolved += 1
        if outcome.get("status") == "runner":
            baseline_runners += 1

    resolved_signals = signal_runners + signal_dead + signal_neutral
    signal_hit_rate = signal_runners / resolved_signals if resolved_signals > 0 else 0.0
    baseline_hit_rate = baseline_runners / baseline_resolved if baseline_resolved > 0 else 0.0
    margin = signal_hit_rate - baseline_hit_rate

    # EV calculation with dual cost models
    ev_a = _calculate_ev(signal_returns, _cost_model_a)
    ev_b = _calculate_ev(signal_returns, _cost_model_b)

    # Runner/loss averages
    runner_returns = [r for r, _ in signal_returns if r >= 100.0]
    loss_returns = [r for r, _ in signal_returns if r < 0]
    avg_runner = sum(runner_returns) / len(runner_returns) if runner_returns else 0.0
    avg_loss = sum(loss_returns) / len(loss_returns) if loss_returns else 0.0

    avg_ath = sum(signal_ath_returns) / len(signal_ath_returns) if signal_ath_returns else 0.0
    avg_mae = sum(signal_maes) / len(signal_maes) if signal_maes else 0.0

    # Observation period
    if all_signals:
        dates = [s.get("signal_at", "") for s in all_signals if s.get("signal_at")]
        if dates:
            earliest = min(dates)
            latest = max(dates)
            try:
                d1 = datetime.fromisoformat(str(earliest).replace("Z", "+00:00"))
                d2 = datetime.fromisoformat(str(latest).replace("Z", "+00:00"))
                obs_days = (d2 - d1).total_seconds() / 86400.0
            except Exception:
                obs_days = 0.0
        else:
            obs_days = 0.0
    else:
        obs_days = 0.0

    # DoD checks (PRD Section 10.2)
    meets_dod = (
        resolved_signals >= 100 and
        signal_hit_rate >= 0.20 and
        ev_b > 0 and
        margin >= 0.10 and
        obs_days >= 60
    )

    result = EvaluationResult(
        total_signals=len(signals),
        total_baselines=len(baselines),
        signal_runners=signal_runners,
        signal_dead=signal_dead,
        signal_neutral=signal_neutral,
        baseline_runners=baseline_runners,
        signal_hit_rate=round(signal_hit_rate, 4),
        baseline_hit_rate=round(baseline_hit_rate, 4),
        margin_over_baseline=round(margin, 4),
        ev_per_signal_a=round(ev_a, 4),
        ev_per_signal_b=round(ev_b, 4),
        avg_runner_return=round(avg_runner, 2),
        avg_loss=round(avg_loss, 2),
        avg_ath_return=round(avg_ath, 2),
        avg_mae=round(avg_mae, 2),
        observation_days=round(obs_days, 1),
        meets_dod=meets_dod
    )

    _print_report(result, window)
    return result


def _calculate_ev(returns_with_liq: list[tuple[float, float]], cost_fn) -> float:
    """Calculate Expected Value per signal after applying cost model."""
    if not returns_with_liq:
        return 0.0
    net_returns = []
    for ret_pct, liq in returns_with_liq:
        cost = cost_fn(liq)
        net_returns.append(ret_pct - cost)
    return sum(net_returns) / len(net_returns)


def _print_report(result: EvaluationResult, window: str):
    """Prints formatted evaluation report to console."""
    dod_status = "✅ LULUS" if result.meets_dod else "❌ BELUM TERCAPAI"

    print(f"\n{'='*60}")
    print(f"  FASE 5 — Paper Trading Evaluation Report ({window})")
    print(f"{'='*60}")
    print(f"  Observation Period:       {result.observation_days:.1f} days (target: ≥60)")
    print(f"  Total Signals:            {result.total_signals}")
    print(f"  Total Baselines:          {result.total_baselines}")
    print(f"  Resolved Signals:         {result.signal_runners + result.signal_dead + result.signal_neutral}")
    print(f"{'─'*60}")
    print(f"  🚀 Signal Runners (≥2x):  {result.signal_runners}")
    print(f"  💀 Signal Dead (≤-70%):   {result.signal_dead}")
    print(f"  ➖ Signal Neutral:         {result.signal_neutral}")
    print(f"{'─'*60}")
    print(f"  Hit-Rate (Signal):        {result.signal_hit_rate:.1%} (target: ≥20%)")
    print(f"  Hit-Rate (Baseline):      {result.baseline_hit_rate:.1%}")
    print(f"  Margin over Baseline:     {result.margin_over_baseline:+.1%} (target: ≥+10%)")
    print(f"{'─'*60}")
    print(f"  EV/Signal (Conservative): {result.ev_per_signal_a:+.2f}%")
    print(f"  EV/Signal (PRD 10.3):     {result.ev_per_signal_b:+.2f}% (target: >0%)")
    print(f"{'─'*60}")
    print(f"  Avg Runner Return:        {result.avg_runner_return:+.1f}%")
    print(f"  Avg Loss:                 {result.avg_loss:+.1f}%")
    print(f"  Avg ATH Return (Peak):    {result.avg_ath_return:+.1f}%")
    print(f"  Avg Max Drawdown (MAE):   {result.avg_mae:.1f}%")
    print(f"{'='*60}")
    print(f"  DoD Status:               {dod_status}")
    print(f"{'='*60}\n")


def _empty_result() -> EvaluationResult:
    return EvaluationResult(
        total_signals=0, total_baselines=0,
        signal_runners=0, signal_dead=0, signal_neutral=0,
        baseline_runners=0, signal_hit_rate=0.0, baseline_hit_rate=0.0,
        margin_over_baseline=0.0, ev_per_signal_a=0.0, ev_per_signal_b=0.0,
        avg_runner_return=0.0, avg_loss=0.0, avg_ath_return=0.0, avg_mae=0.0,
        observation_days=0.0, meets_dod=False
    )
