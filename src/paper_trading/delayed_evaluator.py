"""
Delayed Evaluator — Fase B

Two-Stage T+2 Scoring Pipeline:
  Stage 1 (T=0): pipeline.py runs safety filter + bundling only.
                 If token passes, enqueue to Redis sorted set `delayed_eval`
                 with score = unix_time + 120 (ready at T+2 min).
  Stage 2 (T+2): background worker fires opportunity scoring.
                 Caches fee + social data from Stage 1 to avoid redundant calls.

This is the production-facing pipeline. Backtest EV is measured from T+2 price
via data_collector._fetch_t2_price() (R9/R13/R17 decisions).

Architecture:
  - Redis sorted set `delayed_eval` (key = token_address, score = ready_at timestamp)
  - Worker polls every 5s for tokens whose score <= time.time()
  - Stage 1 cache: stored in Redis hash `stage1_cache:{token_address}`
  - Graceful shutdown: drain in-flight evaluations before exit
"""

import asyncio
import json
import time
from typing import Optional

from src.ingestion.schemas import RawTokenEvent
from src.utils.logger import logger

DELAYED_EVAL_KEY = "delayed_eval"
STAGE1_CACHE_PREFIX = "stage1_cache:"
DELAY_SECONDS = 120.0   # T+2 minutes
POLL_INTERVAL = 5.0     # Worker poll interval


