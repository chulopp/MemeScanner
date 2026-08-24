"""
Tests — Fase 5 Paper Trading
Unit tests for signal recording, price fetcher, outcome resolution, evaluator, and Telegram notifier.
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

from src.paper_trading.price_fetcher import PriceSnapshot
from src.paper_trading.outcome_worker import _classify_outcome


# ============================================================
# Price Fetcher Tests
# ============================================================

@pytest.mark.asyncio
async def test_price_fetcher_dexscreener_tier1():
    """Tier 1 DexScreener should return PriceSnapshot when API responds."""
    from src.paper_trading.price_fetcher import _fetch_dexscreener

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "pairs": [{
            "priceUsd": "0.0001234",
            "liquidity": {"usd": 15000},
            "volume": {"h24": 50000}
        }]
    }

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    snap = await _fetch_dexscreener(mock_client, "MintTest1111111111111111111111111111111111")
    assert snap is not None
    assert snap.price_usd == pytest.approx(0.0001234, rel=1e-3)
    assert snap.liquidity_usd == 15000
    assert snap.source == "dexscreener"


@pytest.mark.asyncio
async def test_price_fetcher_fallback_on_404():
    """If Tier 1 returns 404, result should be None (triggering fallback)."""
    from src.paper_trading.price_fetcher import _fetch_dexscreener

    mock_response = MagicMock()
    mock_response.status_code = 404

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    snap = await _fetch_dexscreener(mock_client, "MintNotFound1111111111111111111111111111")
    assert snap is None


# ============================================================
# Outcome Classification Tests
# ============================================================

def test_classify_outcome_runner():
    """Token with ≥100% return and healthy liquidity should be 'runner'."""
    assert _classify_outcome(100.0, 10_000.0) == "runner"
    assert _classify_outcome(250.0, 50_000.0) == "runner"


def test_classify_outcome_dead():
    """Token with ≤-70% return or dried liquidity should be 'dead'."""
    assert _classify_outcome(-70.0, 10_000.0) == "dead"
    assert _classify_outcome(50.0, 300.0) == "dead"  # liquidity below 500


def test_classify_outcome_neutral():
    """Token between -70% and +100% with healthy liquidity should be 'neutral'."""
    assert _classify_outcome(0.0, 10_000.0) == "neutral"
    assert _classify_outcome(80.0, 10_000.0) == "neutral"
    assert _classify_outcome(-50.0, 10_000.0) == "neutral"


# ============================================================
# Signal Recorder Tests
# ============================================================

@pytest.mark.asyncio
async def test_signal_recorder_records_to_db():
    """Signal recorder should insert a record into paper_signals table."""
    from src.paper_trading.signal_recorder import record_signal, _extract_filter_tags
    from src.ingestion.schemas import RawTokenEvent
    from src.filters.schemas import SafetyCheckResult

    event = RawTokenEvent(
        token_address="TestMint111111111111111111111111111111111111",
        symbol="TEST",
        name="Test Token",
        launch_venue="pump_fun"
    )

    safety = SafetyCheckResult(
        token_address="TestMint111111111111111111111111111111111111",
        venue="pump_fun",
        filter_pass=True,
        opportunity_score=75.0,
        opportunity_breakdown={"vol_velocity": {"score": 80}, "smart_money": {"score": 70}, "global_fee": {"score": 65}},
        mint_authority_renounced=True,
        freeze_authority_renounced=True,
        lp_locked_or_burned=True,
        honeypot_check_passed=True
    )

    mock_price = PriceSnapshot(price_usd=0.0001, liquidity_usd=10_000, volume_24h_usd=5000, source="dexscreener")

    with patch("src.paper_trading.signal_recorder.fetch_price", AsyncMock(return_value=mock_price)):
        with patch("src.paper_trading.signal_recorder.db_manager.insert", AsyncMock(return_value=[{"id": "test-uuid-123"}])):
            with patch("src.paper_trading.signal_recorder.telegram_notifier.send_signal_notification", AsyncMock(return_value=99)):
                with patch("src.paper_trading.signal_recorder.db_manager.update", AsyncMock()):
                    signal_id = await record_signal(event, safety)

    assert signal_id == "test-uuid-123"


def test_extract_filter_tags():
    """Filter tags should reflect which checks passed."""
    from src.paper_trading.signal_recorder import _extract_filter_tags
    from src.filters.schemas import SafetyCheckResult

    result = SafetyCheckResult(
        token_address="TestMint111111111111111111111111111111111111",
        venue="pump_fun",
        filter_pass=True,
        mint_authority_renounced=True,
        freeze_authority_renounced=True,
        lp_locked_or_burned=True,
        honeypot_check_passed=True,
        instant_scalp_flags_count=0,
        sniper_bundle_pct=5.0
    )

    tags = _extract_filter_tags(result)
    assert "mint_renounced" in tags
    assert "freeze_renounced" in tags
    assert "lp_locked" in tags
    assert "honeypot_clear" in tags
    assert "no_scalp_flags" in tags
    assert "low_bundle_risk" in tags


# ============================================================
# Evaluator Tests
# ============================================================

def test_evaluator_cost_models():
    """Both cost models should return correct slippage for different liquidity tiers."""
    from src.paper_trading.evaluator import _cost_model_a, _cost_model_b

    # Cost Model A (Conservative)
    assert _cost_model_a(5_000) == 10.0     # < 50K
    assert _cost_model_a(100_000) == 4.0    # 50K-200K
    assert _cost_model_a(500_000) == 1.0    # > 200K

    # Cost Model B (PRD 10.3)
    assert _cost_model_b(5_000) == 17.5     # < 10K
    assert _cost_model_b(30_000) == 10.0    # 10K-50K
    assert _cost_model_b(100_000) == 5.0    # > 50K


def test_evaluator_ev_calculation():
    """EV calculation should correctly apply cost model and average net returns."""
    from src.paper_trading.evaluator import _calculate_ev, _cost_model_a

    # 2 trades: one +200% on $100K liq (cost 4%), one -80% on $10K liq (cost 10%)
    returns = [(200.0, 100_000.0), (-80.0, 10_000.0)]
    ev = _calculate_ev(returns, _cost_model_a)
    # (200 - 4 + (-80 - 10)) / 2 = (196 + (-90)) / 2 = 106 / 2 = 53.0
    assert ev == pytest.approx(53.0, rel=1e-3)
