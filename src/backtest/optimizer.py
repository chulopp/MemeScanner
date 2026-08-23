"""
Bayesian Optimizer — Fase 4
Menggunakan scikit-optimize (skopt) untuk mencari parameter optimal
yang memaksimalkan: EV_per_trade + 0.5 * filter_precision.

Search Space (semua parameter tagged HYPOTHESIS_INIT):
  - weight_vol_velocity : 0.30 – 0.45
  - weight_smart_money  : 0.25 – 0.40
  - weight_global_fee   : 0.10 – 0.20
  - opportunity_threshold: 50.0 – 75.0
  - liquidity_floor_usd : 500 – 5000 (offline safety check)

Metode: Gaussian Process (default skopt), 50 evaluasi.
"""

import asyncio
import json
from datetime import datetime, timezone
from typing import Optional

from src.database.client import db_manager
from src.backtest.replay_engine import run_replay
from src.utils.logger import logger

# Guard import — scikit-optimize is optional (needed only for optimization)
try:
    from skopt import gp_minimize
    from skopt.space import Real
    from skopt.utils import use_named_args
    SKOPT_AVAILABLE = True
except ImportError:
    SKOPT_AVAILABLE = False


# --- Search Space — HYPOTHESIS_INIT bounds ---
SEARCH_SPACE = [
    Real(0.30, 0.45, name="weight_vol_velocity"),   # HYPOTHESIS_INIT: 0.35
    Real(0.25, 0.40, name="weight_smart_money"),     # HYPOTHESIS_INIT: 0.30
    Real(0.10, 0.20, name="weight_global_fee"),      # HYPOTHESIS_INIT: 0.15
    Real(50.0, 75.0, name="opportunity_threshold"),  # HYPOTHESIS_INIT: 60.0
]


async def _evaluate_params(
    weight_vol_velocity: float,
    weight_smart_money: float,
    weight_global_fee: float,
    opportunity_threshold: float,
    limit: int = 500
) -> float:
    """
    Objective function for one Bayesian evaluation.
    Returns negative objective value (skopt minimizes, so we negate).
    Objective = EV_per_trade + 0.5 * filter_precision
    """
    weight_overrides = {
        "vol_velocity": weight_vol_velocity,
        "smart_money": weight_smart_money,
        "global_fee": weight_global_fee
    }

    metrics = await run_replay(
        opportunity_threshold=opportunity_threshold,
        weight_overrides=weight_overrides,
        limit=limit
    )

    # Objective: maximize EV per trade + weight filter precision
    # Negate because skopt minimizes
    objective = metrics.ev_per_trade + 0.5 * (metrics.filter_precision * 100.0)
    return -objective, metrics


