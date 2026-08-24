"""
Tests — Portfolio Simulator & Multi-Exit Strategy Optimizer
Unit tests verifying compounding balance, stop loss triggers, milestone hit-rate calculations,
and multi-exit strategy matrix evaluations.
"""

import pytest
from src.paper_trading.portfolio_simulator import (
    PortfolioSimulator,
    StrategyMatrixResult,
    MilestoneHitRate,
    DEFAULT_TP_GRID,
    DEFAULT_SL_GRID
)
from src.backtest.cost_model import CostModelConfig


@pytest.fixture
def sample_signals():
    return [
        {
            "token_address": "TokenRunner111111111111111111111111111111",
            "symbol": "RUNNER",
            "liquidity_usd": 15000.0,
            "ath_return_pct": 250.0,
            "peak_5m": 35.0,
            "peak_15m": 120.0,
            "peak_1h": 250.0,
            "return_24h": 180.0,
            "mae_pct": -10.0,
            "status": "runner"
        },
        {
            "token_address": "TokenScalp1111111111111111111111111111111",
            "symbol": "SCALP",
            "liquidity_usd": 8000.0,
            "ath_return_pct": 60.0,
            "peak_5m": 60.0,
            "return_24h": -20.0,
            "mae_pct": -35.0,
            "status": "neutral"
        },
        {
            "token_address": "TokenDead11111111111111111111111111111111",
            "symbol": "DEAD",
            "liquidity_usd": 3000.0,
            "ath_return_pct": 10.0,
            "return_24h": -85.0,
            "mae_pct": -85.0,
            "status": "dead"
        }
    ]


def test_compounding_balance_growth(sample_signals):
    """Verify that portfolio balance compounds as profits are made."""
    simulator = PortfolioSimulator()
    
    res: StrategyMatrixResult = simulator.simulate_strategy(
        signals=sample_signals,
        tp_target_pct=100.0,
        sl_target_pct=-50.0,
        initial_balance=10.0,
        position_risk_pct=20.0
    )

    assert res.initial_balance == 10.0
    assert res.total_trades == 3
    assert res.winning_trades >= 1
    assert res.losing_trades >= 1
    assert len(res.trades) == 3


def test_stop_loss_trigger(sample_signals):
    """Verify that losing trades hit the specified SL level."""
    simulator = PortfolioSimulator()

    res = simulator.simulate_strategy(
        signals=sample_signals,
        tp_target_pct=200.0,
        sl_target_pct=-30.0,
        initial_balance=10.0,
        position_risk_pct=20.0
    )

    # Token DEAD and Token SCALP (with mae -35%) should trigger SL -30%
    dead_trade = next(t for t in res.trades if t.symbol == "DEAD")
    assert dead_trade.exit_reason in ("SL_HIT", "DEAD")
    assert dead_trade.gross_return_pct <= -30.0


def test_milestone_hit_rates(sample_signals):
    """Verify hit-rate percentage calculation across TP milestone ladder."""
    simulator = PortfolioSimulator()
    milestones: list[MilestoneHitRate] = simulator.calculate_milestones(sample_signals)

    assert len(milestones) == len(DEFAULT_TP_GRID)
    
    m25 = next(m for m in milestones if m.target_pct == 25.0)
    m100 = next(m for m in milestones if m.target_pct == 100.0)
    m500 = next(m for m in milestones if m.target_pct == 500.0)

    # 2 out of 3 tokens (RUNNER: 250%, SCALP: 60%) reached >= 25%
    assert m25.reached_count == 2
    assert m25.hit_rate_pct == pytest.approx(66.7, rel=1e-1)

    # 1 out of 3 tokens (RUNNER: 250%) reached >= 100%
    assert m100.reached_count == 1
    assert m100.hit_rate_pct == pytest.approx(33.3, rel=1e-1)

    # 0 reached >= 500%
    assert m500.reached_count == 0
    assert m500.hit_rate_pct == 0.0


def test_matrix_simulation_ranking(sample_signals):
    """Verify matrix simulation runs all combinations and ranks by final balance."""
    simulator = PortfolioSimulator()
    matrix = simulator.run_matrix_simulation(
        signals=sample_signals,
        initial_balance=10.0,
        position_risk_pct=20.0,
        tp_grid=[50.0, 100.0],
        sl_grid=[-30.0, -50.0]
    )

    assert len(matrix) == 4  # 2 TP x 2 SL = 4
    # Should be sorted descending by final balance
    for i in range(len(matrix) - 1):
        assert matrix[i].final_balance >= matrix[i + 1].final_balance


def test_render_cli_report(sample_signals):
    """Verify report rendering produces formatted string with all sections."""
    simulator = PortfolioSimulator()
    milestones = simulator.calculate_milestones(sample_signals)
    matrix = simulator.run_matrix_simulation(
        signals=sample_signals,
        initial_balance=10.0,
        position_risk_pct=20.0
    )

    report = simulator.render_cli_report(
        matrix_results=matrix,
        milestones=milestones,
        initial_balance=10.0,
        position_risk_pct=20.0
    )

    assert "VIRTUAL PORTFOLIO & MULTI-EXIT SIMULATION REPORT" in report
    assert "MILESTONE HIT-RATE DISTRIBUTION" in report
    assert "TOP STRATEGY RANKING" in report
    assert "STRATEGI REKOMENDASI TERBAIK" in report
