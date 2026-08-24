import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock, MagicMock

from src.ingestion.schemas import RawTokenEvent
from src.database.models import SmartMoneyProfileModel
from src.opportunity.vol_velocity import VolumeVelocityEngine, VolumeVelocityResult
from src.opportunity.smart_money import SmartMoneyEngine, SmartMoneyMatchResult
from src.opportunity.global_fee import GlobalFeeUrgencyEngine, GlobalFeeResult
from src.opportunity.holder_curve import HolderCurveResult
from src.opportunity.social_meta import SocialMetaResult
from src.opportunity.scorer import OpportunityScorer, OpportunityScoreResult
from src.filters.pipeline import filter_pipeline
from src.filters.schemas import SafetyCheckResult
from src.filters.bundling import BundlingResult



@pytest.mark.asyncio
async def test_vol_velocity_high_buy_pressure():
    """Verify that high buy/sell ratio yields high Volume Velocity score."""
    engine = VolumeVelocityEngine()

    mock_txs = [
        {
            "timestamp": 1000000000 + i,
            "feePayer": f"BUYER_{i}",
            "tokenTransfers": [{"mint": "MINT123", "toUserAccount": f"BUYER_{i}", "fromUserAccount": "POOL"}],
            "nativeTransfers": [{"fromUserAccount": f"BUYER_{i}", "toUserAccount": "POOL", "amount": 1_000_000_000}]
        }
        for i in range(10)
    ]

    with patch("src.opportunity.vol_velocity.settings.helius_api_key", "test_mock_api_key"):
        with patch.object(engine, "_get_client") as mock_client_getter:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_txs
            mock_client.get.return_value = mock_response
            mock_client_getter.return_value = mock_client

            with patch("time.time", return_value=1000000050):
                res = await engine.calculate_velocity("MINT123", window_seconds=300)

                assert res.is_successful is True
                assert res.buy_count >= 10
                assert res.sell_count == 0
                assert res.score == 100.0
                assert res.provider_used == "helius"


@pytest.mark.asyncio
async def test_vol_velocity_sell_penalty():
    """Verify that when sells outnumber buys, score receives a 50% dampening penalty."""
    engine = VolumeVelocityEngine()

    # 1 buy, 8 sells
    mock_txs = [
        {
            "timestamp": 1000000000,
            "feePayer": "BUYER_0",
            "tokenTransfers": [{"mint": "MINT123", "toUserAccount": "BUYER_0", "fromUserAccount": "POOL"}],
            "nativeTransfers": [{"fromUserAccount": "BUYER_0", "toUserAccount": "POOL", "amount": 500_000_000}]
        }
    ] + [
        {
            "timestamp": 1000000000 + i + 1,
            "feePayer": f"SELLER_{i}",
            "tokenTransfers": [{"mint": "MINT123", "fromUserAccount": f"SELLER_{i}", "toUserAccount": "POOL"}],
            "nativeTransfers": [{"fromUserAccount": "POOL", "toUserAccount": f"SELLER_{i}", "amount": 500_000_000}]
        }
        for i in range(8)
    ]

    with patch("src.opportunity.vol_velocity.settings.helius_api_key", "test_mock_api_key"):
        with patch.object(engine, "_get_client") as mock_client_getter:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_txs
            mock_client.get.return_value = mock_response
            mock_client_getter.return_value = mock_client

            with patch("time.time", return_value=1000000050):
                res = await engine.calculate_velocity("MINT123", window_seconds=300)

                assert res.sell_count >= 8
                assert res.buy_count == 1
                assert res.score < 50.0  # Penalized
                assert res.provider_used == "helius"


@pytest.mark.asyncio
async def test_smart_money_detection_scoring():
    """Verify smart money match scoring tiers (0 -> 0, 1 -> 40, 2 -> 75, 3+ -> 100)."""
    engine = SmartMoneyEngine()
    engine._cached_wallets = {"SMART_1", "SMART_2", "SMART_3"}
    engine._last_cache_refresh = datetime.utcnow()

    # 0 matches
    res0 = await engine.evaluate_token_smart_money(["REGULAR_USER_A", "REGULAR_USER_B"])
    assert res0.matched_wallets_count == 0
    assert res0.score == 0.0

    # 1 match
    res1 = await engine.evaluate_token_smart_money(["SMART_1", "REGULAR_USER_A"])
    assert res1.matched_wallets_count == 1
    assert res1.score == 40.0

    # 2 matches
    res2 = await engine.evaluate_token_smart_money(["SMART_1", "SMART_2", "REGULAR_USER_A"])
    assert res2.matched_wallets_count == 2
    assert res2.score == 75.0

    # 3 matches
    res3 = await engine.evaluate_token_smart_money(["SMART_1", "SMART_2", "SMART_3"])
    assert res3.matched_wallets_count == 3
    assert res3.score == 100.0


