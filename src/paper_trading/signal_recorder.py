"""
Signal Recorder — Fase 5 (Paper Trading Live)
Records every token that passes the full pipeline (filter + scoring) as a live paper trading signal.
Also records baseline tokens (passed filter but below score threshold) for statistical comparison.

Paper Trading Live additions (per implementation plan):
  - Forwards event.source as signal_source (PINTU_A | PINTU_B) to paper_signals
  - Calls position_tracker.open_position() for above-threshold signals
  - Capacity/duplicate handling is delegated to PositionTracker
"""

import asyncio
import uuid
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
    bc_addr = getattr(event, "bonding_curve_address", None)
    price_snap = await fetch_price(event.token_address, bc_addr)
    entry_price = price_snap.price_usd if price_snap else 0.0
    entry_liq = price_snap.liquidity_usd if price_snap else 0.0
    entry_mcap = price_snap.market_cap_usd if price_snap else 0.0

    now_utc = datetime.now(tz=timezone.utc).isoformat()
    signal_id = str(uuid.uuid4())

    # Map event.source to signal_source for Pintu A vs Pintu B tracking
    source_raw = getattr(event, "source", "NEW_PAIR") or "NEW_PAIR"
    signal_source = "PINTU_B" if source_raw == "WALLET_TRACKER" else "PINTU_A"

    record = {
        "id": signal_id,
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
        "signal_source": signal_source,
        "telegram_message_id": None,
        "resolved_5m": False,
        "resolved_15m": False,
        "resolved_1h": False,
        "resolved_4h": False,
        "resolved_24h": False
    }

    try:
        await db_manager.insert("paper_signals", record)

        signal_type = "BASELINE" if is_baseline else "SIGNAL"
        logger.info(
            f"📝 [{signal_type}] Recorded: {event.symbol} ({event.token_address[:8]}...) | "
            f"Score: {score:.1f} | Entry: ${entry_price:.8f} | MC: ${entry_mcap:,.0f} | Liq: ${entry_liq:,.0f} | "
            f"Source: {signal_source}"
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
                is_baseline=False,
                market_cap_usd=entry_mcap
            )
            if msg_id:
                await db_manager.update(
                    "paper_signals",
                    {"telegram_message_id": msg_id},
                    filters={"id": f"eq.{signal_id}"}
                )

            # Paper Trading Live: open virtual position
            asyncio.create_task(_open_virtual_position(event, score, signal_id, entry_price, entry_mcap))

        return signal_id

    except Exception as e:
        logger.error(f"❌ Failed to record signal for {event.token_address[:8]}: {e}")
        return None


async def _open_virtual_position(
    event: RawTokenEvent,
    opportunity_score: float,
    signal_id: str,
    entry_price: Optional[float] = None,
    entry_market_cap_usd: Optional[float] = None,
) -> None:
    """
    Background task: open a virtual position in PositionTracker.
    Skipped signals (capacity/duplicate) are recorded to paper_trade_positions by PositionTracker.
    """
    try:
        from src.paper_trading.position_tracker import position_tracker
        await position_tracker.open_position(
            event=event,
            opportunity_score=opportunity_score,
            paper_signal_id=signal_id,
            entry_price=entry_price,
            entry_market_cap_usd=entry_market_cap_usd,
        )
    except Exception as e:
        logger.warning(f"[SignalRecorder] open_virtual_position failed for {event.token_address[:8]}: {e}")


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
