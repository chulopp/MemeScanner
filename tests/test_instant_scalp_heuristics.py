import pytest
from unittest.mock import AsyncMock, patch
from src.filters.instant_scalp import instant_scalp_filter


@pytest.mark.asyncio
async def test_instant_scalp_flags_trigger():
    """Verify Ponyin 4-filter heuristic triggers when gas spike and young wallets occur."""
    with patch("src.utils.solana_rpc.solana_rpc.get_recent_prioritization_fees", new_callable=AsyncMock) as mock_gas:
        mock_gas.return_value = [200_000, 350_000, 600_000]  # Gas spike
        with patch("src.utils.solana_rpc.solana_rpc.get_wallet_age_days", new_callable=AsyncMock) as mock_age:
            mock_age.return_value = 0.2  # < 1 day old
            with patch("src.utils.solana_rpc.solana_rpc.get_sol_balance", new_callable=AsyncMock) as mock_bal:
                mock_bal.return_value = 0.05  # < 0.2 SOL low balance

                result = await instant_scalp_filter.evaluate(
                    mint_address="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
                    top_holder_pubkeys=[
                        "4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd1",
                        "4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd2"
                    ],
                    initial_buy_tokens=0.0
                )

    assert result["flags_count"] >= 2
    assert result["scalp_flag_gas_spike"] is True
    assert result["scalp_flag_young_wallet"] is True
    assert result["scalp_flag_low_balance"] is True


@pytest.mark.asyncio
async def test_instant_scalp_clean_token():
    """Verify Ponyin heuristic produces 0 flags for healthy token environment."""
    with patch("src.utils.solana_rpc.solana_rpc.get_recent_prioritization_fees", new_callable=AsyncMock) as mock_gas:
        mock_gas.return_value = [5_000, 10_000, 15_000]  # Normal low gas
        with patch("src.utils.solana_rpc.solana_rpc.get_wallet_age_days", new_callable=AsyncMock) as mock_age:
            mock_age.return_value = 45.0  # Mature wallet (45 days old)
            with patch("src.utils.solana_rpc.solana_rpc.get_sol_balance", new_callable=AsyncMock) as mock_bal:
                mock_bal.return_value = 12.5  # Healthy 12.5 SOL balance

                result = await instant_scalp_filter.evaluate(
                    mint_address="7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
                    top_holder_pubkeys=["4Nd1mBQtrMKBCzyLz65aoFi19797KNnVNVNmnNqWCcd1"],
                    initial_buy_tokens=10_000_000.0,
                    total_supply=1_000_000_000.0
                )

    assert result["flags_count"] == 0
    assert result["scalp_flag_gas_spike"] is False
    assert result["scalp_flag_young_wallet"] is False
    assert result["scalp_flag_low_balance"] is False
    assert result["scalp_flag_pump_anomaly"] is False
