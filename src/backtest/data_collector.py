"""
Data Collector — Fase 4 & B
Real-time Live Token Ingestion at T=0 creation.
Captures newly launched tokens from PumpPortal WebSocket and Raydium stream at the exact second of birth.
Stores exact T=0 launch price, initial liquidity, and schedule 24-hour outcome resolution timestamp in `backtest_tokens`.

Fase B (R9/R13/R17):
After persisting T=0 data, schedules a background task to capture T+2 minute price.
T+2 price source: bonding curve on-chain (primary) → DexScreener/Helius (fallback).
Stored in `price_usd_at_t2` column for EV separation in backtest (R14).
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
            "price_usd_at_t2": None,  # Populated by _fetch_t2_price after 2 min (R9/Fase B)
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

            # Fase B (R9): schedule T+2 price capture in background (non-blocking)
            asyncio.create_task(self._fetch_t2_price(event))

            if self.stored_count >= self.target_tokens:
                logger.info(f"🎯 Target of {self.target_tokens} live tokens reached! Stopping stream...")
                self._stop_event.set()

        except Exception as e:
            logger.debug(f"Failed to persist live backtest token {mint[:8]}: {e}")

    async def _fetch_t2_price(self, event: RawTokenEvent) -> None:
        """
        Fase B (R9/R13/R17): Capture token price at T+2 minutes after listing.

        PRIMARY:  Bonding curve on-chain state via get_bonding_curve_price()  (R13+R17)
                  Only valid while token is still in bonding curve (is_complete=False).
        FALLBACK: 3-tier price fetcher (DexScreener -> Helius DAS -> RPC)
                  Used when token has graduated or BC price unavailable.

        Writes result to `price_usd_at_t2` in backtest_tokens.
        If price unavailable at T+2, leaves column NULL (t0_fallback=True in backtest).
        """
        token_address = event.token_address
        try:
            await asyncio.sleep(120.0)  # Wait T+2 minutes

            price_t2: Optional[float] = None
            source = "none"

            # PRIMARY: Bonding curve on-chain state (R13+R17)
            bc_address = event.bonding_curve_address
            if bc_address and event.launch_venue == "pump_fun":
                try:
                    from src.utils.solana_rpc import solana_rpc
                    bc_data = await solana_rpc.get_bonding_curve_price(bc_address)
                    if bc_data and not bc_data["is_complete"]:
                        # Token still in bonding curve — price is valid and current
                        sol_price_usd = await price_feed.get_sol_price_usd()
                        price_t2 = bc_data["price_sol"] * sol_price_usd
                        source = "bonding_curve_onchain"
                        logger.debug(
                            f"BC T+2 price for {token_address[:8]}: "
                            f"vSol={bc_data['virtual_sol_reserves']/1e9:.2f} SOL, "
                            f"price_sol={bc_data['price_sol']:.10f}, "
                            f"price_usd=${price_t2:.10f}"
                        )
                    elif bc_data and bc_data["is_complete"]:
                        # Token graduated at T+2 — BC price is stale, fall through to DexScreener
                        logger.debug(
                            f"BC complete at T+2 for {token_address[:8]} — graduated, "
                            f"falling back to DexScreener"
                        )
                except Exception as e:
                    logger.debug(f"BC T+2 fetch failed for {token_address[:8]}: {e}")

            # FALLBACK: 3-tier price fetcher (DexScreener -> Helius DAS -> RPC)
            if price_t2 is None:
                try:
                    from src.paper_trading.price_fetcher import fetch_price
                    snap = await fetch_price(token_address)
                    if snap and snap.price_usd > 0:
                        price_t2 = snap.price_usd
                        source = snap.source
                except Exception as e:
                    logger.debug(f"Fallback T+2 fetch failed for {token_address[:8]}: {e}")

            # Persist T+2 price (or log if unavailable — column stays NULL, t0_fallback=True in backtest)
            if price_t2 and price_t2 > 0:
                await db_manager.update(
                    "backtest_tokens",
                    filters={"token_address": f"eq.{token_address}"},
                    data={"price_usd_at_t2": price_t2}
                )
                logger.info(
                    f"📊 [T+2] {event.symbol} ({token_address[:8]}): "
                    f"${price_t2:.8f} (source={source})"
                )
            else:
                logger.debug(
                    f"⚠️ T+2 price unavailable for {token_address[:8]} — "
                    f"column stays NULL, backtest will use t0_fallback=True"
                )

        except asyncio.CancelledError:
            pass  # Task cancelled on shutdown — expected, not an error
        except Exception as e:
            logger.debug(f"_fetch_t2_price failed for {token_address[:8]}: {e}")

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