@pytest.mark.asyncio
async def test_smart_money_promotion_and_demotion():
    """Verify auto-promotion and inactivity demotion logic."""
    engine = SmartMoneyEngine()

    # Case 1: Meets promotion criteria
    profile_promote = SmartMoneyProfileModel(
        wallet_address="WALLET_PROMOTE",
        tier="SEED",
        total_trades_recorded=25,
        net_realized_profit_sol=20.5,
        profit_factor=2.1,
        last_active_at=datetime.utcnow()
    )
    res_promote = await engine.evaluate_promotion_and_demotion(profile_promote)
    assert res_promote.tier == "ACTIVE"
    assert res_promote.is_active is True

    # Case 2: Inactive for > 14 days -> Demoted
    profile_demote = SmartMoneyProfileModel(
        wallet_address="WALLET_DEMOTE",
        tier="ACTIVE",
        total_trades_recorded=30,
        net_realized_profit_sol=30.0,
        profit_factor=2.0,
        last_active_at=datetime.utcnow() - timedelta(days=16)
    )
    res_demote = await engine.evaluate_promotion_and_demotion(profile_demote)
    assert res_demote.tier == "DEMOTED"
    assert res_demote.is_active is False


@pytest.mark.asyncio
async def test_global_fee_urgency_wash_trade_filtering():
    """Verify that priority fee urgency detects competitive priority bidding and penalizes wash trades."""
    engine = GlobalFeeUrgencyEngine()

    # Case 1: High priority fee spike
    high_fees = [80000, 95000, 120000, 150000, 60000]
    with patch("src.opportunity.global_fee.solana_rpc.get_recent_prioritization_fees", AsyncMock(return_value=high_fees)):
        res_high = await engine.calculate_fee_urgency()
        assert res_high.is_successful is True
        assert res_high.score >= 80.0
        assert res_high.is_wash_trade_suspected is False

    # Case 2: All 0 fee wash trade
    zero_fees = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    with patch("src.opportunity.global_fee.solana_rpc.get_recent_prioritization_fees", AsyncMock(return_value=zero_fees)):
        res_zero = await engine.calculate_fee_urgency()
        assert res_zero.is_wash_trade_suspected is True
        assert res_zero.score <= 10.0


@pytest.mark.asyncio
async def test_opportunity_scorer_weight_redistribution():
    """Verify dynamic weight redistribution when some components are active."""
    scorer = OpportunityScorer()

    event = RawTokenEvent(
        token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        symbol="OPPCOIN",
        name="Opportunity Coin",
        deployer_wallet_address="2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
        launch_venue="pump_fun",
        initial_buy_amount=10_000_000.0,
        total_supply=1_000_000_000.0
    )

    mock_vol = VolumeVelocityResult(
        score=80.0, buy_count=10, sell_count=2, buy_volume_sol=5.0,
        sell_volume_sol=1.0, net_buy_pressure_ratio=5.0, is_successful=True
    )
    mock_smart = SmartMoneyMatchResult(
        score=40.0, matched_wallets_count=1, matched_wallets=["SMART_1"],
        total_tracked_wallets=25, is_successful=True
    )
    mock_fee = GlobalFeeResult(
        score=60.0, median_fee_micro_lamports=25000, max_fee_micro_lamports=50000,
        p90_fee_micro_lamports=40000, valid_fee_sample_count=50, is_successful=True
    )

    mock_holder_failed = HolderCurveResult(score=0.0, bonding_curve_pct=0.0, unique_holders_count=0, is_successful=False)
    mock_social_failed = SocialMetaResult(score=0.0, is_successful=False)

    with patch("src.opportunity.scorer.volume_velocity_engine.calculate_velocity", AsyncMock(return_value=mock_vol)):
        with patch("src.opportunity.scorer.smart_money_engine.evaluate_token_smart_money", AsyncMock(return_value=mock_smart)):
            with patch("src.opportunity.scorer.global_fee_engine.calculate_fee_urgency", AsyncMock(return_value=mock_fee)):
                with patch("src.opportunity.scorer.holder_curve_engine.evaluate_holder_curve", AsyncMock(return_value=mock_holder_failed)):
                    with patch("src.opportunity.scorer.social_meta_engine.evaluate_social_meta", AsyncMock(return_value=mock_social_failed)):
                        res: OpportunityScoreResult = await scorer.score_token(event)

                        assert res.opportunity_score > 0.0
                        assert res.opportunity_score <= 100.0
                        assert set(res.active_components) == {"vol_velocity", "smart_money", "global_fee"}
                        # Check sum of effective weights == 1.0
                        assert round(sum(res.weights_used.values()), 2) == 1.0
                        assert res.metric_snapshot is not None
                        assert res.metric_snapshot.opportunity_score == res.opportunity_score



