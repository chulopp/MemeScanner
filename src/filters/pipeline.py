import asyncio
from typing import Optional, Callable, Awaitable
from src.ingestion.schemas import RawTokenEvent
from src.filters.schemas import SafetyCheckResult
from src.filters.pump_safety import pump_safety_filter
from src.filters.raydium_safety import raydium_safety_filter
from src.database.client import db_manager
from src.database.models import FilterResultModel
from src.utils.logger import logger


class FilterPipeline:
    """Async filter execution pipeline with DB persistence and result broadcasting."""

    def __init__(self, on_result_callback: Optional[Callable[[SafetyCheckResult, RawTokenEvent], Awaitable[None]]] = None):
        self.on_result_callback = on_result_callback

    async def process_token(self, event: RawTokenEvent) -> SafetyCheckResult:
        """Evaluates a raw token event against safety filters based on its venue."""
        logger.info(f"🔍 Analyzing Safety Filters for {event.symbol} ({event.token_address[:8]}...) [{event.launch_venue.upper()}]")

        if event.launch_venue == "pump_fun":
            result = await pump_safety_filter.evaluate(event)
        elif event.launch_venue == "raydium":
            result = await raydium_safety_filter.evaluate(event)
        else:
            # Default fallback evaluation
            result = await pump_safety_filter.evaluate(event)

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
                f"✅ [bold green]PASSED SAFETY[/bold green]: {event.symbol} | "
                f"Dev Buy: {result.dev_holding_pct:.1f}% | Top10: {result.top10_holder_pct:.1f}% | Venue: {event.launch_venue}"
            )
        else:
            logger.warning(
                f"❌ [bold red]REJECTED[/bold red]: {event.symbol} | "
                f"Reason: {result.rejection_reason}"
            )

        # 4. Forward to downstream callback (e.g. Opportunity Layer / Telegram Bot)
        if self.on_result_callback:
            try:
                await self.on_result_callback(result, event)
            except Exception as cb_err:
                logger.error(f"Error in filter result callback: {cb_err}")

        return result


filter_pipeline = FilterPipeline()
