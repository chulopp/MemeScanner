"""
Position Tracker — Paper Trading Live (v2.0)
Live virtual position management with real-time MFE tracking and TP/SL execution.

Architecture:
  - open_position(): Creates a virtual position in DB when signal passes threshold
  - Polling loop (30s): Checks price for all open positions, updates MFE, triggers TP/SL
  - close_position(): Calculates mfe_pct, captured_ratio, records final state to DB

Parameter Version v2.0 (Exit Engine v2):
  - Threshold: 60.0 (unchanged)
  - SL: -30% (base), tightened to -15% at 15m if MFE<15%, exit at 30m if MFE<15% [TIME_DECAY]
  - Breakeven stop: +50% trigger → SL moves to -10%
  - TP0: +50% → sell 15%
  - TP1: +100% → sell 25%
  - TP2: +300% → sell 25%
  - TP3: +500% → sell 15%
  - Moonbag: remaining 20% with tiered trailing (25%/35%/45% from ATH)
  - Rug guard: exit if liquidity drops >80% from entry baseline
  - Max hold: 2 hours (TIMEOUT_2H) — down from 4 hours
  - Position Size: 2% of equity per trade
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

# ──────────────────────────────────────────────────────────────────────────
# FROZEN PARAMETERS — Exit Engine v2.0 (do NOT change without /grill-me)
# ──────────────────────────────────────────────────────────────────────────
FROZEN_PARAMS = {
    "opportunity_threshold": float(settings.opportunity_threshold),

    # ── Stop Loss ──
    "stop_loss_pct": -30.0,           # Base SL at -30% (phase 1 of time-decay)
    "breakeven_trigger_pct": 50.0,    # v2.0: Trigger breakeven stop at +50%
    "breakeven_sl_pct": -10.0,        # v2.0: SL after breakeven activated (loose room for noise)

    # ── Time-decay Stop Loss (replaces stagnancy v1.3) ──
    "time_decay_tighten_minutes": 15.0,   # v2.0: Phase 2 starts after 15m
    "time_decay_exit_minutes": 30.0,      # v2.0: Phase 3 (force exit) after 30m
    "time_decay_mfe_threshold_pct": 15.0, # v2.0: If MFE < 15%, time-decay triggers
    "time_decay_sl_tighten_pct": -15.0,  # v2.0: Phase 2 SL tightened to -15%

    # ── Take Profit Tiers ──
    "tp0_pct": 50.0,                  # v2.0: New TP0 tier: +50%
    "tp0_sell_fraction": 0.15,        # v2.0: Sell 15% at TP0
    "tp1_pct": 100.0,                 # TP1: +100%
    "tp1_sell_fraction": 0.25,        # v2.0: Sell 25% at TP1 (was 30%)
    "tp2_pct": 300.0,                 # TP2: +300%
    "tp2_sell_fraction": 0.25,        # v2.0: Sell 25% at TP2 (was 30%)
    "tp3_pct": 500.0,                 # TP3: +500%
    "tp3_sell_fraction": 0.15,        # v2.0: Sell 15% at TP3 (was 20%)
    "moonbag_fraction": 0.20,         # 20% moonbag after TP3 (unchanged)

    # ── Tiered Trailing Stop (v2.2: Active once ATH return >= +30%) ──
    "trailing_start_return_pct": 30.0,   # v2.2: Active once token ATH return >= +30%
    "trailing_tier1_max_return": 200.0,  # ATH return 30%-200% → 20% trailing
    "trailing_tier1_pct": 20.0,          # v2.2: Trail 20% from ATH (was 25%)
    "trailing_tier2_max_return": 500.0,  # v2.0: ATH return 200%-500% → 35% trailing
    "trailing_tier2_pct": 35.0,
    "trailing_tier3_pct": 45.0,          # v2.0: ATH return >500% → 45% trailing

    # ── Rug Guard ──
    "rug_guard_drop_threshold_pct": 80.0,  # v2.0: Exit if liquidity drops >80% from entry

    # ── Timing ──
    "max_hold_hours": 2.0,            # v2.0: Reduced from 4h to 2h (TIMEOUT_2H)

    # ── Position Sizing ──
    "position_size_usd": 2.0,         # Fallback / initial $2 per trade (2% of $100 virtual)
    "position_risk_pct": 2.0,         # 2% of current equity per trade (rebased dynamic sizing)
    "min_position_size_usd": 0.05,    # Floor to prevent opening micro-dust positions (< 5 cents)

    # ── Other ──
    "max_active_positions": 10,       # Max simultaneous open positions
    "poll_interval_seconds": 30,      # Price polling cadence
    "parameter_version": "v2.3",      # v2.3: Stage 2 Filter Likuiditas >= $10k (Hipotesis A) + Net Buy Pressure Cap 6.5x (Hipotesis B) + Unified PumpPortal (Free Mode)
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

    # TP milestone state (persisted to DB to survive restarts)
    tp0_hit: bool = False          # v2.0: TP0 at +50%
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

    # v2.0: Exit Engine v2 state (persisted to DB)
    breakeven_sl_active: bool = False      # True after first reach of +50% — SL moves to -10%
    liquidity_at_entry_usd: float = 0.0   # Baseline liquidity for rug guard detection

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

            # ── Capital & Cash Guard (Dynamic Equity Rebasing) ──
            summary = await self.get_portfolio_summary()
            current_equity = max(summary.get("total_equity", 0.0), 0.0)
            available_cash = summary.get("available_cash", 0.0)
            risk_pct = FROZEN_PARAMS.get("position_risk_pct", 2.0)
            min_size = FROZEN_PARAMS.get("min_position_size_usd", 0.05)

            dynamic_size = round(current_equity * (risk_pct / 100.0), 4)

            # Strict guard: check if bankrupt, cash insufficient, or size below floor
            if (
                available_cash <= 0
                or current_equity <= min_size
                or dynamic_size < min_size
                or available_cash < dynamic_size
            ):
                logger.warning(
                    f"⛔ [PositionTracker] Skipping {symbol} — insufficient cash / depleted capital "
                    f"(Cash: ${available_cash:.2f}, Req: ${dynamic_size:.4f}, Equity: ${current_equity:.2f})"
                )
                await self._record_skipped(
                    position_id=position_id,
                    token_address=token_address,
                    symbol=symbol,
                    signal_source=signal_source,
                    opportunity_score=opportunity_score,
                    paper_signal_id=paper_signal_id,
                    skipped_reason="SKIPPED_INSUFFICIENT_CASH",
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
            position_size = dynamic_size

            # v2.0: Capture liquidity at entry for rug guard baseline
            entry_liquidity = price_snap.liquidity_usd if price_snap else 0.0

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
                # TP milestone state — persisted for crash recovery
                "tp0_hit": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "remaining_fraction": 1.0,
                # v2.0: Exit Engine v2 state
                "breakeven_sl_active": False,
                "liquidity_at_entry_usd": round(entry_liquidity, 2),
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
                # v2.0 new fields
                liquidity_at_entry_usd=entry_liquidity,
            )
            self._active[position_id] = pos

            logger.info(
                f"📂 [PositionTracker] Opened: ${symbol} ({token_address[:8]}...) | "
                f"Entry: ${entry_price:.8f} | Source: {signal_source} | Score: {opportunity_score:.1f}"
            )

            # Send Telegram notification (non-blocking)
            asyncio.create_task(self._notify_position_opened(pos))
            return position_id

    async def get_portfolio_summary(self, version: Optional[str] = None) -> dict:
        """
        Computes real-time portfolio accounting instantly from in-memory cache:
        - Starting Capital ($100.0) for active cycle (v2.0)
        - Realized PnL ($) from closed trades of the active parameter_version cycle
        - Allocated Capital ($) across currently open positions
        - Available Cash ($)
        - Floating PnL ($ and %) for each open position and total
        - Total Equity ($) and Total Portfolio ROI (%)
        """
        STARTING_CAPITAL = 100.0
        POSITION_SIZE = FROZEN_PARAMS["position_size_usd"]
        target_version = version or FROZEN_PARAMS.get("parameter_version", "v2.1")

        if not db_manager._connected:
            db_manager.connect()

        # Only sync with DB if active list in memory is empty
        if not self._active:
            await self._recover_open_positions()

        all_trades = await db_manager.query("paper_trade_positions", limit=5000)
        all_closed = [
            t for t in all_trades
            if t.get("exit_reason") not in ("OPEN", None, "CORRUPTED_RESET")
            and not t.get("skipped_reason")
        ]

        # Historical all-time PnL across previous cycles (for archive reporting)
        all_time_pnl_usd = sum(
            float(t.get("position_size_usd", POSITION_SIZE) or POSITION_SIZE) * (float(t.get("realized_return_pct", 0.0) or 0.0) / 100.0)
            for t in all_closed
        )

        # Scoped strictly to the active cycle version (e.g. v2.0)
        closed = [
            t for t in all_closed
            if t.get("parameter_version") == target_version
        ]

        realized_pnl_usd = 0.0
        for t in closed:
            size = float(t.get("position_size_usd", POSITION_SIZE) or POSITION_SIZE)
            ret_pct = float(t.get("realized_return_pct", 0.0) or 0.0)
            realized_pnl_usd += size * (ret_pct / 100.0)

        active_list = list(self._active.values())
        allocated_usd = sum(p.position_size_usd for p in active_list)
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
            "parameter_version": target_version,
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
            "all_time_closed_count": len(all_closed),
            "all_time_pnl_usd": all_time_pnl_usd,
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
        """Fetch current price and evaluate TP/SL conditions for one position (v2.0)."""
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

            # Compute running MFE % for time-decay checks
            mfe_pct = (
                (pos.price_high_ever_seen - entry) / entry * 100.0
                if entry > 0 else 0.0
            )

            # ── 1. Rug Guard (safety-net, runs first — parallel to all other logic) ──
            # v2.0: exit immediately if liquidity collapses >80% from entry baseline.
            rug_threshold = FROZEN_PARAMS["rug_guard_drop_threshold_pct"]
            current_liquidity = snap.liquidity_usd
            if (
                pos.liquidity_at_entry_usd > 0
                and current_liquidity > 0
                and current_liquidity < pos.liquidity_at_entry_usd * (1.0 - rug_threshold / 100.0)
            ):
                drop_pct = (1.0 - current_liquidity / pos.liquidity_at_entry_usd) * 100.0
                logger.warning(
                    f"🚨 [RUG_DETECTED] ${pos.symbol} — liquidity collapsed "
                    f"{drop_pct:.0f}% (${current_liquidity:.0f} from ${pos.liquidity_at_entry_usd:.0f}) — "
                    f"exiting immediately"
                )
                await self._close_position(pos, current_price, "RUG_DETECTED")
                return

            # ── 2. Effective Stop Loss (adapts based on breakeven state + time-decay phase) ──
            hold_hours = (datetime.now(tz=timezone.utc) - pos.entry_time).total_seconds() / 3600.0
            hold_minutes = hold_hours * 60.0

            td_mfe_threshold = FROZEN_PARAMS["time_decay_mfe_threshold_pct"]
            td_tighten_min   = FROZEN_PARAMS["time_decay_tighten_minutes"]
            td_exit_min      = FROZEN_PARAMS["time_decay_exit_minutes"]
            base_sl          = FROZEN_PARAMS["stop_loss_pct"]           # -30%
            tight_sl         = FROZEN_PARAMS["time_decay_sl_tighten_pct"]  # -15%
            be_sl            = FROZEN_PARAMS["breakeven_sl_pct"]        # -10%

            # Determine effective SL for this poll:
            # Breakeven overrides time-decay if active (tighter protection)
            if pos.breakeven_sl_active:
                effective_sl = be_sl  # -10%
            elif hold_minutes >= td_tighten_min and mfe_pct < td_mfe_threshold:
                effective_sl = tight_sl  # -15% (time-decay phase 2)
            else:
                effective_sl = base_sl   # -30% (base)

            if return_pct <= effective_sl:
                phase = "breakeven" if pos.breakeven_sl_active else (
                    "time-decay-tight" if effective_sl == tight_sl else "base"
                )
                logger.info(
                    f"🛑 [SL/{phase.upper()}] ${pos.symbol} — return {return_pct:+.1f}% "
                    f"≤ effective SL {effective_sl:+.1f}% (held {hold_minutes:.0f}m, MFE {mfe_pct:+.1f}%)"
                )
                # v2.1: SL Capping — simulate live on-chain limit/trigger order fill at effective_sl
                # Eliminates artificial 30s polling lag gap-down (-90% vs target -30%)
                simulated_sl_price = entry * (1.0 + effective_sl / 100.0) if entry > 0 else current_price
                await self._close_position(pos, simulated_sl_price, "SL")
                return

            # ── 3. Time-decay Phase 3: Force exit (replaces stagnancy v1.3) ──
            # Phase 3 — after 30m: if MFE still below threshold, this is a zombie. Exit.
            # Condition: NOT triggered if breakeven is already active (position has shown momentum).
            if (
                not pos.breakeven_sl_active
                and not pos.tp0_hit  # TP0 means token reached +50%, not a zombie
                and hold_minutes >= td_exit_min
                and mfe_pct < td_mfe_threshold
            ):
                logger.info(
                    f"⏳ [TIME_DECAY] ${pos.symbol} — held {hold_minutes:.0f}m, "
                    f"MFE only {mfe_pct:+.1f}% (< {td_mfe_threshold}%) — "
                    f"time-decay exit, freeing slot"
                )
                await self._close_position(pos, current_price, "TIME_DECAY")
                return

            # ── 4. Max Hold Duration (TIMEOUT_2H — hard ceiling) ──
            if hold_hours >= FROZEN_PARAMS["max_hold_hours"]:
                logger.info(
                    f"⌛ [TIMEOUT_2H] ${pos.symbol} reached max hold time "
                    f"({hold_hours:.1f}h ≥ {FROZEN_PARAMS['max_hold_hours']}h) — "
                    f"closing at market ${current_price:.8f} (ret: {return_pct:+.1f}%)"
                )
                await self._close_position(pos, current_price, "TIMEOUT_2H")
                return

            # ── 5. Breakeven Stop Activation ──
            # v2.0: once return reaches +50%, move SL to -10% (protect from round-trip losses)
            be_trigger = FROZEN_PARAMS["breakeven_trigger_pct"]
            if not pos.breakeven_sl_active and return_pct >= be_trigger:
                pos.breakeven_sl_active = True
                asyncio.create_task(self._persist_tp_state(pos))
                logger.info(
                    f"🔒 [BREAKEVEN] ${pos.symbol} hit +{be_trigger:.0f}% — "
                    f"SL moved from {base_sl:+.0f}% to {be_sl:+.0f}% (breakeven guard activated)"
                )

            # ── 6. TP0: +50% ──
            if not pos.tp0_hit and return_pct >= FROZEN_PARAMS["tp0_pct"]:
                pos.tp0_hit = True
                sell_fraction = FROZEN_PARAMS["tp0_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP0] ${pos.symbol} hit +50% — selling {sell_fraction*100:.0f}%")
                asyncio.create_task(self._persist_tp_state(pos))
                asyncio.create_task(self._notify_tp_hit(pos, "TP0", return_pct, sell_fraction, current_price))

            # ── 7. TP1: +100% ──
            if pos.tp0_hit and not pos.tp1_hit and return_pct >= FROZEN_PARAMS["tp1_pct"]:
                pos.tp1_hit = True
                sell_fraction = FROZEN_PARAMS["tp1_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP1] ${pos.symbol} hit +100% — selling {sell_fraction*100:.0f}%")
                asyncio.create_task(self._persist_tp_state(pos))
                asyncio.create_task(self._notify_tp_hit(pos, "TP1", return_pct, sell_fraction, current_price))

            # ── 8. TP2: +300% ──
            if pos.tp1_hit and not pos.tp2_hit and return_pct >= FROZEN_PARAMS["tp2_pct"]:
                pos.tp2_hit = True
                sell_fraction = FROZEN_PARAMS["tp2_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP2] ${pos.symbol} hit +300% — selling {sell_fraction*100:.0f}%")
                asyncio.create_task(self._persist_tp_state(pos))
                asyncio.create_task(self._notify_tp_hit(pos, "TP2", return_pct, sell_fraction, current_price))

            # ── 9. TP3: +500% ──
            if pos.tp2_hit and not pos.tp3_hit and return_pct >= FROZEN_PARAMS["tp3_pct"]:
                pos.tp3_hit = True
                sell_fraction = FROZEN_PARAMS["tp3_sell_fraction"]
                pos.remaining_fraction -= sell_fraction
                logger.info(f"🎯 [TP3] ${pos.symbol} hit +500% — selling {sell_fraction*100:.0f}% (20% moonbag remains)")
                asyncio.create_task(self._persist_tp_state(pos))
                asyncio.create_task(self._notify_tp_hit(pos, "TP3", return_pct, sell_fraction, current_price))
                # v2.1: DO NOT close position here. Sisa 20% moonbag tetap aktif & dikawal Section 10 (Tiered Trailing Stop).

            # ── 10. Tiered Trailing Stop ──
            # v2.2: Active once token ATH return >= trailing_start_return_pct (+30%), protecting runners
            trailing_start = FROZEN_PARAMS.get("trailing_start_return_pct", 30.0)
            ath_return_pct = ((pos.price_high_ever_seen - entry) / entry) * 100.0 if entry > 0 else 0.0

            if ath_return_pct >= trailing_start:
                drop_from_ath = ((current_price - pos.price_high_ever_seen) / pos.price_high_ever_seen) * 100.0 if pos.price_high_ever_seen > 0 else 0.0

                # Determine trailing tier based on ATH return
                t1_max = FROZEN_PARAMS["trailing_tier1_max_return"]
                t2_max = FROZEN_PARAMS["trailing_tier2_max_return"]
                if ath_return_pct <= t1_max:
                    trailing_pct = FROZEN_PARAMS["trailing_tier1_pct"]   # 20%
                elif ath_return_pct <= t2_max:
                    trailing_pct = FROZEN_PARAMS["trailing_tier2_pct"]   # 35%
                else:
                    trailing_pct = FROZEN_PARAMS["trailing_tier3_pct"]   # 45%

                if drop_from_ath <= -trailing_pct:
                    logger.info(
                        f"🌙 [TRAILING] ${pos.symbol} tiered trailing triggered — "
                        f"ATH: +{ath_return_pct:.0f}%, dropped {drop_from_ath:.0f}% from ATH "
                        f"(tier: {trailing_pct:.0f}% trailing)"
                    )
                    await self._close_position(pos, current_price, "TRAILING")
                    return

        except Exception as e:
            logger.debug(f"[PositionTracker] Evaluate error for {pos.symbol}: {e}")

    # ──────────────────────────────────────────
    # Position lifecycle helpers
    # ──────────────────────────────────────────

    async def _close_position(self, pos: ActivePosition, exit_price: float, reason: str) -> None:
        """Close position: compute P&L metrics with v2.1 blended weighted TP accounting and persist to DB."""
        # Remove from active tracking first (prevent duplicate closes)
        if pos.position_id not in self._active:
            return
        del self._active[pos.position_id]

        now_utc = datetime.now(tz=timezone.utc)
        entry = pos.entry_price_usd

        # Compute return on the final exit tranche
        final_tranche_return = ((exit_price - entry) / entry) * 100.0 if entry > 0 else 0.0

        # ── v2.1: Blended Weighted Return Accounting ──
        # Sum up profits from partial TP milestones already locked in
        tp_weighted_pct = 0.0
        if pos.tp0_hit:
            tp_weighted_pct += FROZEN_PARAMS["tp0_sell_fraction"] * FROZEN_PARAMS["tp0_pct"]
        if pos.tp1_hit:
            tp_weighted_pct += FROZEN_PARAMS["tp1_sell_fraction"] * FROZEN_PARAMS["tp1_pct"]
        if pos.tp2_hit:
            tp_weighted_pct += FROZEN_PARAMS["tp2_sell_fraction"] * FROZEN_PARAMS["tp2_pct"]
        if pos.tp3_hit:
            tp_weighted_pct += FROZEN_PARAMS["tp3_sell_fraction"] * FROZEN_PARAMS["tp3_pct"]

        # Remaining bag fraction closes at final_tranche_return
        rem_fraction = max(pos.remaining_fraction, 0.0)
        realized_return_pct = tp_weighted_pct + (rem_fraction * final_tranche_return)

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

        status_emoji = {
            "SL": "🛑", "TP0": "✅", "TP1": "✅", "TP2": "💚", "TP3": "💎",
            "TRAILING": "🌙", "TIMEOUT_2H": "⌛", "TIME_DECAY": "⏳",
            "RUG_DETECTED": "🚨",
        }.get(reason, "📋")
        tp_tag = f" (Blended: {realized_return_pct:+.1f}%, final tranche: {final_tranche_return:+.1f}%)" if pos.tp0_hit else f"Return: {realized_return_pct:+.1f}%"
        logger.info(
            f"{status_emoji} [Closed {reason}] ${pos.symbol} | "
            f"{tp_tag} | MFE: {mfe_pct:+.1f}% | "
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

    async def _persist_tp_state(self, pos: "ActivePosition") -> None:
        """Persist current TP milestone state to DB immediately after a TP hit or state change.
        Prevents double-execution of TPs after bot restart. Also persists breakeven state."""
        try:
            await db_manager.update(
                "paper_trade_positions",
                {
                    "tp0_hit": pos.tp0_hit,
                    "tp1_hit": pos.tp1_hit,
                    "tp2_hit": pos.tp2_hit,
                    "tp3_hit": pos.tp3_hit,
                    "remaining_fraction": round(pos.remaining_fraction, 4),
                    "breakeven_sl_active": pos.breakeven_sl_active,
                    "updated_at": datetime.now(tz=timezone.utc).isoformat(),
                },
                filters={"id": f"eq.{pos.position_id}"}
            )
            logger.debug(
                f"[PositionTracker] TP state persisted for {pos.symbol}: "
                f"TP0={pos.tp0_hit}, TP1={pos.tp1_hit}, TP2={pos.tp2_hit}, TP3={pos.tp3_hit}, "
                f"breakeven={pos.breakeven_sl_active}, remaining={pos.remaining_fraction:.2f}"
            )
        except Exception as e:
            logger.warning(f"[PositionTracker] TP state persist failed for {pos.position_id[:8]}: {e}")

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

                # ── Recover persisted TP milestone state (v2.0: includes tp0, breakeven, liquidity) ──
                recovered_tp0  = bool(row.get("tp0_hit", False))
                recovered_tp1  = bool(row.get("tp1_hit", False))
                recovered_tp2  = bool(row.get("tp2_hit", False))
                recovered_tp3  = bool(row.get("tp3_hit", False))
                recovered_fraction   = float(row.get("remaining_fraction", 1.0) or 1.0)
                recovered_breakeven  = bool(row.get("breakeven_sl_active", False))
                recovered_liquidity  = float(row.get("liquidity_at_entry_usd", 0.0) or 0.0)

                if recovered_tp0 or recovered_tp1 or recovered_tp2 or recovered_tp3 or recovered_breakeven:
                    logger.info(
                        f"🔄 [PositionTracker] Recovered state for {row.get('symbol', 'UNKNOWN')}: "
                        f"TP0={recovered_tp0}, TP1={recovered_tp1}, TP2={recovered_tp2}, TP3={recovered_tp3}, "
                        f"breakeven={recovered_breakeven}, remaining_fraction={recovered_fraction:.2f}"
                    )

                pos = ActivePosition(
                    position_id=pos_id,
                    token_address=pos_token_addr,
                    symbol=row.get("symbol", "UNKNOWN"),
                    signal_source=row.get("signal_source", "PINTU_A"),
                    entry_price_usd=pos_entry_price,
                    entry_time=entry_time,
                    position_size_usd=row.get("position_size_usd", 2.0) or 2.0,
                    price_high_ever_seen=row.get("price_high_ever_seen", 0.0) or 0.0,
                    # Restored from DB — prevents double-execution after restart
                    tp0_hit=recovered_tp0,
                    tp1_hit=recovered_tp1,
                    tp2_hit=recovered_tp2,
                    tp3_hit=recovered_tp3,
                    remaining_fraction=recovered_fraction,
                    breakeven_sl_active=recovered_breakeven,
                    liquidity_at_entry_usd=recovered_liquidity,
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
        self, pos: ActivePosition, tier: str, return_pct: float, sell_fraction: float,
        current_price: Optional[float] = None
    ) -> None:
        try:
            from src.paper_trading.telegram_notifier import telegram_notifier
            # Use actual current_price if provided (more accurate than back-calculating from pct)
            exit_price = current_price if current_price and current_price > 0 else (
                pos.entry_price_usd * (1.0 + return_pct / 100.0)
            )
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
            elif reason == "TIMEOUT_2H":
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
            elif reason == "TIME_DECAY":
                # v2.0: Time-decay replaces stagnancy
                await telegram_notifier.send_time_decay_exit(
                    symbol=pos.symbol,
                    token_address=pos.token_address,
                    return_pct=realized_return_pct,
                    mfe_pct=mfe_pct,
                    hold_minutes=(datetime.now(tz=timezone.utc) - pos.entry_time).total_seconds() / 60.0,
                    entry_price=pos.entry_price_usd,
                    exit_price=exit_price,
                    entry_mcap=pos.entry_market_cap_usd,
                    exit_mcap=exit_mcap,
                    opportunity_score=pos.opportunity_score,
                    signal_source=pos.signal_source,
                )
            elif reason == "RUG_DETECTED":
                # v2.0: Rug guard triggered
                await telegram_notifier.send_rug_detected(
                    symbol=pos.symbol,
                    token_address=pos.token_address,
                    return_pct=realized_return_pct,
                    mfe_pct=mfe_pct,
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