@pytest.mark.asyncio
async def test_pipeline_integration_phase3_opportunity_score():
    """Verify that FilterPipeline executes Phase 3 scoring on tokens that pass safety & bundling."""
    event = RawTokenEvent(
        token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        symbol="OPPCOIN",
        name="Opportunity Coin",
        deployer_wallet_address="2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
        launch_venue="pump_fun",
        initial_buy_amount=10_000_000.0,
        total_supply=1_000_000_000.0
    )

    mock_bundling_pass = BundlingResult(
        token_address=event.token_address,
        sniper_bundle_pct=10.0,
        is_bundle_risk=False,
        max_cluster_size=1
    )

    mock_opp_score = OpportunityScoreResult(
        token_address=event.token_address,
        opportunity_score=82.5,
        score_vol_velocity=85.0,
        score_smart_money=75.0,
        score_global_fee=80.0,
        weights_used={"vol_velocity": 0.4375, "smart_money": 0.375, "global_fee": 0.1875},
        active_components=["vol_velocity", "smart_money", "global_fee"],
        breakdown={"vol_velocity": {"score": 85.0}}
    )

    with patch("src.filters.pump_safety.instant_scalp_filter.evaluate", AsyncMock(return_value={"flags_count": 0})):
        with patch("src.filters.pipeline.bundling_engine.evaluate_token_bundling", AsyncMock(return_value=mock_bundling_pass)):
            with patch("src.filters.pipeline.opportunity_scorer.score_token", AsyncMock(return_value=mock_opp_score)):
                with patch("src.database.client.db_manager.insert_metric_snapshot", AsyncMock(return_value=True)):
                    result: SafetyCheckResult = await filter_pipeline.process_token(event)

                    assert result.filter_pass is True
                    assert result.opportunity_score == 82.5
                    assert "opportunity_breakdown" in result.raw_check_data


@pytest.mark.asyncio
async def test_vol_velocity_direction_precision():
    """Verify that swap direction parsing precisely detects buy vs sell and ignores unknown."""
    engine = VolumeVelocityEngine()
    mint = "MINT123"

    mock_txs = [
        # Buy: user receives target token in tokenTransfers, sends SOL
        {
            "timestamp": 1000000000,
            "feePayer": "USER_BUYER_1",
            "tokenTransfers": [{"mint": mint, "toUserAccount": "USER_BUYER_1", "fromUserAccount": "POOL_1"}],
            "nativeTransfers": [{"fromUserAccount": "USER_BUYER_1", "toUserAccount": "POOL_1", "amount": 1_000_000_000}]
        },
        # Sell: user sends target token in tokenTransfers, receives SOL
        {
            "timestamp": 1000000010,
            "feePayer": "USER_SELLER_1",
            "tokenTransfers": [{"mint": mint, "fromUserAccount": "USER_SELLER_1", "toUserAccount": "POOL_1"}],
            "nativeTransfers": [{"fromUserAccount": "POOL_1", "toUserAccount": "USER_SELLER_1", "amount": 1_000_000_000}]
        },
        # Unknown/Unrelated transfer: non-swap, should be ignored
        {
            "timestamp": 1000000020,
            "feePayer": "RANDOM_FEE_PAYER",
            "tokenTransfers": [{"mint": "OTHER_MINT", "toUserAccount": "USER_X"}],
            "nativeTransfers": []
        }
    ]

    with patch("src.opportunity.vol_velocity.settings.helius_api_key", "test_mock_api_key"):
        with patch.object(engine, "_get_client") as mock_client_getter:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = mock_txs
            mock_client.get.return_value = mock_response
            mock_client_getter.return_value = mock_client

            with patch("time.time", return_value=1000000050):
                res = await engine.calculate_velocity(mint, window_seconds=300)

                assert res.buy_count == 1
                assert res.sell_count == 1
                assert res.net_buy_pressure_ratio == 1.0
                assert res.provider_used == "helius"