async def run_bayesian_optimization(
    n_calls: int = 50,
    limit: int = 500,
    notes: str = ""
) -> dict:
    """
    Run Bayesian Optimization over the parameter search space.
    Saves each evaluation result to Supabase `backtest_runs`.

    Args:
        n_calls: Number of Bayesian optimizer evaluations (HYPOTHESIS_INIT: 50)
        limit: Max tokens per replay call
        notes: Optional annotation for this optimization run

    Returns:
        dict with best_params and best_metrics
    """
    if not SKOPT_AVAILABLE:
        logger.error(
            "❌ scikit-optimize not installed. Run: pip install scikit-optimize"
        )
        return {"error": "scikit-optimize not installed"}

    logger.info(f"🔬 Starting Bayesian Optimization ({n_calls} evaluations)...")

    best_result: Optional[dict] = None
    best_objective = float("-inf")
    all_results: list[dict] = []
    eval_count = 0

    # We need a sync wrapper for skopt
    # Run the async evaluation synchronously within each call
    _loop = asyncio.get_event_loop()

    def objective_fn(params):
        nonlocal eval_count, best_result, best_objective

        w_vol, w_sm, w_fee, threshold = params
        eval_count += 1

        logger.info(
            f"📐 Eval {eval_count}/{n_calls}: vol={w_vol:.3f} sm={w_sm:.3f} "
            f"fee={w_fee:.3f} threshold={threshold:.1f}"
        )

        neg_obj, metrics = _loop.run_until_complete(
            _evaluate_params(w_vol, w_sm, w_fee, threshold, limit)
        )
        obj = -neg_obj

        result = {
            "eval": eval_count,
            "params": {
                "weight_vol_velocity": round(w_vol, 4),
                "weight_smart_money": round(w_sm, 4),
                "weight_global_fee": round(w_fee, 4),
                "opportunity_threshold": round(threshold, 2)
            },
            "filter_precision": metrics.filter_precision,
            "opportunity_recall": metrics.opportunity_recall,
            "ev_per_trade": metrics.ev_per_trade,
            "objective": round(obj, 4),
            "dataset_size": metrics.dataset_size,
            "runner_count": metrics.runner_count,
            "dead_count": metrics.dead_count,
            "neutral_count": metrics.neutral_count,
        }
        all_results.append(result)

        if obj > best_objective:
            best_objective = obj
            best_result = result
            logger.info(
                f"✨ New best at eval {eval_count}: EV={metrics.ev_per_trade:+.2f}% | "
                f"Precision={metrics.filter_precision:.1%} | Objective={obj:.2f}"
            )

        # Persist to Supabase
        asyncio.get_event_loop().run_until_complete(
            _save_run_to_supabase(metrics, result["params"], is_optimal=False, notes=notes)
        )

        return neg_obj

    # Run Gaussian Process optimization
    gp_result = gp_minimize(
        objective_fn,
        SEARCH_SPACE,
        n_calls=n_calls,
        n_initial_points=10,  # random exploration first
        acq_func="EI",        # Expected Improvement
        random_state=42
    )

    # Mark best run as optimal in Supabase
    if best_result:
        await _save_run_to_supabase(
            metrics=None,
            params=best_result["params"],
            is_optimal=True,
            filter_precision=best_result["filter_precision"],
            opportunity_recall=best_result["opportunity_recall"],
            ev_per_trade=best_result["ev_per_trade"],
            dataset_size=best_result["dataset_size"],
            runner_count=best_result["runner_count"],
            dead_count=best_result["dead_count"],
            neutral_count=best_result["neutral_count"],
            notes=f"OPTIMAL RUN — {notes}"
        )

    logger.info(f"✅ Bayesian optimization complete. Best objective: {best_objective:.2f}")
    logger.info(f"Best params: {best_result['params'] if best_result else 'N/A'}")

    return {
        "best_params": best_result["params"] if best_result else {},
        "best_ev_per_trade": best_result["ev_per_trade"] if best_result else 0.0,
        "best_filter_precision": best_result["filter_precision"] if best_result else 0.0,
        "total_evaluations": eval_count,
        "all_results": all_results
    }


async def _save_run_to_supabase(
    metrics,
    params: dict,
    is_optimal: bool = False,
    filter_precision: float = 0.0,
    opportunity_recall: float = 0.0,
    ev_per_trade: float = 0.0,
    dataset_size: int = 0,
    runner_count: int = 0,
    dead_count: int = 0,
    neutral_count: int = 0,
    notes: str = ""
) -> None:
    """Persist a backtest run result to Supabase backtest_runs."""
    try:
        if metrics is not None:
            row = {
                "dataset_size": metrics.dataset_size,
                "runner_count": metrics.runner_count,
                "dead_count": metrics.dead_count,
                "neutral_count": metrics.neutral_count,
                "params": params,
                "filter_precision": metrics.filter_precision,
                "opportunity_recall": metrics.opportunity_recall,
                "ev_per_trade": metrics.ev_per_trade,
                "is_optimal": is_optimal,
                "notes": notes
            }
        else:
            row = {
                "dataset_size": dataset_size,
                "runner_count": runner_count,
                "dead_count": dead_count,
                "neutral_count": neutral_count,
                "params": params,
                "filter_precision": filter_precision,
                "opportunity_recall": opportunity_recall,
                "ev_per_trade": ev_per_trade,
                "is_optimal": is_optimal,
                "notes": notes
            }
        await db_manager.insert("backtest_runs", row)
    except Exception as e:
        logger.debug(f"Failed to save run to Supabase: {e}")
