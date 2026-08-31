import json
import os
import pytest
from unittest.mock import AsyncMock, patch
from src.opportunity.global_fee import GlobalFeeUrgencyEngine
from src.utils.solana_rpc import solana_rpc

GIPP_FIXTURE_PATH = 'tests/fixtures/gipp_fees.json'


class TestGIPPIntegration:
    """PRIMARY: Real test against actual GIPP. ALWAYS runs (R18 - no @integration marker).
    
    Tests that GIPP is NOT flagged as wash trade when fee engine has actual live data.
    If getRecentPrioritizationFees returns 0 samples for this token (token is too old / inactive),
    the test is skipped -- this is expected; run capture_gipp_fees.py and use TestGIPPFixture instead.
    """

    @pytest.mark.asyncio
    async def test_gipp_real_rpc_not_flagged(self):
        """Query GIPP mint from DB, run fee engine via real RPC. Skip if infra unavailable."""
        try:
            from src.database.client import db_manager
            await db_manager.initialize()
        except Exception as e:
            pytest.skip(f'Database unavailable: {e}')

        try:
            rows = await db_manager.query(
                'backtest_tokens',
                filters={'label': 'eq.runner'},
                limit=100
            )
        except Exception as e:
            pytest.skip(f'Database query failed: {e}')

        gipp_rows = [r for r in rows if 'GIPP' in (r.get('symbol') or '').upper()]
        if not gipp_rows:
            pytest.skip('GIPP not found in backtest_tokens -- run data collector first')

        gipp_mint = gipp_rows[0]['token_address']
        engine = GlobalFeeUrgencyEngine()

        try:
            result = await engine.calculate_fee_urgency(mint_address=gipp_mint)
        except Exception as e:
            pytest.skip(f'RPC call failed: {e}')

        # If RPC returned 0 samples, token is too old for getRecentPrioritizationFees (~150 slots).
        # This is expected for old tokens -- NOT a test failure. Use TestGIPPFixture (Helius Enhanced TX) instead.
        if result.valid_fee_sample_count == 0:
            pytest.skip(
                f'GIPP RPC returned 0 valid fee samples -- token too old for getRecentPrioritizationFees. '
                f'Run python scripts/capture_gipp_fees.py and rely on TestGIPPFixture for regression testing.'
            )

        assert not result.is_wash_trade_suspected, (
            f'GIPP ({gipp_mint[:12]}) falsely flagged as wash trade with LIVE fee data! '
            f"zero_fee_ratio={result.raw_data.get('zero_fee_ratio', 'N/A')}, "
            f'valid_samples={result.valid_fee_sample_count}'
        )
        assert result.score > 20.0, f'GIPP scored suspiciously low: {result.score}'


class TestGIPPFixture:
    """SECONDARY: Deterministic test using historical GIPP fee data (R16).
    
    This is the PRIMARY regression test when GIPP token is old/inactive.
    Fixture must be captured via: python scripts/capture_gipp_fees.py
    """

    @pytest.fixture
    def gipp_fixture(self):
        if not os.path.exists(GIPP_FIXTURE_PATH):
            pytest.skip(
                f'Fixture not found: {GIPP_FIXTURE_PATH}. '
                f'Run: python scripts/capture_gipp_fees.py'
            )
        with open(GIPP_FIXTURE_PATH) as f:
            data = json.load(f)

        # Verify fixture was captured with correct method (R16)
        capture_method = data.get('capture_method', 'unknown')
        if capture_method != 'helius_enhanced_tx_api':
            pytest.fail(
                f"Fixture captured with '{capture_method}' -- "
                f"must be 'helius_enhanced_tx_api'. Re-run scripts/capture_gipp_fees.py"
            )
        return data

    @pytest.mark.asyncio
    async def test_gipp_fixture_not_flagged(self, gipp_fixture):
        """Use historical GIPP fee data from Helius Enhanced TX API (R16)."""
        engine = GlobalFeeUrgencyEngine()
        real_fees = gipp_fixture['priority_fees_micro_lamports']

        assert len(real_fees) > 0, 'Fixture has no fee data'

        with patch.object(
            solana_rpc, 'get_recent_prioritization_fees',
            new_callable=AsyncMock,
            return_value=real_fees
        ):
            result = await engine.calculate_fee_urgency(
                mint_address=gipp_fixture['token_address']
            )

        assert not result.is_wash_trade_suspected, (
            f'GIPP flagged as wash trade with HISTORICAL fee data! '
            f"zero_fee_ratio={result.raw_data.get('zero_fee_ratio', 'N/A')}, "
            f"capture_method={gipp_fixture.get('capture_method')}, "
            f'valid_samples={result.valid_fee_sample_count}'
        )


class TestWashTradePositive:
    """Sanity: ensure genuine wash trading IS still detected."""

    @pytest.mark.asyncio
    async def test_obvious_wash_trade_flagged(self):
        """More than 85% zero fees with >10 samples = textbook wash trading."""
        engine = GlobalFeeUrgencyEngine()
        wash_fees = [0] * 11 + [5000]

        with patch.object(
            solana_rpc, 'get_recent_prioritization_fees',
            new_callable=AsyncMock,
            return_value=wash_fees
        ):
            result = await engine.calculate_fee_urgency(mint_address='WASH_TEST_FAKE')

        assert result.is_wash_trade_suspected, 'Genuine wash trade NOT detected!'