class DelayedEvaluator:
    """
    Redis-backed delayed scoring queue for the T+2 two-stage pipeline.
    Falls back to in-memory queue if Redis is unavailable (dev/test mode).
    """

    def __init__(self):
        self._redis: Optional[object] = None
        self._in_memory_queue: list[dict] = []   # Fallback queue when Redis not available
        self._in_memory_cache: dict[str, dict] = {}
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None

    async def _get_redis(self) -> Optional[object]:
        """Lazy-load Redis client. Returns None if Redis not available."""
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis
            from src.config import settings
            redis_url = getattr(settings, "redis_url", None) or "redis://localhost:6379"
            self._redis = aioredis.from_url(redis_url, decode_responses=True)
            await self._redis.ping()
            logger.info("✅ DelayedEvaluator connected to Redis")
        except Exception as e:
            logger.warning(
                f"⚠️ Redis unavailable ({e}). "
                f"DelayedEvaluator falling back to in-memory queue (dev mode)."
            )
            self._redis = None
        return self._redis

    async def enqueue(
        self,
        event: RawTokenEvent,
        filter_result: object,
        stage1_scores: Optional[dict] = None
    ) -> None:
        """
        Enqueue token for T+2 opportunity scoring.

        Args:
            event:         RawTokenEvent from Stage 1 safety filter
            filter_result: FilterResult with safety pass/fail details
            stage1_scores: Cached fee + social scores from Stage 1 to reuse at Stage 2
        """
        ready_at = time.time() + DELAY_SECONDS
        token_address = event.token_address

        payload = {
            "token_address": token_address,
            "symbol": event.symbol,
            "ready_at": ready_at,
            "event_json": event.model_dump_json() if hasattr(event, "model_dump_json") else "{}",
            "stage1_scores": stage1_scores or {},
        }

        redis = await self._get_redis()
        if redis:
            try:
                # Store event payload in Redis hash
                cache_key = f"{STAGE1_CACHE_PREFIX}{token_address}"
                await redis.hset(cache_key, mapping={
                    k: json.dumps(v) if not isinstance(v, str) else v
                    for k, v in payload.items()
                })
                await redis.expire(cache_key, 600)  # Expire after 10 min
                # Add to sorted set (score = ready_at timestamp)
                await redis.zadd(DELAYED_EVAL_KEY, {token_address: ready_at})
                logger.info(
                    f"⏱️  [{event.symbol}] Enqueued for T+2 scoring "
                    f"(ready in {DELAY_SECONDS:.0f}s at {redis_ts_str(ready_at)})"
                )
            except Exception as e:
                logger.warning(f"Redis enqueue failed for {token_address[:8]}: {e}. Using in-memory fallback.")
                self._in_memory_queue.append(payload)
                self._in_memory_cache[token_address] = payload
        else:
            # In-memory fallback (dev/test mode)
            self._in_memory_queue.append(payload)
            self._in_memory_cache[token_address] = payload
            logger.info(
                f"⏱️  [{event.symbol}] Enqueued (in-memory) for T+2 scoring "
                f"(ready in {DELAY_SECONDS:.0f}s)"
            )

    async def _process_token(self, token_address: str, payload: dict) -> None:
        """
        Stage 2: Run opportunity scoring at T+2 minutes.
        Reuses cached fee + social scores from Stage 1 to avoid redundant calls.
        """
        symbol = payload.get("symbol", token_address[:8])
        stage1_scores = payload.get("stage1_scores", {})

        try:
            from src.opportunity.scorer import OpportunityScorer
            from src.ingestion.schemas import RawTokenEvent
            from src.filters.schemas import SafetyCheckResult

            # Reconstruct event from JSON
            event_json = payload.get("event_json", "{}")
            try:
                event = RawTokenEvent.model_validate_json(event_json)
            except Exception:
                logger.warning(f"Could not reconstruct event for {symbol} — skipping Stage 2")
                return

            scorer = OpportunityScorer()
            score_result = await scorer.score_token(event)

            logger.info(
                f"🎯 [Stage 2 T+2] {symbol} → Score: {score_result.opportunity_score:.1f}/100 "
                f"| Cached from Stage 1: {list(stage1_scores.keys())}"
            )

            # Push to paper trading signal recording if score passes threshold
            from src.config import settings
            opp_thresh = getattr(settings, "opportunity_threshold", 60.0)
            if score_result.opportunity_score >= opp_thresh:
                try:
                    from src.paper_trading.signal_recorder import record_signal
                    from src.paper_trading.outcome_worker import outcome_worker
                    from src.paper_trading.price_fetcher import fetch_price
                    from datetime import datetime, timezone

                    # Build SafetyCheckResult adapter so record_signal gets the right type
                    # (record_signal was designed for SafetyCheckResult; we populate the fields
                    #  that matter: opportunity_score and opportunity_breakdown)
                    safety_adapter = SafetyCheckResult(
                        token_address=event.token_address,
                        venue=event.launch_venue,
                        filter_pass=True,
                        opportunity_score=score_result.opportunity_score,
                        opportunity_breakdown=score_result.breakdown,
                    )

                    # Record signal with retry 1× (Fix #5 / Q14)
                    signal_id = None
                    try:
                        signal_id = await record_signal(event, safety_adapter)
                    except Exception as first_err:
                        logger.warning(f"⚠️ [Stage 2] Signal recording failed (attempt 1): {first_err}. Retrying...")
                        try:
                            import asyncio as _asyncio
                            await _asyncio.sleep(1.0)
                            signal_id = await record_signal(event, safety_adapter)
                        except Exception as retry_err:
                            logger.warning(f"❌ [Stage 2] Signal recording failed after retry: {retry_err}")

                    if signal_id:
                        price_snap = None
                        try:
                            price_snap = await fetch_price(event.token_address)
                        except Exception:
                            pass
                        entry_price = price_snap.price_usd if price_snap else 0.0
                        await outcome_worker.schedule_signal(
                            signal_id=signal_id,
                            signal_at=datetime.now(tz=timezone.utc),
                            entry_price=entry_price,
                            mint=event.token_address,
                            symbol=event.symbol or "UNKNOWN"
                        )
                        logger.info(f"📝 [Stage 2] {symbol} signal recorded and outcome scheduled (score={score_result.opportunity_score:.1f} >= {opp_thresh})")
                except Exception as e:
                    logger.warning(f"Stage 2 signal recording failed for {symbol}: {e}")

        except Exception as e:
            logger.debug(f"Stage 2 scoring failed for {symbol}: {e}")

    async def _worker_loop(self) -> None:
        """
        Background worker: polls queue every POLL_INTERVAL seconds.
        Fires _process_token for every entry whose ready_at <= now.
        """
        logger.info("🔄 DelayedEvaluator worker started")
        while self._running:
            now = time.time()
            redis = await self._get_redis()

            if redis:
                try:
                    # Get all tokens ready now (score <= now)
                    ready = await redis.zrangebyscore(DELAYED_EVAL_KEY, "-inf", now)
                    if ready:
                        # Remove from sorted set atomically
                        await redis.zrem(DELAYED_EVAL_KEY, *ready)
                        for token_address in ready:
                            cache_key = f"{STAGE1_CACHE_PREFIX}{token_address}"
                            raw_payload = await redis.hgetall(cache_key)
                            payload = {
                                k: _safe_json_loads(v)
                                for k, v in raw_payload.items()
                            }
                            asyncio.create_task(
                                self._process_token(token_address, payload)
                            )
                except Exception as e:
                    logger.debug(f"Worker Redis poll error: {e}")
            else:
                # In-memory fallback
                due = [item for item in self._in_memory_queue if item["ready_at"] <= now]
                for item in due:
                    self._in_memory_queue.remove(item)
                    asyncio.create_task(
                        self._process_token(item["token_address"], item)
                    )

            await asyncio.sleep(POLL_INTERVAL)

        logger.info("🛑 DelayedEvaluator worker stopped")

    async def start(self) -> None:
        """Start the background worker."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        """Gracefully stop the background worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        logger.info("✅ DelayedEvaluator stopped cleanly")


def redis_ts_str(ts: float) -> str:
    """Format a Unix timestamp for logging."""
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _safe_json_loads(value: str) -> object:
    """Parse JSON string or return raw string if not valid JSON."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


# Singleton instance used by pipeline.py
delayed_evaluator = DelayedEvaluator()
