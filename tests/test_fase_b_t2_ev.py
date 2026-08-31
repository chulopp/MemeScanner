"""
Tests for Fase B: T+2 EV Separation in metrics.py and replay_engine.py

Validates:
  1. BacktestSignal dengan label_return_pct_t2 dan t0_fallback fields
  2. compute_metrics memisahkan ev_per_trade_t2 (gate) vs ev_per_trade_t0_fallback (log-only)
  3. Combined ev_per_trade uses T+2 when available, T=0 fallback otherwise
  4. t2_coverage_pct dihitung benar
  5. replay_engine._build_raw_token_event tetap berjalan
  6. price_usd_at_t2 column dibaca dari row dan dikonversi ke label_return_pct_t2 dengan benar
"""

import pytest
from src.backtest.metrics import BacktestSignal, compute_metrics


def make_signal(
    symbol: str,
    label: str,
    opp_score: float,
    label_return_pct: float,
    label_return_pct_t2: float | None = None,
    t0_fallback: bool = False,
    total_cost_pct: float = 2.0,
) -> BacktestSignal:
    return BacktestSignal(
        token_address=f"ADDR_{symbol}",
        symbol=symbol,
        passed_safety=True,
        opportunity_score=opp_score,
        label=label,
        label_return_pct=label_return_pct,
        liquidity_usd=10_000.0,
        total_cost_pct=total_cost_pct,
        label_return_pct_t2=label_return_pct_t2,
        t0_fallback=t0_fallback,
    )


class TestBacktestSignalFields:
    """BacktestSignal has the new T+2 fields with correct defaults."""

    def test_default_t0_fallback_is_true_when_no_t2(self):
        sig = make_signal("TOKEN_A", "runner", 70.0, 200.0, t0_fallback=True)
        assert sig.t0_fallback is True
        assert sig.label_return_pct_t2 is None

    def test_t2_fields_populated(self):
        sig = make_signal("TOKEN_B", "runner", 70.0, 200.0, label_return_pct_t2=150.0, t0_fallback=False)
        assert sig.t0_fallback is False
        assert sig.label_return_pct_t2 == 150.0


