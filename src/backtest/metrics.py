"""
Metrics — Fase 4
Komputasi tiga metrik evaluasi utama:

1. Filter Precision: % token yang lolos safety filter ternyata bukan rug dalam 24 jam
2. Opportunity Recall: % runner yang tertangkap di skor ≥ threshold
3. EV per Trade: rata-rata return bersih (dikurangi cost) untuk token skor ≥ threshold
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BacktestSignal:
    """One token processed through the backtest replay pipeline."""
    token_address: str
    symbol: str
    passed_safety: bool
    opportunity_score: float       # 0–100, or None if rejected at safety
    label: str                     # 'runner' | 'dead' | 'neutral'
    label_return_pct: float        # actual % return at 24h
    liquidity_usd: float
    total_cost_pct: float          # from CostModel
    rejection_reason: Optional[str] = None


@dataclass
class BacktestMetrics:
    """Output of a single backtest run."""
    dataset_size: int
    runner_count: int
    dead_count: int
    neutral_count: int

    # Safety Filter Layer
    total_passed_safety: int
    total_rejected_safety: int
    filter_precision: float             # (passed & not rug) / total passed
    filter_recall_runners: float        # runners that passed safety / total runners

    # Opportunity Layer (at threshold)
    opportunity_threshold: float
    tokens_above_threshold: int
    runners_above_threshold: int
    opportunity_recall: float           # runners above threshold / total runners

    # EV
    ev_per_trade: float                 # mean(return_pct - cost_pct) for skor ≥ threshold
    ev_positive: bool

    # Detail
    all_signals: list[BacktestSignal] = field(default_factory=list)


def compute_metrics(
    signals: list[BacktestSignal],
    opportunity_threshold: float = 60.0  # HYPOTHESIS_INIT
) -> BacktestMetrics:
    """
    Compute all three evaluation metrics from a list of backtest signals.

    Args:
        signals: List of processed token signals
        opportunity_threshold: Score cutoff for opportunity filter evaluation (HYPOTHESIS_INIT)

    Returns:
        BacktestMetrics dataclass
    """
    total = len(signals)
    runners = [s for s in signals if s.label == "runner"]
    dead = [s for s in signals if s.label == "dead"]
    neutral = [s for s in signals if s.label == "neutral"]

    # --- Safety Filter Layer ---
    passed = [s for s in signals if s.passed_safety]
    rejected = [s for s in signals if not s.passed_safety]
    passed_not_rug = [s for s in passed if s.label != "dead"]

    filter_precision = (
        len(passed_not_rug) / len(passed) if passed else 0.0
    )
    filter_recall_runners = (
        len([s for s in runners if s.passed_safety]) / len(runners)
        if runners else 0.0
    )

    # --- Opportunity Layer ---
    above_threshold = [
        s for s in passed
        if s.opportunity_score is not None and s.opportunity_score >= opportunity_threshold
    ]
    runners_above = [s for s in above_threshold if s.label == "runner"]

    opportunity_recall = len(runners_above) / len(runners) if runners else 0.0

    # --- EV per Trade ---
    ev_values = [
        (s.label_return_pct - s.total_cost_pct)
        for s in above_threshold
        if s.label_return_pct is not None and s.total_cost_pct is not None
    ]
    ev_per_trade = (sum(ev_values) / len(ev_values)) if ev_values else 0.0

    return BacktestMetrics(
        dataset_size=total,
        runner_count=len(runners),
        dead_count=len(dead),
        neutral_count=len(neutral),
        total_passed_safety=len(passed),
        total_rejected_safety=len(rejected),
        filter_precision=round(filter_precision, 4),
        filter_recall_runners=round(filter_recall_runners, 4),
        opportunity_threshold=opportunity_threshold,
        tokens_above_threshold=len(above_threshold),
        runners_above_threshold=len(runners_above),
        opportunity_recall=round(opportunity_recall, 4),
        ev_per_trade=round(ev_per_trade, 4),
        ev_positive=ev_per_trade > 0,
        all_signals=signals
    )
