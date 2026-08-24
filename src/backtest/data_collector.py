"""
Data Collector — Fase 4 & 5
Real-time Live Token Ingestion at T=0 creation.
Captures newly launched tokens from PumpPortal WebSocket and Raydium stream at the exact second of birth.
Stores exact T=0 launch price, initial liquidity, and schedule 24-hour outcome resolution timestamp in `backtest_tokens`.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.ingestion.schemas import RawTokenEvent
from src.ingestion.pumpportal_ws import PumpPortalListener
from src.ingestion.raydium_ws import RaydiumListener
from src.database.client import db_manager
from src.utils.price_feed import price_feed
from src.utils.logger import logger

DEFAULT_PUMP_INITIAL_SOL = 30.0
DEFAULT_PUMP_SUPPLY = 1_000_000_000.0


async def _calculate_initial_launch_price(event: RawTokenEvent, sol_price_usd: float) -> float:
    """
    Calculates exact deterministic launch price at T=0 creation.
    For pump.fun: virtual SOL reserve (30 SOL) / total supply * SOL price.
    """
    if event.launch_venue == "pump_fun":
        v_sol = event.initial_sol_liquidity or DEFAULT_PUMP_INITIAL_SOL
        # Virtual reserves start at 30 SOL / 1.073B tokens
        price_sol = v_sol / 1_073_000_000.0
        return round(price_sol * sol_price_usd, 10)
    elif event.launch_venue == "raydium":
        if event.total_supply and event.total_supply > 0 and event.initial_sol_liquidity:
            price_sol = event.initial_sol_liquidity / event.total_supply
            return round(price_sol * sol_price_usd, 10)
        return round(0.00001 * sol_price_usd, 10)
    return round(0.000005 * sol_price_usd, 10)


class LiveBacktestCollector:
    """
    Listens to live token creation streams and persists forward-looking backtest samples.
    """

    def __init__(self, target_tokens: int = 200, resolution_delay_hours: int = 24):
        self.target_tokens = target_tokens
        self.resolution_delay_hours = resolution_delay_hours
        self.stored_count = 0
        self.seen_mints: set[str] = set()
        self._stop_event = asyncio.Event()
        self._pump_listener: Optional[PumpPortalListener] = None
        self._raydium_listener: Optional[RaydiumListener] = None

    async def _on_token_received(self, event: RawTokenEvent):
        mint = event.token_address
        if not mint or mint in self.seen_mints or self.stored_count >= self.target_tokens:
            return

        self.seen_mints.add(mint)
        sol_price = await price_feed.get_sol_price_usd()
        launch_price = await _calculate_initial_launch_price(event, sol_price)
        initial_liquidity_usd = (event.initial_sol_liquidity or 30.0) * sol_price

        now_utc = datetime.now(tz=timezone.utc)
        resolution_due = now_utc + timedelta(hours=self.resolution_delay_hours)

        record = {
            "token_address": mint,
            "symbol": event.symbol[:20] if event.symbol else "UNKNOWN",
            "name": event.name[:60] if event.name else "Unknown Token",
            "launch_venue": event.launch_venue,
            "listed_at": now_utc.isoformat(),
            "price_usd_at_listing": launch_price,
            "launch_price_usd": launch_price,
            "price_usd_24h": None,
            "price_24h_usd": None,
            "liquidity_usd": round(initial_liquidity_usd, 2),
            "volume_24h_usd": 0.0,
            "label": None,
            "label_return_pct": None,
            "is_resolved": False,
            "resolution_due_at": resolution_due.isoformat(),
            "resolved_at": None,
            "raw_dexscreener": event.raw_payload or {},
            "collected_at": now_utc.isoformat()
        }

        try:
            await db_manager.upsert("backtest_tokens", record, on_conflict="token_address")
            self.stored_count += 1
            logger.info(
                f"📥 [Live Ingest T=0] ({self.stored_count}/{self.target_tokens}) "
                f"{event.symbol} ({mint[:8]}...) | Launch Price: ${launch_price:.8f} | "
                f"Resolution Due: {resolution_due.strftime('%Y-%m-%d %H:%M UTC')}"
            )

            if self.stored_count >= self.target_tokens:
                logger.info(f"🎯 Target of {self.target_tokens} live tokens reached! Stopping stream...")
                self._stop_event.set()

        except Exception as e:
            logger.debug(f"Failed to persist live backtest token {mint[:8]}: {e}")

    async def run(self, max_duration_seconds: Optional[int] = None) -> int:
        """Starts live WebSocket streams until target is met or duration expires."""
        logger.info(f"🚀 Starting Live T=0 Token Ingestion (Target: {self.target_tokens} tokens)...")
        await db_manager.initialize()

        self._pump_listener = PumpPortalListener(self._on_token_received)
        self._raydium_listener = RaydiumListener(self._on_token_received)

        await self._pump_listener.start()
        await self._raydium_listener.start()

        try:
            if max_duration_seconds:
                await asyncio.wait_for(self._stop_event.wait(), timeout=float(max_duration_seconds))
            else:
                await self._stop_event.wait()
        except asyncio.TimeoutError:
            logger.info(f"⏰ Duration limit of {max_duration_seconds}s reached.")
        finally:
            await self._pump_listener.stop()
            await self._raydium_listener.stop()

        logger.info(f"✅ Live collection finished. Total tokens ingested: {self.stored_count}")
        return self.stored_count


async def collect_live_tokens(target: int = 200, duration_seconds: Optional[int] = None) -> int:
    collector = LiveBacktestCollector(target_tokens=target)
    return await collector.run(max_duration_seconds=duration_seconds)
