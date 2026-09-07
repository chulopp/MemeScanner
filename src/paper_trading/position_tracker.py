"""
Position Tracker — Paper Trading Live
Live virtual position management with real-time MFE tracking and TP/SL execution.

Architecture:
  - open_position(): Creates a virtual position in DB when signal passes threshold
  - Polling loop (30s): Checks price for all open positions, updates MFE, triggers TP/SL
  - close_position(): Calculates mfe_pct, captured_ratio, records final state to DB

Frozen Parameters (per Implementation Plan, do NOT change before Checkpoint Day 40):
  - Threshold: 60.0
  - SL: -30%
  - TP1: +100% → sell 30% of position
  - TP2: +300% → sell 30% of position
  - TP3: +500% → sell 20% of position
  - Moonbag: remaining 20% with 40%-from-ATH trailing stop
  - Position Size: $2 per trade
  - Max Active Positions: 10
  - Price Polling Interval: 30 seconds
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from src.config import settings
from src.database.client import db_manager
from src.ingestion.schemas import RawTokenEvent
from src.paper_trading.price_fetcher import fetch_price
from src.utils.logger import logger

# ─────────────────────────────────────────────
# FROZEN PARAMETERS — do NOT modify until Day 40
# ─────────────────────────────────────────────
FROZEN_PARAMS = {
    "opportunity_threshold": float(settings.opportunity_threshold),
    "stop_loss_pct": -30.0,           # Hard stop at -30%
    "tp1_pct": 100.0,                 # Take Profit tier 1: +100%
    "tp1_sell_fraction": 0.30,        # Sell 30% of position at TP1
    "tp2_pct": 300.0,                 # Take Profit tier 2: +300%
    "tp2_sell_fraction": 0.30,        # Sell 30% of position at TP2
    "tp3_pct": 500.0,                 # Take Profit tier 3: +500%
    "tp3_sell_fraction": 0.20,        # Sell 20% of position at TP3
    "moonbag_fraction": 0.20,         # 20% moonbag after TP3
    "trailing_stop_from_ath_pct": 40.0,  # Exit moonbag if drops 40% from ATH
    "max_hold_hours": 4.0,            # Max hold duration: 4 hours timeout exit
    "position_size_usd": 2.0,         # $2 per trade (2% of $100 virtual)
    "max_active_positions": 10,       # Max simultaneous open positions
    "poll_interval_seconds": 30,      # Price polling cadence
    "parameter_version": "v1.1",      # Bump on 4H timeout introduction
}

POLL_DISCLAIMER = (
    "⚠️ *Disclaimer:* Angka return dihitung dari polling 30 detik. "
    "Realisasi aktual di pasar nyata bisa 10–30% lebih buruk, terutama pada posisi SL."
)


@dataclass
class ActivePosition:
    """In-memory state for a single open position being tracked."""
    position_id: str
    token_address: str
    symbol: str
    signal_source: str             # 'PINTU_A' | 'PINTU_B'
    entry_price_usd: float
    entry_time: datetime
    position_size_usd: float

    # TP milestone state
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False

    # Remaining fraction (tracks how much of position is still open after partial TPs)
    remaining_fraction: float = 1.0

    # MFE (Maximum Favorable Excursion) tracking
    price_high_ever_seen: float = 0.0
    bonding_curve_address: Optional[str] = None
    opportunity_score: float = 0.0
    entry_market_cap_usd: float = 0.0

    # In-memory latest cached price (updated by _poll_loop for instant UI/PnL responses)
    latest_price_usd: float = 0.0
    latest_mcap_usd: float = 0.0
    last_price_updated_at: Optional[datetime] = None


class PositionTracker:
    """
    Manages virtual paper trading positions with live price polling.

    Lifecycle:
      1. open_position() → creates DB record, registers in memory
      2. _poll_loop() → every 30s, checks prices, triggers TP/SL
      3. _close_position() → writes final P&L to DB
    """

    def __init__(self):
        self._active: dict[str, ActivePosition] = {}   # position_id → ActivePosition
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._open_lock = asyncio.Lock()

    # ──────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────

    async def get_open_count(self) -> int:
        """Returns number of currently open (non-skipped, non-closed) positions."""
        if not db_manager._connected:
            return len(self._active)
        try:
            db_open = await db_manager.query("paper_trade_positions", filters={"exit_reason": "eq.OPEN"}, limit=50)
            db_count = len([t for t in db_open if not t.get("skipped_reason")])
            return max(len(self._active), db_count)
        except Exception:
            return len(self._active)

    async def is_duplicate(self, token_address: str) -> bool:
        """Returns True if token already has an open position."""
        return any(p.token_address == token_address for p in self._active.values())

    async def open_position(
        self,
        event: RawTokenEvent,
        opportunity_score: float,
        paper_signal_id: Optional[str] = None,
        entry_price: Optional[float] = None,
        entry_market_cap_usd: Optional[float] = None,
    ) -> Optional[str]:
        """
        Opens a new virtual position if capacity allows.
        Records to DB. Returns position_id or None if skipped.

        Skipped positions are still recorded to DB with a skipped_reason
        so checkpoints can quantify missed signals.
        """
        async with self._open_lock:
            token_address = event.token_address
            symbol = event.symbol or "UNKNOWN"
            source_raw = getattr(event, "source", "NEW_PAIR") or "NEW_PAIR"
            signal_source = "PINTU_B" if source_raw == "WALLET_TRACKER" else "PINTU_A"

            now_utc = datetime.now(tz=timezone.utc)
            position_id = str(uuid.uuid4())

            # ── Capacity Check ──
            open_count = await self.get_open_count()
            if open_count >= FROZEN_PARAMS["max_active_positions"]:
                logger.info(
                    f"⛔ [PositionTracker] Skipping {symbol} — capacity full "
                    f"({open_count}/{FROZEN_PARAMS['max_active_positions']} positions open)"
                )
                await self._record_skipped(
                    position_id=position_id,
                    token_address=token_address,
                    symbol=symbol,
                    signal_source=signal_source,
                    opportunity_score=opportunity_score,
                    paper_signal_id=paper_signal_id,
                    skipped_reason="SKIPPED_CAPACITY",
                    now_utc=now_utc,
                )
                return None

            # ── Duplicate Check ──
            if await self.is_duplicate(token_address):
                logger.info(f"⛔ [PositionTracker] Skipping {symbol} — duplicate (already holding)")
                await self._record_skipped(
                    position_id=position_id,
                    token_address=token_address,
                    symbol=symbol,
                    signal_source=signal_source,
                    opportunity_score=opportunity_score,
                    paper_signal_id=paper_signal_id,
                    skipped_reason="DUPLICATE",
                    now_utc=now_utc,
                )
                return None

            # ── Fetch Entry Price ──
            resolved_price = 0.0
            resolved_mcap = 0.0
            bc_addr = getattr(event, "bonding_curve_address", None)
            price_snap = await fetch_price(token_address, bc_addr)
            if price_snap and price_snap.price_usd > 0:
                resolved_price = price_snap.price_usd
                resolved_mcap = price_snap.market_cap_usd
            elif entry_price and entry_price > 0:
                resolved_price = entry_price
                resolved_mcap = entry_market_cap_usd or (
                    entry_price * 1_000_000_000 if token_address.endswith("pump") else 0.0
                )

            if resolved_price <= 0:
                logger.warning(f"⚠️ [PositionTracker] Cannot open {symbol} — no price available")
                return None

            entry_price = resolved_price
            entry_mcap = entry_market_cap_usd or resolved_mcap
            position_size = FROZEN_PARAMS["position_size_usd"]

            # ── Insert to DB ──
            record = {
                "id": position_id,
                "token_address": token_address,
                "symbol": symbol[:20],
                "signal_source": signal_source,
                "paper_signal_id": paper_signal_id,
                "opportunity_score_at_entry": round(opportunity_score, 2),
                "entry_price_usd": entry_price,
                "entry_time": now_utc.isoformat(),
                "position_size_usd": position_size,
                "price_high_ever_seen": entry_price,
                "exit_reason": "OPEN",
                "parameter_version": FROZEN_PARAMS["parameter_version"],
                "skipped_reason": None,
            }

            try:
                await db_manager.insert("paper_trade_positions", record)
            except Exception as e:
                logger.error(f"❌ [PositionTracker] DB insert failed for {symbol}: {e}")
                return None

            # ── Register in memory ──
            pos = ActivePosition(
                position_id=position_id,
                token_address=token_address,
                symbol=symbol,
                signal_source=signal_source,
                entry_price_usd=entry_price,
                entry_time=now_utc,
                position_size_usd=position_size,
                price_high_ever_seen=entry_price,
                bonding_curve_address=bc_addr,
                opportunity_score=opportunity_score,
                entry_market_cap_usd=entry_mcap,
                latest_price_usd=entry_price,
                latest_mcap_usd=entry_mcap,
                last_price_updated_at=now_utc,
            )
            self._active[position_id] = pos

            logger.info(
                f"📂 [PositionTracker] Opened: ${symbol} ({token_address[:8]}...) | "
                f"Entry: ${entry_price:.8f} | Source: {signal_source} | Score: {opportunity_score:.1f}"
            )

            # Send Telegram notification (non-blocking)
            asyncio.create_task(self._notify_position_opened(pos))
            return position_id

    async def get_portfolio_summary(self) -> dict:
        """
        Computes real-time portfolio accounting instantly from in-memory cache:
        - Starting Capital ($100.0)
        - Realized PnL ($) from all closed trades (excluding CORRUPTED_RESET)
        - Allocated Capital ($) across currently open positions
        - Available Cash ($)
        - Floating PnL ($ and %) for each open position and total
        - Total Equity ($) and Total Portfolio ROI (%)
        """
        STARTING_CAPITAL = 100.0
        POSITION_SIZE = FROZEN_PARAMS["position_size_usd"]

        if not db_manager._connected:
            db_manager.connect()

        # Only sync with DB if active list in memory is empty
        if not self._active:
            await self._recover_open_positions()

        all_trades = await db_manager.query("paper_trade_positions", limit=5000)
        closed = [
            t for t in all_trades
            if t.get("exit_reason") not in ("OPEN", None, "CORRUPTED_RESET")
            and not t.get("skipped_reason")
        ]

        realized_pnl_usd = 0.0
        for t in closed:
            size = float(t.get("position_size_usd", POSITION_SIZE) or POSITION_SIZE)
            ret_pct = float(t.get("realized_return_pct", 0.0) or 0.0)
            realized_pnl_usd += size * (ret_pct / 100.0)

        active_list = list(self._active.values())
        allocated_usd = len(active_list) * POSITION_SIZE
        available_cash = STARTING_CAPITAL + realized_pnl_usd - allocated_usd

        # Compute instant live floating PnL using background-cached prices
        open_details = []
        total_floating_usd = 0.0

        for pos in active_list:
            cur_price = pos.latest_price_usd if pos.latest_price_usd > 0 else pos.entry_price_usd
            cur_mcap = pos.latest_mcap_usd if pos.latest_mcap_usd > 0 else (
                cur_price * 1_000_000_000 if pos.token_address.endswith("pump") else 0.0
            )
            entry_mcap = pos.entry_market_cap_usd or (
                pos.entry_price_usd * 1_000_000_000 if pos.token_address.endswith("pump") else 0.0
            )

            ret_pct = ((cur_price - pos.entry_price_usd) / pos.entry_price_usd * 100.0) if pos.entry_price_usd > 0 else 0.0
            pnl_usd = pos.position_size_usd * (ret_pct / 100.0)
            total_floating_usd += pnl_usd

            high = max(pos.price_high_ever_seen, cur_price)
            mfe_pct = ((high - pos.entry_price_usd) / pos.entry_price_usd * 100.0) if pos.entry_price_usd > 0 else 0.0
            hold_mins = (datetime.now(tz=timezone.utc) - pos.entry_time).total_seconds() / 60.0

            open_details.append({
                "symbol": pos.symbol,
                "token_address": pos.token_address,
                "signal_source": pos.signal_source,
                "entry_price": pos.entry_price_usd,
                "entry_mcap": entry_mcap,
                "current_price": cur_price,
                "current_mcap": cur_mcap,
                "floating_pct": ret_pct,
                "floating_usd": pnl_usd,
                "mfe_pct": mfe_pct,
                "hold_minutes": hold_mins,
                "position_size": pos.position_size_usd,
            })

        total_equity = available_cash + allocated_usd + total_floating_usd
        portfolio_roi_pct = ((total_equity - STARTING_CAPITAL) / STARTING_CAPITAL) * 100.0

        return {
            "starting_capital": STARTING_CAPITAL,
            "available_cash": available_cash,
            "allocated_usd": allocated_usd,
            "open_count": len(active_list),
            "realized_pnl_usd": realized_pnl_usd,
            "total_floating_usd": total_floating_usd,
            "total_equity": total_equity,
            "portfolio_roi_pct": portfolio_roi_pct,
            "open_positions": open_details,
            "closed_trades_count": len(closed),
            "closed_trades": closed,
        }

    # ──────────────────────────────────────────
    # Background polling loop
    # ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the 30-second polling loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            f"🔄 [PositionTracker] Polling loop started "
            f"(interval: {FROZEN_PARAMS['poll_interval_seconds']}s, "
            f"max_positions: {FROZEN_PARAMS['max_active_positions']})"
        )
        # Recover any open positions from DB on startup
        await self._recover_open_positions()

    async def stop(self) -> None:
        """Gracefully stop the polling loop."""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ [PositionTracker] Stopped cleanly")

    async def _poll_loop(self) -> None:
        """Main loop: polls prices and evaluates TP/SL for all active positions."""
        interval = FROZEN_PARAMS["poll_interval_seconds"]
        while self._running:
            if self._active:
                now_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
                logger.info(
                    f"🔁 [PricePoll] {now_str} — Fetching prices for "
                    f"{len(self._active)} active position(s): "
                    f"{', '.join(p.symbol for p in self._active.values())}"
                )
                # Process all open positions concurrently
                tasks = [
                    self._evaluate_position(pos)
                    for pos in list(self._active.values())
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

            await asyncio.sleep(interval)

    async def _evaluate_position(self, pos: ActivePosition) -> None:
        """Fetch current price and evaluate TP/SL conditions for one position."""
        try:
            snap = await fetch_price(pos.token_address, pos.bonding_curve_address)
            if not snap or snap.price_usd <= 0:
                logger.warning(f"⚠️ [PricePoll] No price for ${pos.symbol} ({pos.token_address[:8]}...)")
                return

            current_price = snap.price_usd
            entry = pos.entry_price_usd
            if entry <= 0:
                return

            # Update cached prices for instant reporting
            pos.latest_price_usd = current_price
            pos.latest_mcap_usd = snap.market_cap_usd if snap.market_cap_usd > 0 else (
                current_price * 1_000_000_000 if pos.token_address.endswith("pump") else 0.0
            )
            pos.last_price_updated_at = datetime.now(tz=timezone.utc)

            return_pct = ((current_price - entry) / entry) * 100.0
            logger.debug(
                f"💹 [PricePoll] ${pos.symbol} | src={snap.source} | "
                f"price=${current_price:.8f} | ret={return_pct:+.1f}%"
            )

            # Update MFE
            if current_price > pos.price_high_ever_seen:
                pos.price_high_ever_seen = current_price
                # Persist MFE to DB asynchronously
                asyncio.create_task(self._update_mfe_in_db(pos.position_id, current_price))

            # ── Stop Loss ──
            if return_pct <= FROZEN_PARAMS["stop_loss_pct"]:
                await self._close_position(pos, current_price, "SL")
                return

            # ── Max Hold Duration (Timeout 4 Jam) ──
            hold_hours = (datetime.now(tz=timezone.utc) - pos.entry_time).total_seconds() / 3600.0
            if hold_hours >= FROZEN_PARAMS.get("max_hold_hours", 4.0):
                logger.info(
                    f"⌛ [TIMEOUT_4H] ${pos.symbol} reached max hold time ({hold_hours:.1f}h >= {FROZEN_PARAMS.get('max_hold_hours', 4.0)}h) — "
                    f"closing at market ${current_price:.8f} (ret: {return_pct:+.1f}%)"
                )
                await self._close_position(pos, current_price, "TIMEOUT_4H")
                return

            # ── TP1: +100% ──
            if not pos.tp1_hit and return_pct >= FROZEN_PARAMS["tp1_pct"]:
                pos.tp1_hit = True
                sell_fraction = FROZEN_PARAMS["tp1_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP1] ${pos.symbol} hit +100% — selling {sell_fraction*100:.0f}%")
                asyncio.create_task(self._notify_tp_hit(pos, "TP1", return_pct, sell_fraction))

            # ── TP2: +300% ──
            if pos.tp1_hit and not pos.tp2_hit and return_pct >= FROZEN_PARAMS["tp2_pct"]:
                pos.tp2_hit = True
                sell_fraction = FROZEN_PARAMS["tp2_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP2] ${pos.symbol} hit +300% — selling {sell_fraction*100:.0f}%")
                asyncio.create_task(self._notify_tp_hit(pos, "TP2", return_pct, sell_fraction))

            # ── TP3: +500% ──
            if pos.tp2_hit and not pos.tp3_hit and return_pct >= FROZEN_PARAMS["tp3_pct"]:
                pos.tp3_hit = True
                sell_fraction = FROZEN_PARAMS["tp3_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP3] ${pos.symbol} hit +500% — selling {sell_fraction*100:.0f}%")
                asyncio.create_task(self._notify_tp_hit(pos, "TP3", return_pct, sell_fraction))
                # After TP3, remaining 20% becomes moonbag — we continue tracking

                # Record TP3 event (partial close of TP1+TP2+TP3 = 80% of position)
                await self._close_position(pos, current_price, "TP3")
                return

            # ── Moonbag Trailing Stop ──
            # Applies after TP3: if moonbag and price falls 40% from ATH
            if pos.tp3_hit:
                ath_return_pct = ((pos.price_high_ever_seen - entry) / entry) * 100.0
                drop_from_ath = ((current_price - pos.price_high_ever_seen) / pos.price_high_ever_seen) * 100.0
                if drop_from_ath <= -FROZEN_PARAMS["trailing_stop_from_ath_pct"]:
                    logger.info(
                        f"🌙 [TRAILING] ${pos.symbol} moonbag triggered — "
                        f"ATH: +{ath_return_pct:.0f}%, now dropped {drop_from_ath:.0f}% from ATH"
                    )
                    await self._close_position(pos, current_price, "TRAILING")
                    return

        except Exception as e:
            logger.debug(f"[PositionTracker] Evaluate error for {pos.symbol}: {e}")

    # ──────────────────────────────────────────
    # Position lifecycle helpers
    # ──────────────────────────────────────────

    async def _close_position(self, pos: ActivePosition, exit_price: float, reason: str) -> None:
        """Close position: compute P&L metrics and persist to DB."""
        # Remove from active tracking first (prevent duplicate closes)
        if pos.position_id not in self._active:
            return
        del self._active[pos.position_id]

        now_utc = datetime.now(tz=timezone.utc)
        entry = pos.entry_price_usd

        # Compute metrics
        realized_return_pct = ((exit_price - entry) / entry) * 100.0 if entry > 0 else 0.0
        mfe_pct = ((pos.price_high_ever_seen - entry) / entry) * 100.0 if entry > 0 else 0.0

        # captured_ratio: what fraction of the peak move was actually captured
        # Avoid division by zero; if MFE ~0 (token went sideways then dumped), use None
        if mfe_pct > 0.1:
            captured_ratio = realized_return_pct / mfe_pct
        else:
            captured_ratio = None

        hold_minutes = (now_utc - pos.entry_time).total_seconds() / 60.0

        update = {
            "exit_price_usd": exit_price,
            "exit_time": now_utc.isoformat(),
            "exit_reason": reason,
            "price_high_ever_seen": pos.price_high_ever_seen,
            "realized_return_pct": round(realized_return_pct, 4),
            "mfe_pct": round(mfe_pct, 4),
            "captured_ratio": round(captured_ratio, 4) if captured_ratio is not None else None,
            "hold_duration_minutes": round(hold_minutes, 2),
            "updated_at": now_utc.isoformat(),
        }

        try:
            await db_manager.update(
                "paper_trade_positions",
                update,
                filters={"id": f"eq.{pos.position_id}"}
            )
        except Exception as e:
            logger.error(f"❌ [PositionTracker] Failed to close position {pos.position_id[:8]}: {e}")

        status_emoji = {"SL": "🛑", "TP1": "✅", "TP2": "💚", "TP3": "💎", "TRAILING": "🌙", "TIMEOUT_4H": "⌛"}.get(reason, "📋")
        logger.info(
            f"{status_emoji} [Closed {reason}] ${pos.symbol} | "
            f"Return: {realized_return_pct:+.1f}% | MFE: {mfe_pct:+.1f}% | "
            f"Captured: {f'{captured_ratio*100:.0f}%' if captured_ratio is not None else 'N/A'} | "
            f"Hold: {hold_minutes:.0f}m"
        )

        # Send Telegram close notification
        asyncio.create_task(self._notify_position_closed(pos, exit_price, reason, realized_return_pct, mfe_pct))

    async def _update_mfe_in_db(self, position_id: str, new_high: float) -> None:
        """Persist new MFE high to DB (called asynchronously on every new ATH)."""
        try:
            await db_manager.update(
                "paper_trade_positions",
                {
                    "price_high_ever_seen": new_high,
                    "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                },
                filters={"id": f"eq.{position_id}"}
            )
        except Exception as e:
            logger.debug(f"[PositionTracker] MFE DB update failed for {position_id[:8]}: {e}")

    async def _record_skipped(
        self,
        position_id: str,
        token_address: str,
        symbol: str,
        signal_source: str,
        opportunity_score: float,
        paper_signal_id: Optional[str],
        skipped_reason: str,
        now_utc: datetime,
    ) -> None:
        """Record a skipped signal to DB for checkpoint analysis."""
        record = {
            "id": position_id,
            "token_address": token_address,
            "symbol": symbol[:20],
            "signal_source": signal_source,
            "paper_signal_id": paper_signal_id,
            "opportunity_score_at_entry": round(opportunity_score, 2),
            "entry_price_usd": 0.0,
            "entry_time": now_utc.isoformat(),
            "position_size_usd": 0.0,
            "price_high_ever_seen": 0.0,
            "exit_reason": None,
            "skipped_reason": skipped_reason,
            "parameter_version": FROZEN_PARAMS["parameter_version"],
        }
        try:
            await db_manager.insert("paper_trade_positions", record)
        except Exception as e:
            logger.debug(f"[PositionTracker] Failed to record skipped signal: {e}")

    async def _recover_open_positions(self) -> None:
        """On startup or on demand, recover positions with exit_reason = 'OPEN' from DB."""
        try:
            if not db_manager._connected:
                db_manager.connect()
            rows = await db_manager.query(
                "paper_trade_positions",
                filters={"exit_reason": "eq.OPEN"},
                limit=FROZEN_PARAMS["max_active_positions"] + 5
            )
            if not rows:
                return

            recovered = 0
            for row in rows:
                pos_id = row.get("id")
                if not pos_id or pos_id in self._active:
                    continue

                if len(self._active) >= FROZEN_PARAMS["max_active_positions"]:
                    logger.warning(
                        f"⚠️ [PositionTracker] Reached capacity limit "
                        f"({len(self._active)}/{FROZEN_PARAMS['max_active_positions']}). "
                        f"Skipping recovery for excess position {row.get('symbol')}."
                    )
                    break

                entry_time_str = row.get("entry_time", "")
                if isinstance(entry_time_str, str):
                    entry_time = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                else:
                    entry_time = entry_time_str or datetime.now(tz=timezone.utc)

                pos_entry_price = row.get("entry_price_usd", 0.0) or 0.0
                pos_token_addr = row.get("token_address", "")
                pos = ActivePosition(
                    position_id=pos_id,
                    token_address=pos_token_addr,
                    symbol=row.get("symbol", "UNKNOWN"),
                    signal_source=row.get("signal_source", "PINTU_A"),
                    entry_price_usd=pos_entry_price,
                    entry_time=entry_time,
                    position_size_usd=row.get("position_size_usd", 2.0) or 2.0,
                    price_high_ever_seen=row.get("price_high_ever_seen", 0.0) or 0.0,
                    # TP milestone state can't be exactly recovered; conservative approach:
                    tp1_hit=False,
                    tp2_hit=False,
                    tp3_hit=False,
                    remaining_fraction=1.0,
                    opportunity_score=float(row.get("opportunity_score_at_entry", 0.0) or 0.0),
                    entry_market_cap_usd=pos_entry_price * 1_000_000_000 if pos_token_addr.endswith("pump") else 0.0,
                    latest_price_usd=pos_entry_price,
                    latest_mcap_usd=pos_entry_price * 1_000_000_000 if pos_token_addr.endswith("pump") else 0.0,
                    last_price_updated_at=entry_time,
                )
                self._active[pos_id] = pos
                recovered += 1

            if recovered > 0:
                logger.info(f"🔄 [PositionTracker] Recovered {recovered} open positions from DB")

        except Exception as e:
            logger.warning(f"[PositionTracker] Recovery error: {e}")

    # ──────────────────────────────────────────
    # Telegram notification helpers
    # ──────────────────────────────────────────

    async def _notify_position_opened(self, pos: ActivePosition) -> None:
        try:
            from src.paper_trading.telegram_notifier import telegram_notifier
            await telegram_notifier.send_position_opened(
                symbol=pos.symbol,
                token_address=pos.token_address,
                signal_source=pos.signal_source,
                entry_price=pos.entry_price_usd,
                opportunity_score=pos.opportunity_score,
                position_size=pos.position_size_usd,
                entry_market_cap_usd=pos.entry_market_cap_usd,
            )
        except Exception as e:
            logger.debug(f"[PositionTracker] notify_opened failed: {e}")

    async def _notify_tp_hit(
        self, pos: ActivePosition, tier: str, return_pct: float, sell_fraction: float
    ) -> None:
        try:
            from src.paper_trading.telegram_notifier import telegram_notifier
            exit_price = pos.entry_price_usd * (1.0 + return_pct / 100.0)
            exit_mcap = exit_price * 1_000_000_000 if pos.token_address.endswith("pump") else 0.0
            await telegram_notifier.send_tp_hit(
                symbol=pos.symbol,
                token_address=pos.token_address,
                tier=tier,
                return_pct=return_pct,
                sell_fraction=sell_fraction,
                remaining_fraction=pos.remaining_fraction,
                entry_price=pos.entry_price_usd,
                exit_price=exit_price,
                entry_mcap=pos.entry_market_cap_usd,
                exit_mcap=exit_mcap,
                opportunity_score=pos.opportunity_score,
                signal_source=pos.signal_source,
            )
        except Exception as e:
            logger.debug(f"[PositionTracker] notify_tp failed: {e}")

    async def _notify_position_closed(
        self,
        pos: ActivePosition,
        exit_price: float,
        reason: str,
        realized_return_pct: float,
        mfe_pct: float,
    ) -> None:
        try:
            from src.paper_trading.telegram_notifier import telegram_notifier
            exit_mcap = exit_price * 1_000_000_000 if pos.token_address.endswith("pump") else 0.0
            if reason == "SL":
                await telegram_notifier.send_sl_hit(
                    symbol=pos.symbol,
                    token_address=pos.token_address,
                    return_pct=realized_return_pct,
                    hold_minutes=(datetime.now(tz=timezone.utc) - pos.entry_time).total_seconds() / 60.0,
                    entry_price=pos.entry_price_usd,
                    exit_price=exit_price,
                    entry_mcap=pos.entry_market_cap_usd,
                    exit_mcap=exit_mcap,
                    opportunity_score=pos.opportunity_score,
                    signal_source=pos.signal_source,
                )
            elif reason == "TRAILING":
                await telegram_notifier.send_trailing_stop_hit(
                    symbol=pos.symbol,
                    token_address=pos.token_address,
                    return_pct=realized_return_pct,
                    mfe_pct=mfe_pct,
                    entry_price=pos.entry_price_usd,
                    exit_price=exit_price,
                    entry_mcap=pos.entry_market_cap_usd,
                    exit_mcap=exit_mcap,
                    opportunity_score=pos.opportunity_score,
                    signal_source=pos.signal_source,
                )
            elif reason == "TIMEOUT_4H":
                await telegram_notifier.send_timeout_hit(
                    symbol=pos.symbol,
                    token_address=pos.token_address,
                    return_pct=realized_return_pct,
                    hold_minutes=(datetime.now(tz=timezone.utc) - pos.entry_time).total_seconds() / 60.0,
                    entry_price=pos.entry_price_usd,
                    exit_price=exit_price,
                    entry_mcap=pos.entry_market_cap_usd,
                    exit_mcap=exit_mcap,
                    opportunity_score=pos.opportunity_score,
                    signal_source=pos.signal_source,
                )
        except Exception as e:
            logger.debug(f"[PositionTracker] notify_closed failed: {e}")


# Singleton
position_tracker = PositionTracker()
