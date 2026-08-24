"""
Signal Recorder — Fase 5
Records every token that passes the full pipeline (filter + scoring) as a live paper trading signal.
Also records baseline tokens (passed filter but below score threshold) for statistical comparison.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from src.config import settings
from src.database.client import db_manager
from src.ingestion.schemas import RawTokenEvent
from src.filters.schemas import SafetyCheckResult
from src.paper_trading.price_fetcher import fetch_price
from src.paper_trading.telegram_notifier import telegram_notifier
from src.utils.logger import logger


async def record_signal(
    event: RawTokenEvent,
    safety_result: SafetyCheckResult
) -> Optional[str]:
    """
    Records a signal (opportunity score >= threshold) to Supabase paper_signals table.
    Sends Telegram Stage 1 notification.
    Returns the signal UUID or None on failure.
    """
    score = safety_result.opportunity_score or 0.0
    breakdown = safety_result.opportunity_breakdown or {}
    is_baseline = score < settings.opportunity_threshold

    # Fetch entry price via 3-tier price feed
    price_snap = await fetch_price(event.token_address)
    entry_price = price_snap.price_usd if price_snap else 0.0
    entry_liq = price_snap.liquidity_usd if price_snap else 0.0

    now_utc = datetime.now(tz=timezone.utc).isoformat()

    record = {
        "token_address": event.token_address,
        "symbol": (event.symbol or "UNKNOWN")[:20],
        "name": (event.name or "")[:60],
        "launch_venue": event.launch_venue,
        "signal_at": now_utc,
        "opportunity_score": round(score, 2),
        "score_breakdown": breakdown,
        "entry_price_usd": entry_price,
        "entry_liquidity_usd": entry_liq,
        "signal_threshold_used": settings.opportunity_threshold,
        "passed_filter_tags": _extract_filter_tags(safety_result),
        "is_baseline": is_baseline,
        "telegram_message_id": None,
        "resolved_5m": False,
        "resolved_15m": False,
        "resolved_1h": False,
        "resolved_4h": False,
        "resolved_24h": False
    }

    try:
        result = await db_manager.insert("paper_signals", record)
        signal_id = result[0]["id"] if result and len(result) > 0 else None

        signal_type = "BASELINE" if is_baseline else "SIGNAL"
        logger.info(
            f"📝 [{signal_type}] Recorded: {event.symbol} ({event.token_address[:8]}...) | "
            f"Score: {score:.1f} | Entry: ${entry_price:.8f} | Liq: ${entry_liq:,.0f}"
        )

        # Send Telegram notification for above-threshold signals (Stage 1 Fast-Path)
        if not is_baseline:
            msg_id = await telegram_notifier.send_signal_notification(
                token_address=event.token_address,
                symbol=event.symbol or "UNKNOWN",
                name=event.name or "",
                opportunity_score=score,
                score_breakdown=breakdown,
                entry_price_usd=entry_price,
                entry_liquidity_usd=entry_liq,
                launch_venue=event.launch_venue,
                is_baseline=False
            )
            if msg_id and signal_id:
                await db_manager.update(
                    "paper_signals",
                    {"telegram_message_id": msg_id},
                    filters={"id": f"eq.{signal_id}"}
                )

                # Stage 2: Trigger async LLM synthesis & in-place message edit
                asyncio.create_task(_run_stage2_synthesis(
                    event=event,
                    safety_result=safety_result,
                    score=score,
                    score_breakdown=breakdown,
                    entry_price_usd=entry_price,
                    entry_liquidity_usd=entry_liq,
                    msg_id=msg_id,
                    signal_id=signal_id
                ))

        return signal_id

    except Exception as e:
        logger.error(f"❌ Failed to record signal for {event.token_address[:8]}: {e}")
        return None


async def _run_stage2_synthesis(
    event: RawTokenEvent,
    safety_result: SafetyCheckResult,
    score: float,
    score_breakdown: dict,
    entry_price_usd: float,
    entry_liquidity_usd: float,
    msg_id: int,
    signal_id: str
):
    """Async background worker for Stage 2 LLM reasoning synthesis & message editing."""
    try:
        from src.llm.synthesis_engine import synthesis_engine
        reasoning = await synthesis_engine.generate_synthesis(
            event=event,
            safety_result=safety_result,
            score=score,
            score_breakdown=score_breakdown,
            entry_price_usd=entry_price_usd,
            entry_liquidity_usd=entry_liq
        )

        if reasoning:
            # Edit Telegram message with 3-bullet points
            await telegram_notifier.edit_signal_with_synthesis(
                message_id=msg_id,
                token_address=event.token_address,
                symbol=event.symbol or "UNKNOWN",
                name=event.name or "",
                opportunity_score=score,
                score_breakdown=score_breakdown,
                entry_price_usd=entry_price_usd,
                entry_liquidity_usd=entry_liquidity_usd,
                launch_venue=event.launch_venue,
                reasoning_text=reasoning,
                is_baseline=False
            )

            # Persist reasoning to Supabase
            await db_manager.update(
                "paper_signals",
                {
                    "llm_reasoning": reasoning,
                    "two_stage_status": "COMPLETED"
                },
                filters={"id": f"eq.{signal_id}"}
            )
        else:
            await db_manager.update(
                "paper_signals",
                {"two_stage_status": "SKIPPED"},
                filters={"id": f"eq.{signal_id}"}
            )
    except Exception as e:
        logger.warning(f"Stage 2 synthesis background task error: {e}")
        try:
            await db_manager.update(
                "paper_signals",
                {"two_stage_status": "FAILED"},
                filters={"id": f"eq.{signal_id}"}
            )
        except Exception:
            pass



def _extract_filter_tags(result: SafetyCheckResult) -> list[str]:
    """Extract list of passed filter checks from SafetyCheckResult."""
    tags = []
    if result.mint_authority_renounced:
        tags.append("mint_renounced")
    if result.freeze_authority_renounced:
        tags.append("freeze_renounced")
    if result.lp_locked_or_burned:
        tags.append("lp_locked")
    if result.honeypot_check_passed:
        tags.append("honeypot_clear")
    if result.instant_scalp_flags_count == 0:
        tags.append("no_scalp_flags")
    if (result.sniper_bundle_pct or 0) < 20:
        tags.append("low_bundle_risk")
    return tags
