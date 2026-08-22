import pytest
from unittest.mock import AsyncMock, patch
from src.ingestion.schemas import RawTokenEvent
from src.filters.pump_safety import pump_safety_filter
from src.filters.raydium_safety import raydium_safety_filter
from src.filters.pipeline import filter_pipeline


@pytest.mark.asyncio
async def test_pump_safety_filter_pass():
    """Verify Pump.fun filter passes when dev allocation is <= 10% and clean wallet."""
    event = RawTokenEvent(
        token_address="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
        symbol="SAFE",
        name="Safe Pump Coin",
        deployer_wallet_address="4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
        launch_venue="pump_fun",
        initial_buy_amount=50_000_000.0,  # 5% of 1B supply
        total_supply=1_000_000_000.0
    )

    with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
        mock_scalp.return_value = {"flags_count": 0, "details": {}}
        result = await pump_safety_filter.evaluate(event)

    assert result.filter_pass is True
    assert result.dev_holding_pct == 5.0
    assert result.rejection_reason is None


@pytest.mark.asyncio
async def test_pump_safety_filter_reject_high_dev_allocation():
    """Verify Pump.fun filter rejects when dev buys > 10% of total supply."""
    event = RawTokenEvent(
        token_address="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
        symbol="GREED",
        name="Greedy Dev Coin",
        deployer_wallet_address="4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
        launch_venue="pump_fun",
        initial_buy_amount=200_000_000.0,  # 20% of 1B supply
        total_supply=1_000_000_000.0
    )

    with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
        mock_scalp.return_value = {"flags_count": 0, "details": {}}
        result = await pump_safety_filter.evaluate(event)

    assert result.filter_pass is False
    assert result.dev_holding_pct == 20.0
    assert "Dev initial allocation too high" in result.rejection_reason


@pytest.mark.asyncio
async def test_raydium_safety_filter_reject_active_mint():
    """Verify Raydium filter rejects when Mint Authority is active."""
    event = RawTokenEvent(
        token_address="DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        symbol="BONK_FAKE",
        name="Fake Bonk",
        launch_venue="raydium"
    )

    with patch("src.utils.solana_rpc.solana_rpc.get_mint_info", new_callable=AsyncMock) as mock_mint:
        mock_mint.return_value = {
            "mint_authority": "4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd",
            "freeze_authority": None,
            "supply": 100_000_000_000,
            "decimals": 5,
            "exists": True
        }
        with patch("src.utils.solana_rpc.solana_rpc.get_token_largest_accounts", new_callable=AsyncMock) as mock_holders:
            mock_holders.return_value = []
            with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
                mock_scalp.return_value = {"flags_count": 0, "details": {}}

                result = await raydium_safety_filter.evaluate(event)

    assert result.filter_pass is False
    assert "Mint Authority active" in result.rejection_reason


@pytest.mark.asyncio
async def test_filter_pipeline_execution():
    """Verify FilterPipeline processes token, updates DB, and returns result."""
    event = RawTokenEvent(
        token_address="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
        symbol="PIPELINE_TEST",
        name="Test Pipeline",
        launch_venue="pump_fun",
        initial_buy_amount=30_000_000.0,
        total_supply=1_000_000_000.0
    )

    with patch("src.filters.instant_scalp.instant_scalp_filter.evaluate", new_callable=AsyncMock) as mock_scalp:
        mock_scalp.return_value = {"flags_count": 0, "details": {}}
        result = await filter_pipeline.process_token(event)

    assert result.filter_pass is True
    assert result.token_address == event.token_address
