"""
Tests — Fase 3: Holder Curve & Social Meta Engines
Unit tests verifying bonding curve progress, unique holder distribution,
social meta extraction, DexScreener paid & boosts status, and full 5-component opportunity scoring.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from src.ingestion.schemas import RawTokenEvent
from src.opportunity.holder_curve import HolderCurveEngine, HolderCurveResult
from src.opportunity.social_meta import SocialMetaEngine, SocialMetaResult
from src.opportunity.vol_velocity import VolumeVelocityResult
from src.opportunity.smart_money import SmartMoneyMatchResult
from src.opportunity.global_fee import GlobalFeeResult
from src.opportunity.scorer import OpportunityScorer, OpportunityScoreResult

# Valid base58 Solana token addresses (length 32-44)
VALID_MINT_1 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
VALID_MINT_2 = "So11111111111111111111111111111111111111112"
VALID_MINT_3 = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"
VALID_MINT_4 = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
VALID_MINT_5 = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


# ============================================================
# Holder Curve Engine Tests
# ============================================================

@pytest.mark.asyncio
async def test_holder_curve_pump_fun_progression_scoring():
    """Verify bonding curve progress calculation on Pump.fun."""
    engine = HolderCurveEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT_1,
        symbol="PUMP",
        name="Pump Token",
        launch_venue="pump_fun",
        raw_payload={"vSolInBondingCurve": 85.0}  # ~64.7% progress
    )

    with patch("src.opportunity.holder_curve.solana_rpc.get_token_largest_accounts", AsyncMock(return_value=[])):
        res = await engine.evaluate_holder_curve(event, candidate_wallets=["W1", "W2", "W3", "W4", "W5", "W6", "W7"])
        assert res.is_successful is True
        assert res.bonding_curve_pct > 50.0
        # 60 pts from holders (7 holders) + 90 pts from bonding curve (>50%) = (0.5*90) + (0.5*60) = 75.0
        assert res.score == pytest.approx(75.0, rel=1e-2)


@pytest.mark.asyncio
async def test_holder_curve_raydium_auto_100_progress():
    """Tokens launched on Raydium should have 100% bonding progress by default."""
    engine = HolderCurveEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT_2,
        symbol="RAYD",
        name="Raydium Pool Token",
        launch_venue="raydium"
    )

    # Mock 25 on-chain holders
    mock_holders = [{"address": f"acc_{i}", "uiAmount": 1000.0} for i in range(25)]
    with patch("src.opportunity.holder_curve.solana_rpc.get_token_largest_accounts", AsyncMock(return_value=mock_holders)):
        res = await engine.evaluate_holder_curve(event)
        assert res.bonding_curve_pct == 100.0
        assert res.unique_holders_count == 25
        # 100 pts bonding (100%) + 85 pts holders (25 holders) -> (0.5*100) + (0.5*85) = 92.5
        assert res.score == pytest.approx(92.5, rel=1e-2)


@pytest.mark.asyncio
async def test_holder_curve_single_holder_dev_penalty():
    """Tokens with only 1 holder (dev) receive low score."""
    engine = HolderCurveEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT_3,
        symbol="SOLO",
        name="Solo Token",
        launch_venue="pump_fun",
        initial_sol_liquidity=1.0  # ~1.1% progress
    )

    with patch("src.opportunity.holder_curve.solana_rpc.get_token_largest_accounts", AsyncMock(return_value=[])):
        with patch.object(engine, "_get_client") as mock_client_factory:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=MagicMock(status_code=404))
            mock_client_factory.return_value = mock_client

            res = await engine.evaluate_holder_curve(event)
            assert res.unique_holders_count == 1
            # 30 pts bonding (<15%) + 10 pts holders (1 holder) -> (0.5*30) + (0.5*10) = 20.0
            assert res.score <= 25.0


# ============================================================
# Social Meta Engine Tests
# ============================================================

@pytest.mark.asyncio
async def test_social_meta_full_socials_and_dex_paid():
    """Verify that a token with Twitter, Telegram, Website, and Dex Paid achieves max score."""
    engine = SocialMetaEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT_4,
        symbol="LEGIT",
        name="Legit Community Token",
        launch_venue="pump_fun",
        raw_payload={
            "twitter": "https://x.com/legit_coin",
            "telegram": "https://t.me/legit_coin",
            "website": "https://legitcoin.vip"
        }
    )

    mock_dex_resp = MagicMock()
    mock_dex_resp.status_code = 200
    mock_dex_resp.json.return_value = {
        "pairs": [{
            "info": {
                "header": "https://img.dexscreener.com/header.png",
                "icon": "https://img.dexscreener.com/icon.png",
                "socials": [
                    {"type": "twitter", "url": "https://x.com/legit_coin"},
                    {"type": "telegram", "url": "https://t.me/legit_coin"}
                ],
                "websites": [{"url": "https://legitcoin.vip"}]
            },
            "boosts": {"active": 5}
        }]
    }

    with patch.object(engine, "_get_client") as mock_client_factory:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_dex_resp)
        mock_client_factory.return_value = mock_client

        res = await engine.evaluate_social_meta(event, total_volume_sol=10.0)
        assert res.has_twitter is True
        assert res.has_telegram is True
        assert res.has_website is True
        assert res.dexscreener_paid is True
        assert res.dexscreener_boosted is True
        # 25 + 20 + 15 + 30 + 10 = 100.0
        assert res.score == 100.0


@pytest.mark.asyncio
async def test_social_meta_empty_socials_zero_score():
    """Verify that an anonymous token with 0 socials gets 0 score."""
    engine = SocialMetaEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT_5,
        symbol="GHOST",
        name="Ghost Token",
        launch_venue="pump_fun",
        raw_payload={}
    )

    with patch.object(engine, "_get_client") as mock_client_factory:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=MagicMock(status_code=404))
        mock_client_factory.return_value = mock_client

        res = await engine.evaluate_social_meta(event)
        assert res.has_twitter is False
        assert res.has_telegram is False
        assert res.has_website is False
        assert res.dexscreener_paid is False
        assert res.score == 0.0


@pytest.mark.asyncio
async def test_social_meta_suspicious_boost_penalty():
    """Verify penalty when artificial boosts are detected with low volume."""
    engine = SocialMetaEngine()

    event = RawTokenEvent(
        token_address=VALID_MINT_1,
        symbol="FAKEMEME",
        name="Fake Meme",
        launch_venue="pump_fun",
        initial_sol_liquidity=0.5,
        raw_payload={"twitter": "https://x.com/fake"}
    )

    mock_dex_resp = MagicMock()
    mock_dex_resp.status_code = 200
    mock_dex_resp.json.return_value = {
        "pairs": [{
            "info": {},
            "boosts": {"active": 100}  # Very high boosts with only 0.5 SOL volume
        }]
    }

    with patch.object(engine, "_get_client") as mock_client_factory:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_dex_resp)
        mock_client_factory.return_value = mock_client

        res = await engine.evaluate_social_meta(event, total_volume_sol=0.5)
        assert res.suspicious_artificial_boost is True
        # Twitter (+25) + Boost (+10) - Penalty (-20) = 15.0
        assert res.score == 15.0


# ============================================================
# Full Opportunity Scorer 5-Component Integration Test
# ============================================================

@pytest.mark.asyncio
async def test_opportunity_scorer_all_5_components_active():
    """
    Verify that OpportunityScorer executes all 5 components concurrently
    and applies PRD exact weighting: 35% vol, 30% smart money, 15% fee, 10% holder, 10% social.
    """
    scorer = OpportunityScorer()

    event = RawTokenEvent(
        token_address=VALID_MINT_1,
        symbol="FULL5",
        name="Full Five Component Token",
        launch_venue="pump_fun",
        initial_buy_amount=10_000_000.0,
        total_supply=1_000_000_000.0
    )

    mock_vol = VolumeVelocityResult(score=100.0, buy_count=20, sell_count=2, buy_volume_sol=15.0, sell_volume_sol=1.0, net_buy_pressure_ratio=15.0, is_successful=True)
    mock_smart = SmartMoneyMatchResult(score=100.0, matched_wallets_count=3, matched_wallets=["SM1", "SM2", "SM3"], total_tracked_wallets=25, is_successful=True)
    mock_fee = GlobalFeeResult(score=100.0, median_fee_micro_lamports=50000, max_fee_micro_lamports=100000, p90_fee_micro_lamports=80000, valid_fee_sample_count=50, is_successful=True)
    mock_holder = HolderCurveResult(score=100.0, bonding_curve_pct=90.0, unique_holders_count=50, is_successful=True)
    mock_social = SocialMetaResult(score=100.0, has_twitter=True, has_telegram=True, has_website=True, dexscreener_paid=True, is_successful=True)

    with patch("src.opportunity.scorer.volume_velocity_engine.calculate_velocity", AsyncMock(return_value=mock_vol)):
        with patch("src.opportunity.scorer.smart_money_engine.evaluate_token_smart_money", AsyncMock(return_value=mock_smart)):
            with patch("src.opportunity.scorer.global_fee_engine.calculate_fee_urgency", AsyncMock(return_value=mock_fee)):
                with patch("src.opportunity.scorer.holder_curve_engine.evaluate_holder_curve", AsyncMock(return_value=mock_holder)):
                    with patch("src.opportunity.scorer.social_meta_engine.evaluate_social_meta", AsyncMock(return_value=mock_social)):
                        res: OpportunityScoreResult = await scorer.score_token(event)

                        assert res.opportunity_score == 100.0
                        assert set(res.active_components) == {
                            "vol_velocity", "smart_money", "global_fee", "holder_curve", "social_meta"
                        }
                        assert res.weights_used["vol_velocity"] == 0.35
                        assert res.weights_used["smart_money"] == 0.30
                        assert res.weights_used["global_fee"] == 0.15
                        assert res.weights_used["holder_curve"] == 0.10
                        assert res.weights_used["social_meta"] == 0.10
                        assert res.score_holder_curve == 100.0
                        assert res.score_social_meta == 100.0
                        assert res.metric_snapshot is not None
                        assert res.metric_snapshot.bonding_curve_pct == 90.0
                        assert res.metric_snapshot.unique_holders_count == 50
