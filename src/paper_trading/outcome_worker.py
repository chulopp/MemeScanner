"""
Outcome Worker — Fase 5
APScheduler-based background worker for multi-timeframe outcome resolution.
Tracks ATH (All-Time High) and MAE (Maximum Adverse Excursion) per active signal,
then resolves outcomes at each window: 5m, 15m, 1h, 4h, 24h.
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from src.database.client import db_manager
from src.paper_trading.price_fetcher import fetch_price
from src.paper_trading.telegram_notifier import telegram_notifier
from src.utils.logger import logger

# Guard import
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    AsyncIOScheduler = None

# Label thresholds — HYPOTHESIS_INIT
RUNNER_THRESHOLD_PCT = 100.0
DEAD_THRESHOLD_PCT = -70.0
DEAD_LIQUIDITY_FLOOR = 500.0

RESOLUTION_WINDOWS = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "24h": timedelta(hours=24),
}

# In-memory ATH / MAE tracking per signal_id
_signal_tracking: dict[str, dict] = {}
# Format: { signal_id: { "ath": float, "mae_pct": float, "entry_price": float, "mint": str, "symbol": str } }


def _classify_outcome(return_pct: float, liquidity_usd: float) -> str:
    if liquidity_usd < DEAD_LIQUIDITY_FLOOR or return_pct <= DEAD_THRESHOLD_PCT:
        return "dead"
    elif return_pct >= RUNNER_THRESHOLD_PCT:
        return "runner"
    return "neutral"


class OutcomeWorker:
    """APScheduler-based worker for ATH tracking and window resolution."""

    def __init__(self):
        self._scheduler: Optional[object] = None

    def is_available(self) -> bool:
        return APSCHEDULER_AVAILABLE

    async def start(self):
        if not APSCHEDULER_AVAILABLE:
            logger.warning("⚠️ APScheduler not installed. Outcome worker disabled. Run: pip install APScheduler")
            return

        self._scheduler = AsyncIOScheduler()

        # ATH tracker: poll every 30 seconds for all active signals
        self._scheduler.add_job(
            self._track_ath_tick,
            trigger=IntervalTrigger(seconds=30),
            id="ath_tracker",
            replace_existing=True,
            max_instances=1
        )

        self._scheduler.start()
        logger.info("🕐 Outcome Worker (APScheduler) started — ATH tracker running every 30s")

        # Recover pending signals from DB on startup
        await self._recover_pending_signals()

    async def stop(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            logger.info("Outcome Worker stopped.")
        _signal_tracking.clear()

    async def schedule_signal(self, signal_id: str, signal_at: datetime, entry_price: float, mint: str, symbol: str):
        """Schedules window resolution jobs for a newly recorded signal."""
        if not self._scheduler:
            return

        # Register in ATH/MAE tracker
        _signal_tracking[signal_id] = {
            "ath": entry_price,
            "mae_pct": 0.0,
            "entry_price": entry_price,
            "mint": mint,
            "symbol": symbol
        }

        for window_name, delta in RESOLUTION_WINDOWS.items():
            run_at = signal_at + delta
            job_id = f"resolve_{signal_id}_{window_name}"

            self._scheduler.add_job(
                self._resolve_window,
                trigger=DateTrigger(run_date=run_at),
                args=[signal_id, window_name],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300  # 5 min grace for misfired jobs after restart
            )

        logger.info(f"⏱ Scheduled 5 resolution windows for signal {signal_id[:8]}... ({symbol})")

    async def _get_paper_trade_status(self, token_address: str, signal_id: str) -> Optional[dict]:
        """Checks if a signal was followed in paper_trade_positions and retrieves its execution status."""
        try:
            trades = []
            if signal_id:
                try:
                    import uuid as _uuid
                    _uuid.UUID(str(signal_id))
                    trades = await db_manager.query("paper_trade_positions", filters={"paper_signal_id": f"eq.{signal_id}"}, limit=1)
                except (ValueError, TypeError):
                    trades = []

            if not trades and token_address:
                trades = await db_manager.query("paper_trade_positions", filters={"token_address": f"eq.{token_address}"}, limit=1)
            if not trades:
                return None

            trade = trades[0]
            skipped_reason = trade.get("skipped_reason")
            exit_reason = trade.get("exit_reason")
            score = float(trade.get("opportunity_score_at_entry", 0.0) or 0.0)
            source = trade.get("signal_source", "PINTU_A")

            if skipped_reason:
                return {
                    "status": "SKIPPED",
                    "reason": skipped_reason,
                    "score": score,
                    "source": source
                }
            elif exit_reason == "OPEN":
                return {
                    "status": "OPEN",
                    "entry_price": float(trade.get("entry_price_usd", 0.0) or 0.0),
                    "score": score,
                    "source": source
                }
            else:
                exit_p = float(trade.get("exit_price_usd", 0.0) or 0.0)
                exit_mc = exit_p * 1_000_000_000 if token_address.endswith("pump") and exit_p > 0 else 0.0
                return {
                    "status": "CLOSED",
                    "exit_reason": exit_reason,
                    "return_pct": float(trade.get("realized_return_pct", 0.0) or 0.0),
                    "exit_price": exit_p,
                    "exit_mcap": exit_mc,
                    "score": score,
                    "source": source
                }
        except Exception as e:
            logger.debug(f"Error checking paper trade status for {token_address[:8]}: {e}")
            return None

    async def _track_ath_tick(self):
        """Called every 30 seconds — polls price for all active signals concurrently (semaphore=3) and updates ATH/MAE."""
        if not _signal_tracking:
            return

        sem = asyncio.Semaphore(3)

        async def _check_one(signal_id: str, tracking: dict):
            mint = tracking.get("mint")
            entry_price = tracking.get("entry_price", 0.0)
            if not mint or entry_price <= 0:
                return

            async with sem:
                try:
                    snap = await fetch_price(mint)
                    if not snap or snap.price_usd <= 0:
                        return

                    current_price = snap.price_usd
                    if current_price > tracking.get("ath", entry_price):
                        tracking["ath"] = current_price

                    drawdown_pct = ((current_price - entry_price) / entry_price) * 100.0
                    if drawdown_pct < tracking.get("mae_pct", 0.0):
                        tracking["mae_pct"] = drawdown_pct
                except Exception as poll_err:
                    logger.debug(f"ATH tick error for {mint[:8]}: {poll_err}")

        tasks = [
            _check_one(s_id, tr)
            for s_id, tr in list(_signal_tracking.items())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _resolve_window(self, signal_id: str, window_name: str):
        """Resolves a single outcome window for a signal."""
        tracking = _signal_tracking.get(signal_id)
        if not tracking:
            logger.debug(f"No tracking data for signal {signal_id[:8]} at window {window_name}")
            return

        mint = tracking["mint"]
        symbol = tracking["symbol"]
        entry_price = tracking["entry_price"]
        ath = tracking["ath"]
        mae_pct = tracking["mae_pct"]

        # Fetch current price at resolution time
        snap = await fetch_price(mint)
        current_price = snap.price_usd if snap else 0.0
        current_liq = snap.liquidity_usd if snap else 0.0

        if entry_price > 0 and current_price > 0:
            return_pct = ((current_price - entry_price) / entry_price) * 100.0
        elif entry_price > 0:
            return_pct = -100.0
        else:
            return_pct = 0.0

        # CRITICAL FIX: Ensure ATH reflects current price if current price is higher than tracked ATH
        if current_price > ath:
            ath = current_price
            tracking["ath"] = ath

        ath_return_pct = ((ath - entry_price) / entry_price) * 100.0 if entry_price > 0 else 0.0

        # CRITICAL FIX: Ensure MAE reflects current price drawdown if worse than tracked MAE
        if entry_price > 0 and current_price > 0:
            cur_drawdown = ((current_price - entry_price) / entry_price) * 100.0
            if cur_drawdown < mae_pct:
                mae_pct = cur_drawdown
                tracking["mae_pct"] = mae_pct
        elif return_pct <= -70.0:
            mae_pct = return_pct
            tracking["mae_pct"] = mae_pct

        status = _classify_outcome(return_pct, current_liq)

        # Insert outcome record
        outcome_record = {
            "signal_id": signal_id,
            "token_address": mint,
            "time_window": window_name,
            "resolved_at": datetime.now(tz=timezone.utc).isoformat(),
            "price_at_window": current_price,
            "return_pct": round(return_pct, 4),
            "ath_since_signal": ath,
            "ath_return_pct": round(ath_return_pct, 4),
            "mae_pct": round(abs(mae_pct), 4),
            "liquidity_at_window": current_liq,
            "status": status
        }

        try:
            await db_manager.upsert("signal_outcomes", outcome_record, on_conflict="signal_id,time_window")

            # Mark window as resolved in paper_signals
            resolved_col = f"resolved_{window_name.replace('m', 'm').replace('h', 'h')}"
            await db_manager.update(
                "paper_signals",
                {resolved_col: True},
                filters={"id": f"eq.{signal_id}"}
            )

            status_emoji = {"runner": "🚀", "dead": "💀", "neutral": "➖"}.get(status, "❓")
            logger.info(
                f"{status_emoji} [Resolved {window_name}] {symbol} ({mint[:8]}...) | "
                f"Return: {return_pct:+.1f}% | ATH: {ath_return_pct:+.1f}% | MAE: {abs(mae_pct):.1f}% | {status.upper()}"
            )

            # Check paper trading execution correlation
            paper_trade_info = await self._get_paper_trade_status(mint, signal_id)

            entry_mcap = entry_price * 1_000_000_000 if mint.endswith("pump") and entry_price > 0 else 0.0
            cur_mcap = current_price * 1_000_000_000 if mint.endswith("pump") and current_price > 0 else 0.0
            ath_mcap = ath * 1_000_000_000 if mint.endswith("pump") and ath > 0 else 0.0

            # Send Telegram outcome update for key windows (1h, 4h, 24h)
            if window_name in ("1h", "4h", "24h"):
                await telegram_notifier.send_outcome_update(
                    symbol=symbol,
                    token_address=mint,
                    time_window=window_name,
                    return_pct=return_pct,
                    ath_return_pct=ath_return_pct,
                    mae_pct=abs(mae_pct),
                    status=status,
                    paper_trade_info=paper_trade_info,
                    entry_price=entry_price,
                    current_price=current_price,
                    ath_price=ath,
                    entry_mcap=entry_mcap,
                    current_mcap=cur_mcap,
                    ath_mcap=ath_mcap,
                )

        except Exception as e:
            logger.error(f"❌ Failed to resolve {window_name} for signal {signal_id[:8]}: {e}")

        # Clean up tracking after 24h window
        if window_name == "24h":
            _signal_tracking.pop(signal_id, None)
            logger.info(f"🏁 Signal {signal_id[:8]} ({symbol}) fully resolved. Removed from active tracking.")

    async def _recover_pending_signals(self):
        """On startup, recover unresolved signals and reschedule remaining windows with historical ATH preserved."""
        try:
            rows = await db_manager.query(
                "paper_signals",
                filters={"resolved_24h": "eq.false"},
                limit=500
            )
            if not rows:
                logger.info("ℹ️ No pending signals to recover.")
                return

            recovered = 0
            now = datetime.now(tz=timezone.utc)

            for row in rows:
                signal_id = row.get("id")
                mint = row.get("token_address", "")
                symbol = row.get("symbol", "UNKNOWN")
                entry_price = float(row.get("entry_price_usd", 0.0) or 0.0)
                signal_at_str = row.get("signal_at", "")

                if not signal_id or not signal_at_str:
                    continue

                if isinstance(signal_at_str, str):
                    signal_at = datetime.fromisoformat(signal_at_str.replace("Z", "+00:00"))
                else:
                    signal_at = signal_at_str

                # Recover best previous ATH and worst MAE from earlier resolved windows in DB
                best_ath = entry_price
                worst_mae = 0.0
                try:
                    outcomes = await db_manager.query(
                        "signal_outcomes",
                        filters={"signal_id": f"eq.{signal_id}"},
                        limit=10
                    )
                    for oc in outcomes:
                        oc_ath = float(oc.get("ath_since_signal", 0.0) or 0.0)
                        if oc_ath > best_ath:
                            best_ath = oc_ath
                        oc_mae = float(oc.get("mae_pct", 0.0) or 0.0)
                        if -abs(oc_mae) < worst_mae:
                            worst_mae = -abs(oc_mae)
                except Exception:
                    pass

                # Register in tracker with recovered ATH / MAE
                _signal_tracking[signal_id] = {
                    "ath": best_ath,
                    "mae_pct": worst_mae,
                    "entry_price": entry_price,
                    "mint": mint,
                    "symbol": symbol
                }

                # Schedule only unresolved windows
                for window_name, delta in RESOLUTION_WINDOWS.items():
                    resolved_col = f"resolved_{window_name.replace('m', 'm').replace('h', 'h')}"
                    if row.get(resolved_col, False):
                        continue

                    run_at = signal_at + delta
                    if run_at < now:
                        # Window already passed — resolve immediately
                        asyncio.create_task(self._resolve_window(signal_id, window_name))
                    else:
                        job_id = f"resolve_{signal_id}_{window_name}"
                        self._scheduler.add_job(
                            self._resolve_window,
                            trigger=DateTrigger(run_date=run_at),
                            args=[signal_id, window_name],
                            id=job_id,
                            replace_existing=True,
                            misfire_grace_time=300
                        )

                recovered += 1

            if recovered > 0:
                logger.info(f"🔄 Recovered {recovered} pending signals from DB (ATH history preserved) and rescheduled resolution windows.")

        except Exception as e:
            logger.error(f"Error recovering pending signals: {e}")


outcome_worker = OutcomeWorker()
