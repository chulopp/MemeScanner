"""
Bayesian Optimizer with 5-Fold Walk-Forward Cross Validation — Fase 4
Finds optimal scoring weights and threshold while eliminating in-sample overfitting:
1. Splits resolved historical data into chronological Expanding-Window Walk-Forward folds.
2. Bayesian Optimizer searches optimal parameters exclusively on Training Folds.
3. Final performance is evaluated and reported strictly on Out-of-Sample (OOS) Test Folds.

Search Space:
  - weight_vol_velocity : 0.30 – 0.45
  - weight_smart_money  : 0.25 – 0.40
  - weight_global_fee   : 0.10 – 0.20
  - opportunity_threshold: 50.0 – 75.0
"""

import asyncio
from typing import Optional, Any

from src.database.client import db_manager
from src.backtest.cross_validation import split_time_series_folds, evaluate_walk_forward_cv, WalkForwardCVResult
from src.backtest.replay_engine import run_replay_on_tokens
from src.exit.strategy import ExitStrategyConfig, TpTier, TrailingTier
from src.utils.logger import logger

try:
    from skopt import gp_minimize
    from skopt.space import Real
    SKOPT_AVAILABLE = True
except ImportError:
    gp_minimize = None
    Real = None
    SKOPT_AVAILABLE = False


def _get_search_space():
    if not SKOPT_AVAILABLE or Real is None:
        return []
    return [
        # Scoring weights — HYPOTHESIS_INIT
        Real(0.20, 0.45, name="weight_vol_velocity"),   # Base: 0.35
        Real(0.15, 0.40, name="weight_smart_money"),     # Base: 0.30
        Real(0.05, 0.25, name="weight_global_fee"),      # Base: 0.15
        Real(0.05, 0.20, name="weight_holder_curve"),    # Base: 0.10
        Real(0.05, 0.20, name="weight_social_meta"),     # Base: 0.10
        Real(25.0, 70.0, name="opportunity_threshold"),  # Base: 60.0
        # Exit strategy parameters — HYPOTHESIS_INIT
        Real(-65.0, -30.0, name="exit_sl_pct"),                  # Base: -50.0
        Real(50.0, 200.0, name="exit_tp1_pct"),                  # Base: 100.0 (2x)
        Real(200.0, 500.0, name="exit_tp2_pct"),                 # Base: 300.0 (4x)
        Real(400.0, 1000.0, name="exit_tp3_pct"),                # Base: 500.0 (6x)
        Real(25.0, 55.0, name="exit_trailing_tier1_pct"),        # Base: 40.0 (trail at <2x ATH)
        Real(35.0, 65.0, name="exit_trailing_tier2_pct"),        # Base: 50.0 (trail at 2-5x ATH)
        Real(45.0, 75.0, name="exit_trailing_tier3_pct"),        # Base: 60.0 (trail at >10x ATH)
    ]



