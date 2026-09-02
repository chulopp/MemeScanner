"""
Metrics — Fase 4, B & Exit
Komputasi tiga metrik evaluasi utama:

1. Filter Precision: % token yang lolos safety filter ternyata bukan rug dalam 24 jam
2. Opportunity Recall: % runner yang tertangkap di skor ≥ threshold
3. EV per Trade: rata-rata return bersih (dikurangi cost) untuk token skor ≥ threshold

Fase B (R9/R14):
- label_return_pct_t2: return dihitung dari harga T+2 entry (gate metric)
- t0_fallback: True kalau harga T+2 tidak tersedia, fallback ke T=0
- ev_per_trade_t2: GATE METRIC — hanya dari row yang punya data T+2 asli
- ev_per_trade_t0_fallback: log-only — dari row yang pakai T=0 fallback
- t2_coverage_pct: transparansi berapa % signal yang beneran punya data T+2

Fase Exit (Fase 1):
- realized_return_pct: EV dihitung dari exit engine (multi-tier TP + moonbag trailing + SL)
- ev_per_trade_with_exit: gate metric baru, lebih realistis dari flat 24h return
- tp_hit_rates: dict {tier_label: hit_rate_pct} berapa sering setiap TP tier tercapai
- avg_moonbag_return_pct: rata-rata return moonbag portion per trade
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
    label_return_pct: float        # actual % return at 24h (from T=0 or T+2 fallback)
    liquidity_usd: float
    total_cost_pct: float          # from CostModel
    rejection_reason: Optional[str] = None
    # Fase B (R9/R14): T+2 EV separation
    label_return_pct_t2: Optional[float] = None  # Return from T+2 entry price (None if unavailable)
    t0_fallback: bool = False                     # True if T+2 price unavailable, using T=0 instead

    # Fase Exit (Fase 1): Exit strategy simulation result
    realized_return_pct: Optional[float] = None   # Weighted return from tiered TP + moonbag + SL
    tp_tiers_hit: int = 0                         # How many TP tiers fired
    moonbag_return_pct: Optional[float] = None    # Return on moonbag portion
    moonbag_exit_reason: str = "N/A"              # 'TRAILING_STOP'|'TIMEOUT_24H'|'SL'|'N/A'


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

    # EV (combined: T+2 where available, T=0 fallback otherwise)
    ev_per_trade: float                 # mean(return_pct - cost_pct) for skor ≥ threshold
    ev_positive: bool

    # Fase B (R14): Separated EV metrics
    ev_per_trade_t2: float = 0.0           # GATE METRIC: EV from rows with real T+2 data only
    ev_per_trade_t0_fallback: float = 0.0  # LOG-ONLY: EV from T=0 fallback rows (not gate)
    t2_coverage_pct: float = 0.0           # % of above-threshold signals with real T+2 data

    # Fase Exit (Fase 1): Exit-aware EV
    ev_per_trade_with_exit: float = 0.0    # NEW GATE METRIC: EV from exit engine simulation
    tp_hit_rates: dict = field(default_factory=dict)  # {"TP1": pct, "TP2": pct, "TP3": pct}
    avg_moonbag_return_pct: float = 0.0    # Average moonbag return across above-threshold trades
    exit_coverage_pct: float = 0.0         # % above-threshold signals that have exit simulation data

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

    # --- Fase B (R14): Separated T+2 vs T=0 fallback EV ---
    # Split above-threshold signals by T+2 data availability
    t2_signals = [
        s for s in above_threshold
        if not s.t0_fallback and s.label_return_pct_t2 is not None
    ]
    t0_signals = [s for s in above_threshold if s.t0_fallback]

    # GATE METRIC: EV from real T+2 data only (R14)
    ev_t2_values = [
        (s.label_return_pct_t2 - s.total_cost_pct)
        for s in t2_signals
        if s.label_return_pct_t2 is not None and s.total_cost_pct is not None
    ]
    ev_per_trade_t2 = (sum(ev_t2_values) / len(ev_t2_values)) if ev_t2_values else 0.0

    # LOG-ONLY METRIC: EV from T=0 fallback rows (for comparison, NOT gate)
    ev_t0_values = [
        (s.label_return_pct - s.total_cost_pct)
        for s in t0_signals
        if s.label_return_pct is not None and s.total_cost_pct is not None
    ]
    ev_per_trade_t0 = (sum(ev_t0_values) / len(ev_t0_values)) if ev_t0_values else 0.0

    # Combined EV (backward compat): T+2 where available, T=0 fallback otherwise
    ev_values = [
        (
            (s.label_return_pct_t2 if not s.t0_fallback and s.label_return_pct_t2 is not None
             else s.label_return_pct)
            - s.total_cost_pct
        )
        for s in above_threshold
        if s.total_cost_pct is not None
    ]
    ev_per_trade = (sum(ev_values) / len(ev_values)) if ev_values else 0.0

    # T+2 coverage transparency
    t2_coverage = len(t2_signals) / len(above_threshold) if above_threshold else 0.0

    # --- Fase Exit: Exit-aware EV and TP hit rates ---
    exit_signals = [
        s for s in above_threshold
        if s.realized_return_pct is not None
    ]
    exit_coverage = len(exit_signals) / len(above_threshold) if above_threshold else 0.0

    ev_with_exit_values = [s.realized_return_pct for s in exit_signals if s.realized_return_pct is not None]
    ev_per_trade_with_exit = (sum(ev_with_exit_values) / len(ev_with_exit_values)) if ev_with_exit_values else 0.0

    moonbag_returns = [s.moonbag_return_pct for s in exit_signals if s.moonbag_return_pct is not None]
    avg_moonbag_return = (sum(moonbag_returns) / len(moonbag_returns)) if moonbag_returns else 0.0

    # TP hit rates: how often each tier was reached
    tp_hit_rates: dict = {}
    for tier_name in ("TP1", "TP2", "TP3"):
        tier_idx = int(tier_name[2]) - 1  # 0-indexed
        tier_hits = sum(1 for s in exit_signals if s.tp_tiers_hit > tier_idx)
        tp_hit_rates[tier_name] = round(
            (tier_hits / len(exit_signals) * 100.0) if exit_signals else 0.0, 1
        )

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
        ev_per_trade_t2=round(ev_per_trade_t2, 4),
        ev_per_trade_t0_fallback=round(ev_per_trade_t0, 4),
        t2_coverage_pct=round(t2_coverage, 4),
        ev_per_trade_with_exit=round(ev_per_trade_with_exit, 4),
        tp_hit_rates=tp_hit_rates,
        avg_moonbag_return_pct=round(avg_moonbag_return, 4),
        exit_coverage_pct=round(exit_coverage, 4),
        all_signals=signals
    )