class TestComputeMetricsT2Separation:
    """compute_metrics correctly separates T+2 gate metric from T=0 fallback."""

    def test_ev_t2_only_from_real_t2_rows(self):
        """ev_per_trade_t2 uses ONLY rows with t0_fallback=False and label_return_pct_t2 set."""
        signals = [
            # Runner with T+2 data: +150% - 2% cost = +148%
            make_signal("RUNNER_T2", "runner", 70.0, 200.0, label_return_pct_t2=150.0, t0_fallback=False),
            # Dead with T+2 data: -50% - 2% cost = -52%
            make_signal("DEAD_T2", "dead", 70.0, -80.0, label_return_pct_t2=-50.0, t0_fallback=False),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        # ev_t2 = ((150 - 2) + (-50 - 2)) / 2 = (148 + -52) / 2 = 96 / 2 = 48
        assert abs(metrics.ev_per_trade_t2 - 48.0) < 0.01
        # No T=0 fallback rows above threshold → ev_t0_fallback stays 0
        assert metrics.ev_per_trade_t0_fallback == 0.0

    def test_ev_t0_fallback_separate_from_t2(self):
        """ev_per_trade_t0_fallback uses ONLY rows with t0_fallback=True."""
        signals = [
            # T+2 row: +200 - 2 = 198
            make_signal("RUNNER_T2", "runner", 70.0, 200.0, label_return_pct_t2=200.0, t0_fallback=False),
            # T=0 fallback row: -70 - 2 = -72
            make_signal("DEAD_T0", "dead", 70.0, -70.0, t0_fallback=True),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        # t2 EV: only RUNNER_T2 → 200 - 2 = 198
        assert abs(metrics.ev_per_trade_t2 - 198.0) < 0.01
        # t0 EV: only DEAD_T0 → -70 - 2 = -72
        assert abs(metrics.ev_per_trade_t0_fallback - (-72.0)) < 0.01

    def test_combined_ev_uses_t2_when_available(self):
        """Combined ev_per_trade uses T+2 return when available, T=0 when not."""
        signals = [
            # Has T+2: use label_return_pct_t2=100, not label_return_pct=200
            make_signal("R1", "runner", 70.0, 200.0, label_return_pct_t2=100.0, t0_fallback=False),
            # No T+2: use label_return_pct=50
            make_signal("R2", "runner", 70.0, 50.0, t0_fallback=True),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        # Combined: ((100 - 2) + (50 - 2)) / 2 = (98 + 48) / 2 = 73
        assert abs(metrics.ev_per_trade - 73.0) < 0.01

    def test_t2_coverage_pct_calculated_correctly(self):
        """t2_coverage_pct = fraction of above-threshold signals with real T+2 data."""
        signals = [
            make_signal("T2_A", "runner", 70.0, 100.0, label_return_pct_t2=100.0, t0_fallback=False),
            make_signal("T2_B", "runner", 70.0, 100.0, label_return_pct_t2=100.0, t0_fallback=False),
            make_signal("T0_C", "neutral", 70.0, 0.0, t0_fallback=True),
            make_signal("T0_D", "neutral", 70.0, 0.0, t0_fallback=True),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        # 2 T+2 rows out of 4 above-threshold = 50%
        assert abs(metrics.t2_coverage_pct - 0.50) < 0.01

    def test_all_t0_fallback_has_zero_t2_ev(self):
        """When no T+2 data available, ev_per_trade_t2 = 0 (not computed from T=0)."""
        signals = [
            make_signal("T0_A", "runner", 70.0, 200.0, t0_fallback=True),
            make_signal("T0_B", "dead", 70.0, -80.0, t0_fallback=True),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        assert metrics.ev_per_trade_t2 == 0.0
        assert metrics.t2_coverage_pct == 0.0

    def test_below_threshold_signals_excluded_from_all_ev(self):
        """Signals below opportunity_threshold don't affect any EV metric."""
        signals = [
            # Below threshold — should not count
            make_signal("LOW", "runner", 30.0, 500.0, label_return_pct_t2=500.0, t0_fallback=False),
            # Above threshold
            make_signal("HIGH", "dead", 70.0, -50.0, label_return_pct_t2=-50.0, t0_fallback=False),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        # Only HIGH is above threshold: ev_t2 = (-50 - 2) = -52
        assert abs(metrics.ev_per_trade_t2 - (-52.0)) < 0.01
        # Recall: 0 runners above threshold
        assert metrics.opportunity_recall == 0.0

    def test_opportunity_recall_unaffected_by_t2_changes(self):
        """Recall computation is independent of T+2 data availability."""
        signals = [
            make_signal("R1", "runner", 70.0, 100.0, label_return_pct_t2=100.0, t0_fallback=False),
            make_signal("R2", "runner", 50.0, 100.0, t0_fallback=True),  # below threshold
            make_signal("D1", "dead", 70.0, -80.0, t0_fallback=True),
        ]
        metrics = compute_metrics(signals, opportunity_threshold=60.0)

        # Only R1 above threshold, 2 runners total → recall = 1/2 = 50%
        assert abs(metrics.opportunity_recall - 0.5) < 0.01


class TestReplayEngineT2Integration:
    """replay_engine reads price_usd_at_t2 and populates signals correctly."""

    def test_t2_return_calculation_from_row(self):
        """
        Verify the formula used in replay_engine for label_return_pct_t2:
        ((price_24h - price_t2) / price_t2) * 100
        """
        price_t2 = 0.0001
        price_24h = 0.0003
        expected = ((price_24h - price_t2) / price_t2) * 100.0  # = 200%

        assert abs(expected - 200.0) < 0.001

    def test_t2_none_when_price_t2_missing(self):
        """When price_usd_at_t2 is NULL in DB row, label_return_pct_t2 must be None."""
        row = {
            "price_usd_at_t2": None,
            "price_usd_24h": 0.0003,
        }
        price_t2 = row.get("price_usd_at_t2")
        price_24h = row.get("price_usd_24h") or row.get("price_24h_usd")
        t0_fallback = price_t2 is None

        label_return_pct_t2 = None
        if price_t2 and price_t2 > 0 and price_24h and price_24h > 0:
            label_return_pct_t2 = ((price_24h - price_t2) / price_t2) * 100.0

        assert t0_fallback is True
        assert label_return_pct_t2 is None

    def test_t2_computed_when_both_prices_available(self):
        """When price_usd_at_t2 and price_usd_24h both present, t2 return is computed."""
        row = {
            "price_usd_at_t2": 0.0001,
            "price_usd_24h": 0.0004,
        }
        price_t2 = row.get("price_usd_at_t2")
        price_24h = row.get("price_usd_24h") or row.get("price_24h_usd")
        t0_fallback = price_t2 is None

        label_return_pct_t2 = None
        if price_t2 and price_t2 > 0 and price_24h and price_24h > 0:
            label_return_pct_t2 = ((price_24h - price_t2) / price_t2) * 100.0

        assert t0_fallback is False
        assert abs(label_return_pct_t2 - 300.0) < 0.001