@pytest.mark.asyncio
async def test_vol_velocity_rpc_signatures_tier():
    """Verify Tier 3 RPC signatures fallback when Helius and DexScreener are both unavailable."""
    engine = VolumeVelocityEngine()

    mock_signatures = [
        {"signature": f"sig_{i}", "blockTime": 1000000000 + i * 10}
        for i in range(10)
    ]

    with patch("src.opportunity.vol_velocity.settings.helius_api_key", None):
        with patch.object(engine, "_fetch_from_dexscreener", AsyncMock(return_value=None)):
            with patch("src.opportunity.vol_velocity.solana_rpc.get_signatures_for_address", AsyncMock(return_value=mock_signatures)):
                with patch("time.time", return_value=1000000050):
                    res = await engine.calculate_velocity("MINT_RPC_TEST", initial_buy_sol=1.5, window_seconds=300)

                    assert res.is_successful is True
                    assert res.provider_used == "solana_rpc"
                    assert res.buy_count >= 1
                    assert res.buy_volume_sol == 1.5
                    assert res.score > 0.0


@pytest.mark.asyncio
async def test_token_specific_prioritization_fee():
    """Verify that calculate_fee_urgency passes token mint address to solana_rpc."""
    engine = GlobalFeeUrgencyEngine()
    mint = "TOKEN_MINT_ABC"

    with patch("src.opportunity.global_fee.solana_rpc.get_recent_prioritization_fees", AsyncMock(return_value=[50000, 75000, 100000])) as mock_rpc_call:
        res = await engine.calculate_fee_urgency(mint_address=mint)

        mock_rpc_call.assert_called_once_with([mint])
        assert res.is_successful is True
        assert res.median_fee_micro_lamports == 75000.0


@pytest.mark.asyncio
async def test_sol_price_feed_caching_and_fallback():
    """Verify that price feed fetches, caches for 5 minutes, and falls back gracefully."""
    from src.utils.price_feed import PriceFeedClient

    client = PriceFeedClient(cache_ttl_seconds=300, default_sol_usd=180.0)

    # 1. Successful Jupiter response
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "data": {
            "So11111111111111111111111111111111111111112": {
                "price": "215.50"
            }
        }
    }

    with patch.object(client, "_get_client") as mock_http:
        http_inst = AsyncMock()
        http_inst.get.return_value = mock_resp
        mock_http.return_value = http_inst

        price = await client.get_sol_price_usd()
        assert price == 215.50

        # 2. Second call should hit cache without new HTTP request
        http_inst.get.reset_mock()
        cached_price = await client.get_sol_price_usd()
        assert cached_price == 215.50
        http_inst.get.assert_not_called()

    # 3. Fallback when network fails
    fresh_client = PriceFeedClient(default_sol_usd=180.0)
    with patch.object(fresh_client, "_get_client", side_effect=Exception("Network error")):
        fallback_price = await fresh_client.get_sol_price_usd()
        assert fallback_price == 180.0


@pytest.mark.asyncio
async def test_vol_velocity_dexscreener_fallback():
    """Verify that DexScreener fallback activates when Helius API is unavailable or missing key."""
    engine = VolumeVelocityEngine()

    mock_dex_resp = MagicMock()
    mock_dex_resp.status_code = 200
    mock_dex_resp.json.return_value = {
        "pairs": [
            {
                "chainId": "solana",
                "txns": {
                    "m5": {"buys": 20, "sells": 5}
                },
                "volume": {"m5": 1800.0}
            }
        ]
    }

    # Simulate empty/no Helius key
    with patch("src.opportunity.vol_velocity.settings.helius_api_key", None):
        with patch.object(engine, "_get_client") as mock_http:
            http_inst = AsyncMock()
            http_inst.get.return_value = mock_dex_resp
            mock_http.return_value = http_inst

            res = await engine.calculate_velocity("MINT_DEX_TEST", initial_buy_sol=0.0)

            assert res.is_successful is True
            assert res.provider_used == "dexscreener"
            assert res.buy_count == 20
            assert res.sell_count == 5
            assert res.net_buy_pressure_ratio == 4.0
            assert res.score == 80.0


