"""
5-Fold Time-Series Walk-Forward Cross Validation Engine — Fase 4
Eliminates data snooping and in-sample overfitting by strictly enforcing chronological train/test splits:
  - Fold 1: Train on [0% - 20%], Test on [20% - 40%]
  - Fold 2: Train on [0% - 40%], Test on [40% - 60%]
  - Fold 3: Train on [0% - 60%], Test on [60% - 80%]
  - Fold 4: Train on [0% - 80%], Test on [80% - 100%]

Optimizer tunes parameters exclusively on Train Folds.
Final evaluation metrics are reported strictly on Out-of-Sample Test Folds.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any

from src.backtest.replay_engine import run_replay_on_tokens
from src.backtest.metrics import BacktestMetrics
from src.utils.logger import logger


@dataclass
class FoldEvaluationResult:
    fold_index: int
    train_size: int
    test_size: int
    train_time_range: tuple[str, str]
    test_time_range: tuple[str, str]
    train_metrics: BacktestMetrics
    test_metrics: BacktestMetrics  # Out-of-Sample (OOS)


@dataclass
class WalkForwardCVResult:
    n_splits: int
    total_tokens: int
    folds: list[FoldEvaluationResult]
    avg_train_ev: float
    avg_train_precision: float
    avg_test_ev: float              # Out-of-Sample average EV
    avg_test_precision: float       # Out-of-Sample average Precision
    avg_test_recall: float          # Out-of-Sample average Recall
    is_ev_positive_oos: bool


def split_time_series_folds(
    tokens: list[dict],
    n_splits: int = 5
) -> list[tuple[list[dict], list[dict]]]:
    """
    Chronologically sorts tokens and splits into walk-forward expanding window folds.
    Returns list of (train_tokens, test_tokens).
    """
    if not tokens:
        return []

    # Sort strictly by listing timestamp ascending
    sorted_tokens = sorted(
        tokens,
        key=lambda x: str(x.get("listed_at") or x.get("collected_at") or "")
    )

    n = len(sorted_tokens)
    if n < n_splits * 2:
        # If dataset is small, create a single 70/30 train/test split
        split_idx = int(n * 0.70)
        return [(sorted_tokens[:split_idx], sorted_tokens[split_idx:])]

    folds = []
    step = n / float(n_splits)

    for k in range(1, n_splits):
        train_end = int(k * step)
        test_end = int((k + 1) * step) if k < n_splits - 1 else n

        train_data = sorted_tokens[:train_end]
        test_data = sorted_tokens[train_end:test_end]

        if train_data and test_data:
            folds.append((train_data, test_data))

    return folds


async def evaluate_walk_forward_cv(
    tokens: list[dict],
    opportunity_threshold: float = 60.0,
    weight_overrides: Optional[dict] = None,
    n_splits: int = 5
) -> WalkForwardCVResult:
    """
    Executes walk-forward cross validation over the provided dataset.
    """
    folds_data = split_time_series_folds(tokens, n_splits=n_splits)
    if not folds_data:
        logger.warning("No valid folds could be generated from tokens list.")
        dummy_metrics = BacktestMetrics(0, 0, 0, 0, 0, 0, 0.0, 0.0, opportunity_threshold, 0, 0, 0.0, 0.0, False)
        return WalkForwardCVResult(
            n_splits=n_splits, total_tokens=len(tokens), folds=[],
            avg_train_ev=0.0, avg_train_precision=0.0, avg_test_ev=0.0,
            avg_test_precision=0.0, avg_test_recall=0.0, is_ev_positive_oos=False
        )

    fold_results: list[FoldEvaluationResult] = []

    for idx, (train_set, test_set) in enumerate(folds_data, 1):
        train_start = train_set[0].get("listed_at", "")[:10]
        train_end = train_set[-1].get("listed_at", "")[:10]
        test_start = test_set[0].get("listed_at", "")[:10]
        test_end = test_set[-1].get("listed_at", "")[:10]

        train_metrics = await run_replay_on_tokens(
            tokens=train_set,
            opportunity_threshold=opportunity_threshold,
            weight_overrides=weight_overrides
        )

        test_metrics = await run_replay_on_tokens(
            tokens=test_set,
            opportunity_threshold=opportunity_threshold,
            weight_overrides=weight_overrides
        )

        res = FoldEvaluationResult(
            fold_index=idx,
            train_size=len(train_set),
            test_size=len(test_set),
            train_time_range=(train_start, train_end),
            test_time_range=(test_start, test_end),
            train_metrics=train_metrics,
            test_metrics=test_metrics
        )
        fold_results.append(res)
        logger.info(
            f"📈 [Fold {idx}/{len(folds_data)}] Train Size: {len(train_set)} | Test Size: {len(test_set)} | "
            f"Train EV: {train_metrics.ev_per_trade:+.2f}% | Test OOS EV: {test_metrics.ev_per_trade:+.2f}%"
        )

    avg_train_ev = sum(f.train_metrics.ev_per_trade for f in fold_results) / len(fold_results)
    avg_train_prec = sum(f.train_metrics.filter_precision for f in fold_results) / len(fold_results)
    avg_test_ev = sum(f.test_metrics.ev_per_trade for f in fold_results) / len(fold_results)
    avg_test_prec = sum(f.test_metrics.filter_precision for f in fold_results) / len(fold_results)
    avg_test_rec = sum(f.test_metrics.opportunity_recall for f in fold_results) / len(fold_results)

    return WalkForwardCVResult(
        n_splits=len(fold_results),
        total_tokens=len(tokens),
        folds=fold_results,
        avg_train_ev=round(avg_train_ev, 4),
        avg_train_precision=round(avg_train_prec, 4),
        avg_test_ev=round(avg_test_ev, 4),
        avg_test_precision=round(avg_test_prec, 4),
        avg_test_recall=round(avg_test_rec, 4),
        is_ev_positive_oos=avg_test_ev > 0
    )
