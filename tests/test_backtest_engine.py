"""
Tests — Fase 4 Backtest Engine & Walk-Forward Cross Validation
Unit tests for:
- 5-Fold Time-Series Walk-Forward Cross Validation (anti-overfitting & temporal integrity)
- T=0 initial launch price calculation (anti-lookahead bias)
- 24h Outcome resolution & labeling
- Realistic Graduated Cost Model
- Multi-factor Replay Engine
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from src.ingestion.schemas import RawTokenEvent
from src.backtest.cost_model import compute_trade_cost, CostModelConfig, TradeCost
from src.backtest.metrics import BacktestSignal, compute_metrics
from src.backtest.labeler import assign_label
from src.backtest.data_collector import _calculate_initial_launch_price
from src.backtest.cross_validation import split_time_series_folds, evaluate_walk_forward_cv


# ============================================================
# Cost Model Tests
# ============================================================

def test_cost_model_micro_liquidity():
    """Token with <$50K liquidity should get 5% slippage."""
    cost = compute_trade_cost(liquidity_usd=10_000.0)
    assert cost.slippage_pct == 5.0
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
    assert cost.total_cost_pct == 12.0


# ============================================================
# T=0 Launch Price Calculation Tests (Anti-Lookahead Bias)
# ============================================================

@pytest.mark.asyncio
async def test_calculate_initial_launch_price_pump_fun():
    """Pump.fun token starts at virtual 30 SOL reserve / 1.073B tokens * SOL price."""
    event = RawTokenEvent(
        token_address="MintPump111111111111111111111111111111111111",
        symbol="PUMP",
        name="Pump Token",
        launch_venue="pump_fun",
        initial_sol_liquidity=30.0,
        total_supply=1_000_000_000.0
    )
    price = await _calculate_initial_launch_price(event, sol_price_usd=200.0)
    # 30 / 1.073B * 200 = ~0.00000559 USD
    assert price > 0.0
    assert 0.000004 < price < 0.000008


# ============================================================
# Outcome Labeler Tests
# ============================================================

def test_label_runner():
    """Token with ≥100% return should be labeled 'runner'."""
    assert assign_label(100.0, liquidity_usd=50_000.0) == "runner"
    assert assign_label(250.0, liquidity_usd=50_000.0) == "runner"
    assert assign_label(99.99, liquidity_usd=50_000.0) == "neutral"


def test_label_dead():
    """Token with ≤-70% return or dried up liquidity should be labeled 'dead'."""
    assert assign_label(-70.0, liquidity_usd=50_000.0) == "dead"
    assert assign_label(-99.9, liquidity_usd=50_000.0) == "dead"
    assert assign_label(50.0, liquidity_usd=200.0) == "dead"  # liquidity collapsed


def test_label_neutral():
    """Token between -70% and +100% with healthy liquidity should be labeled 'neutral'."""
    assert assign_label(0.0, liquidity_usd=20_000.0) == "neutral"
    assert assign_label(50.0, liquidity_usd=20_000.0) == "neutral"
    assert assign_label(-50.0, liquidity_usd=20_000.0) == "neutral"


# ============================================================
# 5-Fold Walk-Forward Cross Validation Tests (Anti-Overfitting)
# ============================================================

def test_split_time_series_folds_chronological_integrity():
    """Walk-forward splits must strictly preserve time ordering (train max <= test min)."""
    base_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    mock_tokens = [
        {
            "token_address": f"MINT_{i}",
            "listed_at": (base_time + timedelta(hours=i)).isoformat(),
            "label": "neutral",
            "label_return_pct": 10.0,
            "liquidity_usd": 10_000.0
        }
        for i in range(50)
    ]

    folds = split_time_series_folds(mock_tokens, n_splits=5)
    assert len(folds) == 4

    for k, (train_set, test_set) in enumerate(folds):
        assert len(train_set) > 0
        assert len(test_set) > 0
        train_max_time = max(t["listed_at"] for t in train_set)
        test_min_time = min(t["listed_at"] for t in test_set)
        # Train timestamp must be strictly less than or equal to test timestamp (no future leakage!)
        assert train_max_time <= test_min_time


# ============================================================
# Metrics & Replay Engine Tests
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
        _make_signal(True, "dead", -80.0, 70.0),
        _make_signal(False, "dead", -90.0, 0.0),
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    assert metrics.total_passed_safety == 3
    assert metrics.filter_precision == pytest.approx(2 / 3, rel=1e-3)


def test_metrics_opportunity_recall():
    """Opportunity recall = runners above threshold / total runners."""
    signals = [
        _make_signal(True, "runner", 200.0, 80.0),
        _make_signal(True, "runner", 110.0, 45.0),
        _make_signal(True, "dead", -80.0, 75.0),
        _make_signal(False, "runner", 300.0, 0.0),
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    assert metrics.runner_count == 3
    assert metrics.opportunity_recall == pytest.approx(1 / 3, rel=1e-3)


def test_metrics_ev_per_trade_positive():
    """EV = mean(return_pct - cost_pct) for tokens above threshold."""
    signals = [
        _make_signal(True, "runner", 200.0, 75.0, 100_000.0),
        _make_signal(True, "runner", 150.0, 80.0, 100_000.0),
        _make_signal(True, "neutral", 5.0, 65.0, 10_000.0),
    ]
    metrics = compute_metrics(signals, opportunity_threshold=60.0)
    assert metrics.ev_per_trade > 0.0
    assert metrics.ev_positive is True


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
            "liquidity_usd": 500.0,
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
    assert metrics.total_rejected_safety == 1
    assert metrics.total_passed_safety == 1
    assert metrics.opportunity_recall == 1.0