@pytest.mark.asyncio
async def test_vol_velocity_all_providers_fail_graceful_degradation():
    """Verify that if all volume providers fail and no initial buy, engine sets is_successful=False."""
    engine = VolumeVelocityEngine()

    with patch("src.opportunity.vol_velocity.settings.helius_api_key", None):
        with patch.object(engine, "_fetch_from_dexscreener", AsyncMock(return_value=None)):
            with patch.object(engine, "_fetch_from_rpc_signatures", AsyncMock(return_value=None)):
                res = await engine.calculate_velocity("MINT_FAIL", initial_buy_sol=0.0)

                assert res.is_successful is False
                assert res.score == 0.0
                assert res.provider_used == "none"


@pytest.mark.asyncio
async def test_smart_money_empty_registry_weight_redistribution():
    """Verify that when smart money registry is empty, scorer redistributes weight to remaining active components."""
    scorer = OpportunityScorer()

    event = RawTokenEvent(
        token_address="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        symbol="OPPCOIN",
        name="Opportunity Coin",
        deployer_wallet_address="2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",
        launch_venue="pump_fun",
        initial_buy_amount=10_000_000.0,
        total_supply=1_000_000_000.0
    )

    mock_vol = VolumeVelocityResult(
        score=80.0, buy_count=10, sell_count=2, buy_volume_sol=5.0,
        sell_volume_sol=1.0, net_buy_pressure_ratio=5.0, is_successful=True
    )
    # Smart money returns is_successful=False because registry is empty
    mock_smart_empty = SmartMoneyMatchResult(
        score=0.0, matched_wallets_count=0, matched_wallets=[],
        total_tracked_wallets=0, is_successful=False
    )
    mock_fee = GlobalFeeResult(
        score=60.0, median_fee_micro_lamports=25000, max_fee_micro_lamports=50000,
        p90_fee_micro_lamports=40000, valid_fee_sample_count=50, is_successful=True
    )

    mock_holder_failed = HolderCurveResult(score=0.0, bonding_curve_pct=0.0, unique_holders_count=0, is_successful=False)
    mock_social_failed = SocialMetaResult(score=0.0, is_successful=False)

    with patch("src.opportunity.scorer.volume_velocity_engine.calculate_velocity", AsyncMock(return_value=mock_vol)):
        with patch("src.opportunity.scorer.smart_money_engine.evaluate_token_smart_money", AsyncMock(return_value=mock_smart_empty)):
            with patch("src.opportunity.scorer.global_fee_engine.calculate_fee_urgency", AsyncMock(return_value=mock_fee)):
                with patch("src.opportunity.scorer.holder_curve_engine.evaluate_holder_curve", AsyncMock(return_value=mock_holder_failed)):
                    with patch("src.opportunity.scorer.social_meta_engine.evaluate_social_meta", AsyncMock(return_value=mock_social_failed)):
                        res: OpportunityScoreResult = await scorer.score_token(event)

                        assert res.opportunity_score > 0.0
                        # Active components should only be vol_velocity and global_fee (smart_money, holder, social excluded cleanly)
                        assert set(res.active_components) == {"vol_velocity", "global_fee"}
                        assert "smart_money" not in res.active_components
                        # Base weights: vol=0.35, fee=0.15 (total active base weight = 0.50)
                        assert res.weights_used["vol_velocity"] == 0.70
                        assert res.weights_used["global_fee"] == 0.30

                # Normalized effective weights: vol = 0.35/0.50 = 0.70, fee = 0.15/0.50 = 0.30
                assert res.weights_used["vol_velocity"] == 0.70
                assert res.weights_used["global_fee"] == 0.30
                # Score = 0.70*80 + 0.30*60 = 56 + 18 = 74.0
                assert res.opportunity_score == 74.0
