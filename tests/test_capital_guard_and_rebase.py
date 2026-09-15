import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
import uuid

from src.ingestion.schemas import RawTokenEvent
from src.paper_trading.position_tracker import (
    PositionTracker,
    ActivePosition,
    FROZEN_PARAMS,
)


@pytest.fixture
def sample_event():
    return RawTokenEvent(
        token_address="2zcGoHYwuz6zxPy3zvCQuarRaqX8JzsCX9vAHNXSpump",
        symbol="TESTCOIN",
        name="Test Coin",
        deployer_wallet_address="9s4ji7MRh6xx9PVx7D4by8YjpHMfPVtHfYuMUy8VaTTp",
        launch_venue="pump_fun",
        launch_timestamp=datetime.now(tz=timezone.utc),
        initial_buy_amount=1000.0,
        total_supply=1_000_000_000.0,
        initial_sol_liquidity=30.0,
        source="NEW_PAIR",
    )


@pytest.mark.asyncio
async def test_dynamic_sizing_rebases_to_current_equity(sample_event):
    """Verify that position sizing is 2% of current equity, not fixed to initial capital."""
    tracker = PositionTracker()

    # Mock portfolio summary where equity is $50.0 and cash is $40.0
    mock_summary = {
        "starting_capital": 100.0,
        "available_cash": 40.0,
        "allocated_usd": 10.0,
        "total_floating_usd": 0.0,
        "total_equity": 50.0,
        "portfolio_roi_pct": -50.0,
        "open_positions": [],
        "closed_trades": [],
    }

    with patch.object(tracker, "get_open_count", new=AsyncMock(return_value=1)), \
         patch.object(tracker, "is_duplicate", new=AsyncMock(return_value=False)), \
         patch.object(tracker, "get_portfolio_summary", new=AsyncMock(return_value=mock_summary)), \
         patch("src.paper_trading.position_tracker.fetch_price", new=AsyncMock(return_value=MagicMock(price_usd=0.005, market_cap_usd=5000))), \
         patch("src.paper_trading.position_tracker.db_manager.insert", new=AsyncMock(return_value={"id": "mock"})):

        pos_id = await tracker.open_position(
            event=sample_event,
            opportunity_score=65.0,
        )

        assert pos_id is not None
        opened_pos = tracker._active[pos_id]
        # 2% of $50.0 = $1.00
        assert opened_pos.position_size_usd == 1.0


@pytest.mark.asyncio
async def test_cash_guard_blocks_opening_when_cash_negative(sample_event):
    """When available cash is negative, system MUST skip opening and record SKIPPED_INSUFFICIENT_CASH."""
    tracker = PositionTracker()

    # Simulating the exact bug report state: available_cash = -$7.22, equity = $5.81
    mock_summary = {
        "starting_capital": 100.0,
        "available_cash": -7.22,
        "allocated_usd": 14.0,
        "total_floating_usd": -0.97,
        "total_equity": 5.81,
        "portfolio_roi_pct": -94.19,
        "open_positions": [],
        "closed_trades": [],
    }

    mock_record_skipped = AsyncMock()

    with patch.object(tracker, "get_open_count", new=AsyncMock(return_value=7)), \
         patch.object(tracker, "is_duplicate", new=AsyncMock(return_value=False)), \
         patch.object(tracker, "get_portfolio_summary", new=AsyncMock(return_value=mock_summary)), \
         patch.object(tracker, "_record_skipped", new=mock_record_skipped):

        pos_id = await tracker.open_position(
            event=sample_event,
            opportunity_score=70.0,
        )

        assert pos_id is None
        assert len(tracker._active) == 0
        mock_record_skipped.assert_called_once()
        call_kwargs = mock_record_skipped.call_args[1]
        assert call_kwargs["skipped_reason"] == "SKIPPED_INSUFFICIENT_CASH"