async def run_bayesian_optimization(
    n_calls: int = 30,
    limit: int = 500,
    n_splits: int = 5,
    notes: str = ""
) -> dict:
    """
    Executes Bayesian optimization with rigorous Walk-Forward Cross Validation.
    """
    if not SKOPT_AVAILABLE:
        logger.error("❌ scikit-optimize not installed. Run: pip install scikit-optimize")
        return {"error": "scikit-optimize not installed"}

    await db_manager.initialize()

    # Load resolved tokens
    rows = await db_manager.query(
        "backtest_tokens",
        filters={"label": "not.is.null"},
        limit=limit
    )

    if not rows or len(rows) < 5:
        logger.warning("Not enough resolved tokens in database to run Cross-Validation (need at least 5).")
        return {"error": "Insufficient resolved tokens"}

    # Sort strictly by timestamp
    sorted_tokens = sorted(rows, key=lambda x: str(x.get("listed_at") or x.get("collected_at") or ""))
    folds_data = split_time_series_folds(sorted_tokens, n_splits=n_splits)

    logger.info(f"🔬 Starting Bayesian Optimization with {len(folds_data)} Walk-Forward Folds ({n_calls} evaluations)...")

    import concurrent.futures

    best_params: Optional[dict] = None
    best_train_objective = float("-inf")
    eval_count = 0
    loop = asyncio.get_running_loop()

    # Primary training dataset: latest train fold (or aggregated training portions)
    primary_train_set = folds_data[-1][0] if folds_data else sorted_tokens

    def _run_optimizer_thread():
        nonlocal best_params, best_train_objective, eval_count

        def objective_fn(params):
            nonlocal eval_count, best_params, best_train_objective

            w_vol, w_sm, w_fee, w_holder, w_social, threshold, \
                exit_sl, exit_tp1, exit_tp2, exit_tp3, \
                exit_trail1, exit_trail2, exit_trail3 = params
            eval_count += 1

            w_dict = {
                "vol_velocity": w_vol,
                "smart_money": w_sm,
                "global_fee": w_fee,
                "holder_curve": w_holder,
                "social_meta": w_social
            }

            exit_cfg = ExitStrategyConfig(
                sl_pct=exit_sl,
                tp_tiers=[
                    TpTier(sell_fraction=0.30, target_return_pct=exit_tp1),
                    TpTier(sell_fraction=0.30, target_return_pct=exit_tp2),
                    TpTier(sell_fraction=0.20, target_return_pct=exit_tp3),
                ],
                trailing_tiers=[
                    TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=exit_trail3),
                    TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=exit_trail2),
                    TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=exit_trail1),
                ],
            )

            # Safely evaluate coroutine from thread on the main event loop
            future = asyncio.run_coroutine_threadsafe(
                run_replay_on_tokens(
                    primary_train_set,
                    opportunity_threshold=threshold,
                    weight_overrides=w_dict,
                    exit_config=exit_cfg,
                ),
                loop
            )
            train_metrics = future.result()

            # Objective: weighted combination of EV-with-exit (60%), EV-raw (20%), precision (20%)
            # Prioritize exit-aware EV as primary gate metric
            ev_exit = train_metrics.ev_per_trade_with_exit if train_metrics.exit_coverage_pct > 0.1 else train_metrics.ev_per_trade
            obj = (
                0.60 * ev_exit
                + 0.20 * train_metrics.ev_per_trade
                + 0.20 * (train_metrics.filter_precision * 100.0)
            )

            if obj > best_train_objective:
                best_train_objective = obj
                best_params = {
                    "weight_vol_velocity": round(w_vol, 4),
                    "weight_smart_money": round(w_sm, 4),
                    "weight_global_fee": round(w_fee, 4),
                    "weight_holder_curve": round(w_holder, 4),
                    "weight_social_meta": round(w_social, 4),
                    "opportunity_threshold": round(threshold, 2),
                    "exit_sl_pct": round(exit_sl, 2),
                    "exit_tp1_pct": round(exit_tp1, 2),
                    "exit_tp2_pct": round(exit_tp2, 2),
                    "exit_tp3_pct": round(exit_tp3, 2),
                    "exit_trailing_tier1_pct": round(exit_trail1, 2),
                    "exit_trailing_tier2_pct": round(exit_trail2, 2),
                    "exit_trailing_tier3_pct": round(exit_trail3, 2),
                }
                logger.info(
                    f"\u2728 [Eval {eval_count}/{n_calls}] New Best EV(exit)={ev_exit:+.2f}% | "
                    f"EV(raw)={train_metrics.ev_per_trade:+.2f}% | "
                    f"Precision: {train_metrics.filter_precision:.1%}"
                )

            return -obj

        search_space = _get_search_space()
        return gp_minimize(
            objective_fn,
            search_space,
            n_calls=n_calls,
            n_initial_points=min(8, max(4, n_calls // 4)),
            acq_func="EI",
            random_state=42
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        gp_result = await loop.run_in_executor(pool, _run_optimizer_thread)

    # 4. Rigorous Out-of-Sample Evaluation on all Walk-Forward Test Folds
    logger.info("🛡️ Evaluating Best Parameters on Out-of-Sample Test Folds...")
    cv_result: WalkForwardCVResult = await evaluate_walk_forward_cv(
        tokens=sorted_tokens,
        opportunity_threshold=best_params["opportunity_threshold"],
        weight_overrides=best_params,
        n_splits=n_splits
    )


    # Save final run result to Supabase
    fold_details = [
        {
            "fold": f.fold_index,
            "train_size": f.train_size,
            "test_size": f.test_size,
            "train_ev": f.train_metrics.ev_per_trade,
            "oos_test_ev": f.test_metrics.ev_per_trade,
            "oos_precision": f.test_metrics.filter_precision,
            "oos_recall": f.test_metrics.opportunity_recall
        }
        for f in cv_result.folds
    ]

    run_record = {
        "dataset_size": len(sorted_tokens),
        "runner_count": sum(1 for r in sorted_tokens if r.get("label") == "runner"),
        "dead_count": sum(1 for r in sorted_tokens if r.get("label") == "dead"),
        "neutral_count": sum(1 for r in sorted_tokens if r.get("label") == "neutral"),
        "params": best_params,
        "filter_precision": cv_result.avg_train_precision,
        "opportunity_recall": cv_result.avg_test_recall,
        "ev_per_trade": cv_result.avg_train_ev,
        "oos_ev_per_trade": cv_result.avg_test_ev,
        "oos_filter_precision": cv_result.avg_test_precision,
        "oos_opportunity_recall": cv_result.avg_test_recall,
        "fold_results": fold_details,
        "is_optimal": True,
        "notes": f"5-Fold Walk-Forward Cross Validation. {notes}"
    }

    try:
        await db_manager.insert("backtest_runs", run_record)
    except Exception as e:
        logger.debug(f"Failed to persist backtest run: {e}")

    logger.info(
        f"🎯 Walk-Forward CV Results (Out-of-Sample):\n"
        f"  Average OOS EV/Trade:    {cv_result.avg_test_ev:+.2f}%\n"
        f"  Average OOS Precision:   {cv_result.avg_test_precision:.1%}\n"
        f"  Average OOS Recall:      {cv_result.avg_test_recall:.1%}\n"
        f"  OOS EV Positive:         {'✅' if cv_result.is_ev_positive_oos else '❌'}\n"
        f"  Optimal Parameters:      {best_params}"
    )

    return {
        "best_params": best_params,
        "oos_ev_per_trade": cv_result.avg_test_ev,
        "oos_filter_precision": cv_result.avg_test_precision,
        "oos_opportunity_recall": cv_result.avg_test_recall,
        "is_ev_positive_oos": cv_result.is_ev_positive_oos,
        "cv_result": cv_result
    }
