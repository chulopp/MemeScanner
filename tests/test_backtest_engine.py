"""
Tests — Fase 4 Backtest Engine
Unit tests for replay engine, cost model, metrics computation,
and Bayesian optimizer (mocked).
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.backtest.cost_model import compute_trade_cost, CostModelConfig, TradeCost
from src.backtest.metrics import BacktestSignal, compute_metrics
from src.backtest.labeler import _assign_label


# ============================================================
# Cost Model Tests
# ============================================================

def test_cost_model_micro_liquidity():
    """Token with <$50K liquidity should get 5% slippage."""
    cost = compute_trade_cost(liquidity_usd=10_000.0)
    assert cost.slippage_pct == 5.0
    # Total = entry + exit slippage + 2x priority fee
    assert cost.total_cost_pct > 10.0


def test_cost_model_small_liquidity():
    """Token with $50K–$200K liquidity should get 2% slippage."""
    cost = compute_trade_cost(liquidity_usd=100_000.0)
    assert cost.slippage_pct == 2.0
    assert cost.total_cost_pct > 4.0


def test_cost_model_medium_liquidity():
    """Token with >$200K liquidity should get 0.5% slippage."""
    cost = compute_trade_cost(liquidity_usd=500_000.0)
    assert cost.slippage_pct == 0.5
    assert cost.total_cost_pct > 1.0


def test_cost_model_custom_config():
    """Cost model config allows custom priority fee and trade size."""
    config = CostModelConfig(priority_fee_sol=0.005, sol_price_usd=200.0, trade_size_usd=100.0)
    cost = compute_trade_cost(liquidity_usd=10_000.0, config=config)
    # priority_fee_usd = 0.005 * 200 = 1.0 USD
    # priority_fee_pct = 1.0 / 100.0 * 100 = 1.0%
    # total = (5 * 2) + (1.0 * 2) = 12.0%
    assert cost.total_cost_pct == 12.0


# ============================================================
# Labeler Tests
# ============================================================

def test_label_runner():
    """Token with ≥100% return should be labeled 'runner'."""
    assert _assign_label(100.0) == "runner"
    assert _assign_label(250.0) == "runner"
    assert _assign_label(99.99) == "neutral"  # just below threshold


def test_label_dead():
    """Token with ≤-70% return should be labeled 'dead'."""
    assert _assign_label(-70.0) == "dead"
    assert _assign_label(-99.9) == "dead"
    assert _assign_label(-69.99) == "neutral"  # just above threshold


def test_label_neutral():
    """Token between -70% and +100% should be labeled 'neutral'."""
    assert _assign_label(0.0) == "neutral"
    assert _assign_label(50.0) == "neutral"
    assert _assign_label(-50.0) == "neutral"


# ============================================================
# Metrics Computation Tests
# ============================================================

def _make_signal(
    passed: bool,
    label: str,
    return_pct: float,
    score: float,
    liquidity: float = 50_000.0
) -> BacktestSignal:
    cost = compute_trade_cost(liquidity)
    return BacktestSignal(
        token_address=f"MOCK_{label[:3].upper()}_{id(label)}",
        symbol="TEST",
        passed_safety=passed,
        opportunity_score=score,
        label=label,
        label_return_pct=return_pct,
        liquidity_usd=liquidity,
        total_cost_pct=cost.total_cost_pct
    )


def test_metrics_filter_precision():
    """Filter precision = non-rug passed / total passed."""
    signals = [
        _make_signal(True, "runner", 150.0, 80.0),
        _make_signal(True, "neutral", 10.0, 55.0),
        _make_signal(True, "dead", -80.0, 70.0),   # rug that passed — bad
        _make_signal(False, "dead", -90.0, 0.0),   # correctly rejected
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    # Passed: 3 (runner, neutral, dead). Non-rug passed: 2 (runner, neutral)
    assert metrics.total_passed_safety == 3
    assert metrics.filter_precision == pytest.approx(2 / 3, rel=1e-3)


def test_metrics_opportunity_recall():
    """Opportunity recall = runners above threshold / total runners."""
    signals = [
        _make_signal(True, "runner", 200.0, 80.0),   # runner, above threshold ✓
        _make_signal(True, "runner", 110.0, 45.0),   # runner, below threshold ✗
        _make_signal(True, "dead", -80.0, 75.0),
        _make_signal(False, "runner", 300.0, 0.0),   # runner rejected at safety ✗
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    # Total runners: 3. Runners above threshold: 1 (only the one passed safety & score>=60)
    assert metrics.runner_count == 3
    assert metrics.opportunity_recall == pytest.approx(1 / 3, rel=1e-3)


def test_metrics_ev_per_trade_positive():
    """EV = mean(return_pct - cost_pct) for tokens above threshold."""
    signals = [
        _make_signal(True, "runner", 200.0, 75.0, 100_000.0),   # cost ~4.0%, EV ~196%
        _make_signal(True, "runner", 150.0, 80.0, 100_000.0),   # cost ~4.0%, EV ~146%
        _make_signal(True, "neutral", 5.0, 65.0, 10_000.0),     # cost ~10.2%, EV ~-5.2%
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    assert metrics.ev_per_trade > 0.0
    assert metrics.ev_positive is True


def test_metrics_ev_per_trade_negative():
    """When all tokens above threshold are dead, EV should be negative."""
    signals = [
        _make_signal(True, "dead", -80.0, 70.0, 10_000.0),  # cost ~10.2%, EV ~ -90.2%
        _make_signal(True, "dead", -75.0, 65.0, 10_000.0),  # EV ~ -85.2%
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    assert metrics.ev_per_trade < 0.0
    assert metrics.ev_positive is False


def test_metrics_empty_signals():
    """Empty signal list should return zeroed metrics gracefully."""
    metrics = compute_metrics([], opportunity_threshold=60.0)
    assert metrics.dataset_size == 0
    assert metrics.filter_precision == 0.0
    assert metrics.ev_per_trade == 0.0


# ============================================================
# Offline Safety Check Tests
# ============================================================

def test_offline_safety_rejects_zero_liquidity():
    """Token with near-zero liquidity should be rejected."""
    from src.backtest.replay_engine import _offline_safety_check
    passed, reason = _offline_safety_check({"liquidity_usd": 0.0, "volume_24h_usd": 0.0})
    assert passed is False
    assert reason == "OFFLINE:ZERO_LIQUIDITY"


def test_offline_safety_rejects_wash_trade():
    """Token with volume 60x liquidity should be rejected as wash trade."""
    from src.backtest.replay_engine import _offline_safety_check
    passed, reason = _offline_safety_check({"liquidity_usd": 1_000.0, "volume_24h_usd": 70_000.0})
    assert passed is False
    assert reason == "OFFLINE:WASH_TRADE_PROXY"


def test_offline_safety_passes_normal_token():
    """Normal token with reasonable liquidity and volume should pass."""
    from src.backtest.replay_engine import _offline_safety_check
    passed, reason = _offline_safety_check({"liquidity_usd": 50_000.0, "volume_24h_usd": 100_000.0})
    assert passed is True
    assert reason is None


# ============================================================
# Replay Engine Integration Test (mocked DB)
# ============================================================

@pytest.mark.asyncio
async def test_replay_engine_returns_metrics_from_labeled_data():
    """Verify replay engine processes labeled tokens and returns BacktestMetrics."""
    from src.backtest.replay_engine import run_replay

    mock_rows = [
        {
            "token_address": "TokenA1111111111111111111111111111111111111",
            "symbol": "RUNNER",
            "name": "Runner Token",
            "launch_venue": "pump_fun",
            "listed_at": "2024-01-01T00:00:00+00:00",
            "label": "runner",
            "label_return_pct": 220.0,
            "liquidity_usd": 80_000.0,
            "volume_24h_usd": 200_000.0,
            "price_usd_at_listing": 0.0001,
            "raw_dexscreener": {}
        },
        {
            "token_address": "TokenB2222222222222222222222222222222222222",
            "symbol": "DEAD",
            "name": "Dead Token",
            "launch_venue": "pump_fun",
            "listed_at": "2024-01-01T00:00:00+00:00",
            "label": "dead",
            "label_return_pct": -85.0,
            "liquidity_usd": 500.0,   # will trigger zero-liquidity rejection
            "volume_24h_usd": 50.0,
            "price_usd_at_listing": 0.0001,
            "raw_dexscreener": {}
        },
    ]

    mock_score_result = MagicMock()
    mock_score_result.opportunity_score = 75.0

    with patch("src.backtest.replay_engine.db_manager.query", AsyncMock(return_value=mock_rows)):
        with patch("src.backtest.replay_engine.load_p80_priority_fee_from_supabase", AsyncMock(return_value=0.001)):
            with patch("src.backtest.replay_engine.OpportunityScorer.score_token", AsyncMock(return_value=mock_score_result)):
                metrics = await run_replay(opportunity_threshold=60.0, limit=10)

    assert metrics.dataset_size == 2
    assert metrics.runner_count == 1
    assert metrics.dead_count == 1
    # Dead token has ~0 liquidity → rejected by offline safety
    assert metrics.total_rejected_safety == 1
    assert metrics.total_passed_safety == 1
    # The runner passed safety with score 75 ≥ 60 → recall = 1/1 = 100%
    assert metrics.opportunity_recall == 1.0