@pytest.mark.asyncio
async def test_cash_guard_blocks_when_cash_less_than_required_size(sample_event):
    """When available cash is positive but less than required position size, block trade."""
    tracker = PositionTracker()

    # Equity is $100 -> dynamic size is $2.00, but cash is only $1.50
    mock_summary = {
        "starting_capital": 100.0,
        "available_cash": 1.50,
        "allocated_usd": 98.50,
        "total_floating_usd": 0.0,
        "total_equity": 100.0,
        "portfolio_roi_pct": 0.0,
        "open_positions": [],
        "closed_trades": [],
    }

    mock_record_skipped = AsyncMock()

    with patch.object(tracker, "get_open_count", new=AsyncMock(return_value=5)), \
         patch.object(tracker, "is_duplicate", new=AsyncMock(return_value=False)), \
         patch.object(tracker, "get_portfolio_summary", new=AsyncMock(return_value=mock_summary)), \
         patch.object(tracker, "_record_skipped", new=mock_record_skipped):

        pos_id = await tracker.open_position(
            event=sample_event,
            opportunity_score=68.0,
        )

        assert pos_id is None
        mock_record_skipped.assert_called_once()
        assert mock_record_skipped.call_args[1]["skipped_reason"] == "SKIPPED_INSUFFICIENT_CASH"


@pytest.mark.asyncio
async def test_capital_depletion_ruin_guard(sample_event):
    """When equity is exhausted (<= min size), system must refuse to open trades."""
    tracker = PositionTracker()

    mock_summary = {
        "starting_capital": 100.0,
        "available_cash": 0.02,
        "allocated_usd": 0.0,
        "total_floating_usd": 0.0,
        "total_equity": 0.02,  # < min_position_size_usd (0.05)
        "portfolio_roi_pct": -99.98,
        "open_positions": [],
        "closed_trades": [],
    }

    mock_record_skipped = AsyncMock()

    with patch.object(tracker, "get_open_count", new=AsyncMock(return_value=0)), \
         patch.object(tracker, "is_duplicate", new=AsyncMock(return_value=False)), \
         patch.object(tracker, "get_portfolio_summary", new=AsyncMock(return_value=mock_summary)), \
         patch.object(tracker, "_record_skipped", new=mock_record_skipped):

        pos_id = await tracker.open_position(
            event=sample_event,
            opportunity_score=80.0,
        )

        assert pos_id is None
        mock_record_skipped.assert_called_once()
        assert mock_record_skipped.call_args[1]["skipped_reason"] == "SKIPPED_INSUFFICIENT_CASH"


@pytest.mark.asyncio
async def test_allocated_usd_sums_actual_position_sizes():
    """Verify that get_portfolio_summary sums actual position sizes of active positions."""
    tracker = PositionTracker()

    # Create 3 active positions with different sizes ($1.0, $0.5, $2.0)
    tracker._active = {
        "pos1": ActivePosition(
            position_id="pos1",
            token_address="Mint1",
            symbol="COIN1",
            signal_source="PINTU_A",
            entry_price_usd=0.01,
            entry_time=datetime.now(tz=timezone.utc),
            position_size_usd=1.0,
        ),
        "pos2": ActivePosition(
            position_id="pos2",
            token_address="Mint2",
            symbol="COIN2",
            signal_source="PINTU_A",
            entry_price_usd=0.02,
            entry_time=datetime.now(tz=timezone.utc),
            position_size_usd=0.5,
        ),
        "pos3": ActivePosition(
            position_id="pos3",
            token_address="Mint3",
            symbol="COIN3",
            signal_source="PINTU_B",
            entry_price_usd=0.03,
            entry_time=datetime.now(tz=timezone.utc),
            position_size_usd=2.0,
        ),
    }

    with patch("src.paper_trading.position_tracker.db_manager.query", new=AsyncMock(return_value=[])):
        summary = await tracker.get_portfolio_summary()

        # Allocated must be 1.0 + 0.5 + 2.0 = 3.5 (not 3 * 2.0 = 6.0)
        assert summary["allocated_usd"] == 3.5
        # Available cash = 100 - 3.5 = 96.5
        assert summary["available_cash"] == 96.5
        assert summary["total_equity"] == 96.5 + 3.5  # 100.0
