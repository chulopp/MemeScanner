import asyncio
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field

from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.database.models import MetricSnapshotModel
from src.database.client import db_manager
from src.opportunity.vol_velocity import volume_velocity_engine, VolumeVelocityResult
from src.opportunity.smart_money import smart_money_engine, SmartMoneyMatchResult
from src.opportunity.global_fee import global_fee_engine, GlobalFeeResult
from src.utils.logger import logger


class OpportunityScoreResult(BaseModel):
    token_address: str
    opportunity_score: float = 0.0
    score_vol_velocity: Optional[float] = None
    score_smart_money: Optional[float] = None
    score_global_fee: Optional[float] = None
    score_holder_curve: Optional[float] = None
    score_social_meta: Optional[float] = None
    weights_used: dict[str, float] = Field(default_factory=dict)
    active_components: list[str] = Field(default_factory=list)
    breakdown: dict[str, Any] = Field(default_factory=dict)
    metric_snapshot: Optional[MetricSnapshotModel] = None


class OpportunityScorer:
    """
    Multi-Factor Opportunity Scoring Engine [Fase 3].
    Formula [HIPOTESIS_AWAL]:
    Score = (0.35 * VolVelocity) + (0.30 * SmartMoney) + (0.15 * GlobalFee) + (0.10 * HolderCurve) + (0.10 * SocialMeta)
    
    Dynamically redistributes weights among active components if a component is unavailable/failed.
    """

    async def score_token(
        self,
        event: RawTokenEvent,
        candidate_wallets: Optional[list[str]] = None
    ) -> OpportunityScoreResult:
        """
        Executes concurrent opportunity scoring across all multi-factor engines.
        """
        token_addr = event.token_address
        wallets_to_check = candidate_wallets or []
        if event.deployer_wallet_address and event.deployer_wallet_address not in wallets_to_check:
            wallets_to_check.append(event.deployer_wallet_address)

        # Run scoring components concurrently
        vol_task = volume_velocity_engine.calculate_velocity(
            mint_address=token_addr,
            initial_buy_sol=event.initial_sol_liquidity
        )
        smart_task = smart_money_engine.evaluate_token_smart_money(
            candidate_wallet_addresses=wallets_to_check
        )
        fee_task = global_fee_engine.calculate_fee_urgency()

        vol_res, smart_res, fee_res = await asyncio.gather(
            vol_task, smart_task, fee_task, return_exceptions=True
        )

        # Base hypothesis weights [HIPOTESIS_AWAL]
        base_weights = {
            "vol_velocity": settings.score_w_vol_velocity,
            "smart_money": settings.score_w_smart_money,
            "global_fee": settings.score_w_global_fee,
            "holder_curve": settings.score_w_holder_curve,
            "social_meta": settings.score_w_social_meta
        }

        component_scores: dict[str, Optional[float]] = {
            "vol_velocity": None,
            "smart_money": None,
            "global_fee": None,
            "holder_curve": None,  # Placeholder for future phase
            "social_meta": None    # Placeholder for future phase
        }

        breakdown: dict[str, Any] = {}

        # 1. Volume Velocity
        if isinstance(vol_res, VolumeVelocityResult) and vol_res.is_successful:
            component_scores["vol_velocity"] = vol_res.score
            breakdown["vol_velocity"] = {
                "score": vol_res.score,
                "buy_count": vol_res.buy_count,
                "sell_count": vol_res.sell_count,
                "net_buy_pressure_ratio": vol_res.net_buy_pressure_ratio,
                "buy_vol_sol": vol_res.buy_volume_sol
            }
        else:
            logger.debug(f"Volume velocity unavailable for {token_addr[:8]}: {vol_res}")

        # 2. Smart Money
        if isinstance(smart_res, SmartMoneyMatchResult) and smart_res.is_successful:
            component_scores["smart_money"] = smart_res.score
            breakdown["smart_money"] = {
                "score": smart_res.score,
                "matched_count": smart_res.matched_wallets_count,
                "matched_wallets": smart_res.matched_wallets,
                "total_tracked": smart_res.total_tracked_wallets
            }
        else:
            logger.debug(f"Smart money match unavailable for {token_addr[:8]}: {smart_res}")

        # 3. Global Fee Urgency
        if isinstance(fee_res, GlobalFeeResult) and fee_res.is_successful:
            component_scores["global_fee"] = fee_res.score
            breakdown["global_fee"] = {
                "score": fee_res.score,
                "median_fee": fee_res.median_fee_micro_lamports,
                "max_fee": fee_res.max_fee_micro_lamports,
                "is_wash_trade_suspected": fee_res.is_wash_trade_suspected
            }
        else:
            logger.debug(f"Global fee urgency unavailable for {token_addr[:8]}: {fee_res}")

        # Active components with valid scores
        active_comps = [k for k, v in component_scores.items() if v is not None]
        total_active_base_weight = sum(base_weights[c] for c in active_comps)

        # Dynamic weight redistribution
        effective_weights: dict[str, float] = {}
        weighted_score_sum = 0.0

        if total_active_base_weight > 0:
            for comp in active_comps:
                score_val = component_scores[comp]
                if score_val is not None:
                    # Normalized effective weight summing to 1.0
                    eff_w = base_weights[comp] / total_active_base_weight
                    effective_weights[comp] = round(eff_w, 4)
                    weighted_score_sum += eff_w * score_val

        final_opportunity_score = round(min(max(weighted_score_sum, 0.0), 100.0), 2)

        # Build snapshot model for database persistence
        snapshot = MetricSnapshotModel(
            token_address=token_addr,
            snapshot_at=datetime.utcnow(),
            opportunity_score=final_opportunity_score,
            score_vol_velocity=component_scores["vol_velocity"],
            score_smart_money=component_scores["smart_money"],
            score_global_fee=component_scores["global_fee"],
            score_holder_curve=component_scores["holder_curve"],
            score_social_meta=component_scores["social_meta"],
            market_cap_usd=0.0,
            liquidity_usd=event.initial_sol_liquidity * 180.0 if event.initial_sol_liquidity else 0.0,
            volume_5m_usd=(vol_res.buy_volume_sol + vol_res.sell_volume_sol) * 180.0 if isinstance(vol_res, VolumeVelocityResult) else 0.0,
            buy_tx_count_5m=vol_res.buy_count if isinstance(vol_res, VolumeVelocityResult) else 0,
            sell_tx_count_5m=vol_res.sell_count if isinstance(vol_res, VolumeVelocityResult) else 0,
            net_buy_pressure_ratio=vol_res.net_buy_pressure_ratio if isinstance(vol_res, VolumeVelocityResult) else 0.0,
            global_priority_fees_sol=fee_res.median_fee_micro_lamports / 1_000_000_000.0 if isinstance(fee_res, GlobalFeeResult) else 0.0,
            bonding_curve_pct=0.0,
            unique_holders_count=len(wallets_to_check),
            weights_used=effective_weights,
            active_components=active_comps,
            raw_metrics=breakdown
        )

        logger.info(
            f"🎯 [bold magenta]Opportunity Score[/bold magenta]: {event.symbol} -> "
            f"[bold yellow]{final_opportunity_score:.1f}/100[/bold yellow] | "
            f"Vol: {component_scores['vol_velocity'] or 0:.0f} | "
            f"SmartMoney: {component_scores['smart_money'] or 0:.0f} | "
            f"Fee: {component_scores['global_fee'] or 0:.0f}"
        )

        return OpportunityScoreResult(
            token_address=token_addr,
            opportunity_score=final_opportunity_score,
            score_vol_velocity=component_scores["vol_velocity"],
            score_smart_money=component_scores["smart_money"],
            score_global_fee=component_scores["global_fee"],
            score_holder_curve=component_scores["holder_curve"],
            score_social_meta=component_scores["social_meta"],
            weights_used=effective_weights,
            active_components=active_comps,
            breakdown=breakdown,
            metric_snapshot=snapshot
        )


opportunity_scorer = OpportunityScorer()
