import pytest
from unittest.mock import patch, AsyncMock
from src.filters.bundling import BundlingEngine, BundlingResult
from src.filters.funding_graph import FundingTraceNode, FundingHopInfo
from src.ingestion.schemas import RawTokenEvent
from src.filters.pipeline import filter_pipeline


@pytest.mark.asyncio
async def test_bundling_detection_high_cluster_rejected():
    """Verify that a token with Sybil cluster > 25% is marked as bundle risk."""
    engine = BundlingEngine()

    mock_nodes = [
        FundingTraceNode(
            wallet_address="W1111111111111111111111111111111111111111",
            token_holding_pct=15.0,
            hop1=FundingHopInfo(funder_address="SharedParentFunder", amount_sol=2.0)
        ),
        FundingTraceNode(
            wallet_address="W2222222222222222222222222222222222222222",
            token_holding_pct=15.0,
            hop1=FundingHopInfo(funder_address="SharedParentFunder", amount_sol=2.0)
        )
    ]

    with patch.object(engine, "extract_early_buyers_and_top_holders", AsyncMock(return_value={"W1": 150_000_000, "W2": 150_000_000})):
        with patch("src.filters.bundling.funding_tracer.trace_wallets_batch", AsyncMock(return_value=mock_nodes)):
            res: BundlingResult = await engine.evaluate_token_bundling(
                mint_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                total_supply=1_000_000_000.0
            )

            assert res.sniper_bundle_pct == 30.0
            assert res.is_bundle_risk is True
            assert res.max_cluster_size == 2


@pytest.mark.asyncio
async def test_bundling_detection_clean_distribution_passed():
    """Verify that independent wallets with < 25% cluster pass bundling filter."""
    engine = BundlingEngine()

    mock_nodes = [
        FundingTraceNode(
            wallet_address="W1111111111111111111111111111111111111111",
            token_holding_pct=5.0,
            hop1=FundingHopInfo(funder_address="IndependentParentA", amount_sol=1.0)
        ),
        FundingTraceNode(
            wallet_address="W2222222222222222222222222222222222222222",
            token_holding_pct=5.0,
            hop1=FundingHopInfo(funder_address="IndependentParentB", amount_sol=1.0)
        )
    ]

    with patch.object(engine, "extract_early_buyers_and_top_holders", AsyncMock(return_value={"W1": 50_000_000, "W2": 50_000_000})):
        with patch("src.filters.bundling.funding_tracer.trace_wallets_batch", AsyncMock(return_value=mock_nodes)):
            res: BundlingResult = await engine.evaluate_token_bundling(
                mint_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                total_supply=1_000_000_000.0
            )

            assert res.sniper_bundle_pct == 0.0
            assert res.is_bundle_risk is False


@pytest.mark.asyncio
async def test_pipeline_integration_with_bundling_filter():
    """Verify that FilterPipeline rejects tokens when bundling check fails."""
    event = RawTokenEvent(
        token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        symbol="BUNDLECOIN",
        name="Bundle Coin",
        deployer_wallet_address="2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
        launch_venue="pump_fun",
        initial_buy_amount=30_000_000.0,
        total_supply=1_000_000_000.0
    )

    mock_bundling_res = BundlingResult(
        token_address=event.token_address,
        sniper_bundle_pct=35.0,
        is_bundle_risk=True,
        max_cluster_size=3,
        relationships=[]
    )

    with patch("src.filters.pump_safety.instant_scalp_filter.evaluate", AsyncMock(return_value={"flags_count": 0})):
        with patch("src.filters.pipeline.bundling_engine.evaluate_token_bundling", AsyncMock(return_value=mock_bundling_res)):
            result = await filter_pipeline.process_token(event)

            assert result.filter_pass is False
            assert "Bundle Monopoly Risk" in (result.rejection_reason or "")
            assert result.sniper_bundle_pct == 35.0


@pytest.mark.asyncio
async def test_extract_early_buyers_resolves_ata_to_owner():
    """Verify that ATAs returned by getTokenLargestAccounts are resolved to their owner wallets."""
    engine = BundlingEngine()

    mock_largest = [
        {"address": "ATA_ACCOUNT_1", "uiAmount": 100_000.0},
        {"address": "ATA_ACCOUNT_2", "uiAmount": 200_000.0}
    ]

    async def mock_get_owner(ata):
        if ata == "ATA_ACCOUNT_1":
            return "OWNER_WALLET_1"
        elif ata == "ATA_ACCOUNT_2":
            return "OWNER_WALLET_2"
        return None

    with patch("src.filters.bundling.solana_rpc.get_token_largest_accounts", AsyncMock(return_value=mock_largest)):
        with patch("src.filters.bundling.solana_rpc.get_token_account_owner", AsyncMock(side_effect=mock_get_owner)):
            holdings = await engine.extract_early_buyers_and_top_holders(
                mint_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                total_supply=1_000_000_000.0
            )

            assert "OWNER_WALLET_1" in holdings
            assert holdings["OWNER_WALLET_1"] == 100_000.0
            assert "OWNER_WALLET_2" in holdings
            assert holdings["OWNER_WALLET_2"] == 200_000.0
            assert "ATA_ACCOUNT_1" not in holdings


@pytest.mark.asyncio
async def test_total_supply_zero_no_crash():
    """Verify that total_supply=0 is gracefully guarded and does not raise ZeroDivisionError."""
    engine = BundlingEngine()

    with patch.object(engine, "extract_early_buyers_and_top_holders", AsyncMock(return_value={})):
        res = await engine.evaluate_token_bundling(
            mint_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            total_supply=0.0
        )
        assert res.sniper_bundle_pct == 0.0
        assert res.is_bundle_risk is False


@pytest.mark.asyncio
async def test_deployer_fallback_uses_initial_buy():
    """Verify that deployer holding uses actual initial buy amount instead of fabricated 1%."""
    engine = BundlingEngine()

    with patch("src.filters.bundling.solana_rpc.get_token_largest_accounts", AsyncMock(return_value=[])):
        holdings = await engine.extract_early_buyers_and_top_holders(
            mint_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            total_supply=1_000_000_000.0,
            deployer_address="DEPLOYER_WALLET_XYZ",
            deployer_initial_buy=45_000_000.0
        )

        assert "DEPLOYER_WALLET_XYZ" in holdings
        assert holdings["DEPLOYER_WALLET_XYZ"] == 45_000_000.0
