import asyncio
from typing import Optional, Callable, Awaitable
from src.ingestion.schemas import RawTokenEvent
from src.filters.schemas import SafetyCheckResult
from src.filters.pump_safety import pump_safety_filter
from src.filters.raydium_safety import raydium_safety_filter
from src.filters.bundling import bundling_engine, BundlingResult
from src.opportunity.scorer import opportunity_scorer, OpportunityScoreResult
from src.database.client import db_manager
from src.database.models import FilterResultModel, WalletRelationshipModel
from src.utils.logger import logger


class FilterPipeline:
    """Async filter execution pipeline with DB persistence and result broadcasting."""

    def __init__(self, on_result_callback: Optional[Callable[[SafetyCheckResult, RawTokenEvent], Awaitable[None]]] = None):
        self.on_result_callback = on_result_callback

    async def process_token(self, event: RawTokenEvent) -> SafetyCheckResult:
        """Evaluates a raw token event against safety filters based on its venue."""
        logger.info(f"🔍 Analyzing Safety Filters for {event.symbol} ({event.token_address[:8]}...) [{event.launch_venue.upper()}]")

        # --- Phase 1: Hard Safety & Instant Scalp Filters ---
        if event.launch_venue == "pump_fun":
            result = await pump_safety_filter.evaluate(event)
        elif event.launch_venue == "raydium":
            result = await raydium_safety_filter.evaluate(event)
        else:
            # Default fallback evaluation
            result = await pump_safety_filter.evaluate(event)

        # --- Phase 2: Bundling & 2-Hop Funding Graph Engine ---
        # Run bundling & funding graph analysis if passed preliminary safety
        candidate_wallets: list[str] = []
        if result.filter_pass:
            logger.info(f"🕸️ Running Bundling & 2-Hop Graph Analysis on {event.symbol} ({event.token_address[:8]}...)...")
            bundling_res: BundlingResult = await bundling_engine.evaluate_token_bundling(
                mint_address=event.token_address,
                total_supply=event.total_supply,
                deployer_address=event.deployer_wallet_address,
                deployer_initial_buy=event.initial_buy_amount
            )

            result.sniper_bundle_pct = bundling_res.sniper_bundle_pct
            result.top10_holder_pct = bundling_res.top10_holder_pct
            result.raw_check_data["bundling_details"] = bundling_res.raw_cluster_data

            # Include all analyzed candidate wallets (clean early buyers + cluster members) for Smart Money matching
            if bundling_res.analyzed_wallets:
                candidate_wallets.extend(bundling_res.analyzed_wallets)

            # Persist detected wallet relationships
            if bundling_res.relationships:
                rel_models = [
                    WalletRelationshipModel(
                        wallet_a=r["wallet_a"],
                        wallet_b=r["wallet_b"],
                        relationship_type=r["relationship_type"],
                        hop_distance=r.get("hop_distance", 1),
                        shared_funding_sol=r.get("shared_funding_sol", 0.0),
                        confidence_score=r.get("confidence_score", 0.0)
                    )
                    for r in bundling_res.relationships
                ]
                await db_manager.batch_insert_relationships(rel_models)

            # Check if bundle exceeds maximum allowed threshold
            if bundling_res.is_bundle_risk:
                result.filter_pass = False
                bundle_reason = (
                    f"Bundle Monopoly Risk: {bundling_res.sniper_bundle_pct:.1f}% supply controlled "
                    f"by Sybil cluster of {bundling_res.max_cluster_size} wallets"
                )
                result.rejection_reason = (
                    f"{result.rejection_reason} | {bundle_reason}"
                    if result.rejection_reason else bundle_reason
                )

        # --- Phase 3: Enqueue for Stage 2 Delayed Evaluation (T+2 min) [Fase B] ---
        if result.filter_pass:
            try:
                from src.paper_trading.delayed_evaluator import delayed_evaluator
                await delayed_evaluator.enqueue(event, result)
                logger.info(f"⏱️  [Stage 1] {event.symbol} passed safety → enqueued for T+2 scoring")
            except Exception as de_err:
                logger.warning(f"Failed to enqueue {event.symbol} to delayed_evaluator: {de_err}")

        # 1. Update Token status in Database
        new_status = "PASSED_SAFETY" if result.filter_pass else "REJECTED"
        await db_manager.update_token_status(event.token_address, new_status)

        # 2. Insert Filter Result record in Database
        filter_record = FilterResultModel(
            token_address=result.token_address,
            checked_at=result.checked_at,
            mint_authority_renounced=result.mint_authority_renounced,
            freeze_authority_renounced=result.freeze_authority_renounced,
            lp_locked_or_burned=result.lp_locked_or_burned,
            lp_lock_pct=result.lp_lock_pct,
            top10_holder_pct=result.top10_holder_pct,
            honeypot_check_passed=result.honeypot_check_passed,
            dev_holding_pct=result.dev_holding_pct,
            sniper_bundle_pct=result.sniper_bundle_pct,
            instant_scalp_flags_count=result.instant_scalp_flags_count,
            filter_pass=result.filter_pass,
            rejection_reason=result.rejection_reason,
            raw_check_data=result.raw_check_data
        )
        await db_manager.insert_filter_result(filter_record)

        # 3. Log outcome visually
        if result.filter_pass:
            logger.info(
                f"✅ [bold green]PASSED SAFETY (Stage 1)[/bold green]: {event.symbol} | "
                f"Dev Buy: {result.dev_holding_pct:.1f}% | Top10: {result.top10_holder_pct:.1f}% | Venue: {event.launch_venue} | Queued for T+2 Scoring"
            )
        else:
            logger.warning(
                f"❌ [bold red]REJECTED[/bold red]: {event.symbol} | "
                f"Reason: {result.rejection_reason}"
            )

        # 4. Forward to downstream callback (e.g. Paper Trading / Telegram Bot)
        if self.on_result_callback:
            try:
                await self.on_result_callback(result, event)
            except Exception as cb_err:
                logger.error(f"Error in filter result callback: {cb_err}")

        # NOTE: Signal recording (paper_signals table) happens in Stage 2 (delayed_evaluator._process_token)
        # after the T+2 wait — NOT here in Stage 1. This prevents double-recording every token.

        return result


filter_pipeline = FilterPipeline()
