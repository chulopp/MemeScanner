"""
Phase verification gate (REVISED v3 - R1, R2, R11, R15 decisions).
Run: python scripts/verify_phase.py --phase A --threshold 31.0

Gate thresholds (all from walk-forward CV OOS -- not run_replay all-inclusive):
  - OOS Recall >= 50%  (R15: computed only from folds that have >=1 runner in test set)
  - OOS Precision >= 50%
  - OOS EV per Trade > 0
  - Dataset size >= runner_count * 25  (R11: dynamic)
  - At least 1 fold has runners in test set  (R15)
"""
import argparse
import asyncio
import sys
import os

# Allow running from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.database.client import db_manager
from src.backtest.cross_validation import evaluate_walk_forward_cv
from src.backtest.replay_engine import run_replay

RUNNER_MULTIPLIER = 25   # R11: minimum tokens = runner_count * this
N_SPLITS = 5             # Walk-forward CV folds


async def main():
    parser = argparse.ArgumentParser(description="MemeScanner Phase Verification Gate")
    parser.add_argument("--phase", type=str, default="A", help="Phase name (default: A)")
    parser.add_argument("--threshold", type=float, default=31.0, help="Opportunity score threshold (default: 31.0)")
    args, unknown = parser.parse_known_args()

    # Fallback to positional if someone passes erify_phase.py A
    phase = args.phase
    if unknown and not phase:
        phase = unknown[0]

    threshold = args.threshold
    # If positional args passed e.g. python scripts/verify_phase.py A 31.0
    for u in unknown:
        try:
            val = float(u)
            threshold = val
        except ValueError:
            if u.isalpha():
                phase = u

    print(f"\n{'='*60}")
    print(f"  MemeScanner Phase {phase} Verification Gate (v3)")
    print(f"  Calibrated Opportunity Threshold: {threshold:.1f}")
    print(f"{'='*60}\n")

    await db_manager.initialize()

    rows = await db_manager.query(
        "backtest_tokens",
        filters={"label": "not.is.null"},
        limit=5000
    )

    total_tokens = len(rows)
    runner_count = sum(1 for r in rows if r.get("label") == "runner")
    dead_count = sum(1 for r in rows if r.get("label") == "dead")
    neutral_count = sum(1 for r in rows if r.get("label") == "neutral")
    min_dataset_size = runner_count * RUNNER_MULTIPLIER

    print(f"Dataset loaded: {total_tokens} tokens total")
    print(f"   Runners:  {runner_count}")
    print(f"   Dead:     {dead_count}")
    print(f"   Neutral:  {neutral_count}")
    print(f"   MIN_DATASET_SIZE = {runner_count} x {RUNNER_MULTIPLIER} = {min_dataset_size}\n")

    if runner_count == 0:
        print("GATE FAIL: No runners in dataset -- cannot compute recall.")
        sys.exit(1)

    if total_tokens < min_dataset_size:
        print(f"GATE FAIL: Dataset too small: {total_tokens} < {min_dataset_size}")
        sys.exit(1)

    # SECONDARY reference: Full replay (log-only)
    print(f"Running full replay (reference, log-only at threshold={threshold:.1f})...")
    try:
        replay_metrics = await run_replay(opportunity_threshold=threshold, limit=5000)
        print(f"   Reference: Recall={replay_metrics.opportunity_recall:.1%}, "
              f"Precision={replay_metrics.filter_precision:.1%}, "
              f"EV={replay_metrics.ev_per_trade:+.2f}%\n")
    except Exception as e:
        print(f"   Reference replay failed (non-fatal): {e}\n")

    # PRIMARY GATE: Walk-forward CV OOS
    print(f"Running Walk-Forward CV ({N_SPLITS}-fold OOS at threshold={threshold:.1f})...")
    cv_result = await evaluate_walk_forward_cv(
        tokens=rows,
        opportunity_threshold=threshold,
        n_splits=N_SPLITS
    )

    print(f"\n   {'Fold':<6} {'Train':<8} {'Test':<8} {'Runners':<10} {'Status':<22} {'OOS Recall':<12} OOS EV")
    print(f"   {'-'*78}")

    folds_with_runners = []
    folds_without_runners = []

    for fold in cv_result.folds:
        test_runners = 0
        if hasattr(fold.test_metrics, "all_signals") and fold.test_metrics.all_signals:
            test_runners = sum(1 for s in fold.test_metrics.all_signals if s.label == "runner")

        if test_runners > 0:
            folds_with_runners.append((fold, test_runners))
            status = "ACTIVE"
        else:
            folds_without_runners.append(fold)
            status = "EXCLUDED(0 runners)"

        print(
            f"   {fold.fold_index:<6} "
            f"{fold.train_size:<8} "
            f"{fold.test_size:<8} "
            f"{test_runners:<10} "
            f"{status:<22} "
            f"{fold.test_metrics.opportunity_recall:.1%}{'':6}"
            f"{fold.test_metrics.ev_per_trade:+.2f}%"
        )

    print(f"   {'-'*78}")

    if folds_without_runners:
        print(f"\n   R15: {len(folds_without_runners)} fold(s) excluded (no runners in test set).")
        print(f"        Recall average uses {len(folds_with_runners)} valid fold(s) only.")

    if folds_with_runners:
        adjusted_avg_recall = sum(
            f.test_metrics.opportunity_recall for f, _ in folds_with_runners
        ) / len(folds_with_runners)
    else:
        adjusted_avg_recall = 0.0

    print(f"\n   Avg OOS Recall (R15 adjusted): {adjusted_avg_recall:.1%}  [{len(folds_with_runners)} active folds]")
    print(f"   Avg OOS Precision:             {cv_result.avg_test_precision:.1%}")
    print(f"   Avg OOS EV per Trade:          {cv_result.avg_test_ev:+.2f}%")

    gate_checks = {
        f"Dataset >= {min_dataset_size} (runner x {RUNNER_MULTIPLIER})": total_tokens >= min_dataset_size,
        "At least 1 fold has runners in test (R15)": len(folds_with_runners) >= 1,
        f"OOS Recall >= 50% ({len(folds_with_runners)} active folds, R15)": adjusted_avg_recall >= 0.50,
        "OOS Precision >= 50%": cv_result.avg_test_precision >= 0.50,
        "OOS EV > 0": cv_result.avg_test_ev > 0,
        "OOS EV Positive flag": cv_result.is_ev_positive_oos,
    }

    print(f"\n{'='*60}")
    print(f"  Gate Results -- Phase {phase} (Threshold {threshold:.1f})")
    print(f"{'='*60}")
    all_pass = True
    for name, passed in gate_checks.items():
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}]  {name}")
        if not passed:
            all_pass = False

    print(f"\n{'='*60}")
    if all_pass:
        print(f"  PHASE {phase} GATE PASSED -- safe to proceed")
    else:
        print(f"  PHASE {phase} GATE FAILED -- do NOT proceed")
    print(f"{'='*60}\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    asyncio.run(main())
