"""
Replay Engine — Fase 4
Feeds historical tokens from `backtest_tokens` through the existing
scoring engines in offline mode (no live RPC calls for filters).

RPC-dependent filter steps (deployer history, ATA resolution, bundling graph)
are skipped in offline mode — this is a known limitation documented in the report.
The safety check is simplified: only static threshold checks available from
DexScreener data (liquidity thresholds, volume proxy checks).
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import patch

from src.database.client import db_manager
from src.ingestion.schemas import RawTokenEvent
from src.opportunity.scorer import OpportunityScorer, OpportunityScoreResult
from src.backtest.cost_model import CostModelConfig, compute_trade_cost, load_p80_priority_fee_from_supabase
from src.backtest.metrics import BacktestSignal, BacktestMetrics, compute_metrics
from src.utils.logger import logger


def _offline_safety_check(row: dict) -> tuple[bool, Optional[str]]:
    """
    Simplified offline safety check using only DexScreener static data.
    Skips all RPC-dependent checks (deployer history, ATA resolution, funding graph).
    Returns (passed, rejection_reason).

    LIMITATION: This is a proxy. Production filter uses more signals.
    Parameters tagged HYPOTHESIS_INIT can be adjusted in optimizer.
    """
    liquidity_usd = row.get("liquidity_usd") or 0.0
    volume_24h = row.get("volume_24h_usd") or 0.0

    # Reject if liquidity is effectively zero (likely dead/rug) — HYPOTHESIS_INIT threshold
    if liquidity_usd < 1_000.0:
        return False, "OFFLINE:ZERO_LIQUIDITY"

    # Reject if 24h volume >> liquidity (wash trade proxy) — HYPOTHESIS_INIT
    if volume_24h > 0 and (volume_24h / max(liquidity_usd, 1)) > 50.0:
        return False, "OFFLINE:WASH_TRADE_PROXY"

    return True, None


def _build_raw_token_event(row: dict) -> Optional[RawTokenEvent]:
    """Reconstruct a RawTokenEvent from a backtest_tokens Supabase row."""
    try:
        raw = row.get("raw_dexscreener") or {}
        base_token = raw.get("baseToken") or {}
        token_address = row["token_address"]

        listed_at = row.get("listed_at")
        if isinstance(listed_at, str):
            ts = datetime.fromisoformat(listed_at.replace("Z", "+00:00"))
        else:
            ts = datetime.now(tz=timezone.utc)

        return RawTokenEvent(
            token_address=token_address,
            symbol=(row.get("symbol") or base_token.get("symbol") or "UNKNOWN")[:20],
            name=(row.get("name") or base_token.get("name") or "")[:60],
            deployer_wallet_address="OFFLINE_BACKTEST_PLACEHOLDER",
            launch_venue=row.get("launch_venue", "pump_fun"),
            initial_buy_amount=0.0,
            total_supply=1_000_000_000.0,
            timestamp=ts
        )
    except Exception as e:
        logger.debug(f"Failed to reconstruct event for {row.get('token_address', '?')[:8]}: {e}")
        return None


async def run_replay(
    opportunity_threshold: float = 60.0,  # HYPOTHESIS_INIT
    weight_overrides: Optional[dict] = None,
    limit: int = 500
) -> BacktestMetrics:
    """
    Main replay function. Processes labeled backtest_tokens through the existing
    scoring engines and returns BacktestMetrics.

    Args:
        opportunity_threshold: Score cutoff for opportunity classification (HYPOTHESIS_INIT)
        weight_overrides: Optional dict of {component_name: weight} to override base weights
                          Keys: vol_velocity, smart_money, global_fee
        limit: Max tokens to process in this run

    Returns:
        BacktestMetrics with filter precision, recall, and EV per trade
    """
    logger.info(f"▶️  Starting replay engine (threshold={opportunity_threshold}, limit={limit})")

    rows = await db_manager.query(
        "backtest_tokens",
        filters={"label": "not.is.null"},
        limit=limit
    )

    if not rows:
        logger.warning("No labeled tokens found in backtest_tokens. Run `collect` + `label` first.")
        return compute_metrics([], opportunity_threshold)

    logger.info(f"Loaded {len(rows)} labeled tokens for replay")

    # Load P80 priority fee from actual Supabase data
    p80_fee_sol = await load_p80_priority_fee_from_supabase()
    cost_config = CostModelConfig(priority_fee_sol=p80_fee_sol)
    logger.info(f"💸 Cost model: P80 priority fee = {p80_fee_sol:.5f} SOL")

    scorer = OpportunityScorer()
    signals: list[BacktestSignal] = []
    processed = 0

    # Build settings overrides for weight injection
    settings_patches: dict = {}
    if weight_overrides:
        if "vol_velocity" in weight_overrides:
            settings_patches["score_w_vol_velocity"] = weight_overrides["vol_velocity"]
        if "smart_money" in weight_overrides:
            settings_patches["score_w_smart_money"] = weight_overrides["smart_money"]
        if "global_fee" in weight_overrides:
            settings_patches["score_w_global_fee"] = weight_overrides["global_fee"]

    for row in rows:
        event = _build_raw_token_event(row)
        if not event:
            continue

        label = row.get("label", "neutral")
        label_return_pct = row.get("label_return_pct", 0.0) or 0.0
        liquidity_usd = row.get("liquidity_usd", 0.0) or 0.0

        # --- Offline Safety Check ---
        passed_safety, rejection_reason = _offline_safety_check(row)

        # --- Opportunity Score ---
        opp_score: Optional[float] = None
        if passed_safety:
            try:
                if settings_patches:
                    with patch.multiple("src.opportunity.scorer.settings", **settings_patches):
                        score_result: OpportunityScoreResult = await scorer.score_token(event)
                else:
                    score_result = await scorer.score_token(event)
                opp_score = score_result.opportunity_score
            except Exception as e:
                logger.debug(f"Scorer error for {event.token_address[:8]}: {e}")

        # --- Cost Model ---
        trade_cost = compute_trade_cost(liquidity_usd, cost_config)

        signals.append(BacktestSignal(
            token_address=event.token_address,
            symbol=event.symbol,
            passed_safety=passed_safety,
            opportunity_score=opp_score or 0.0,
            label=label,
            label_return_pct=label_return_pct,
            liquidity_usd=liquidity_usd,
            total_cost_pct=trade_cost.total_cost_pct,
            rejection_reason=rejection_reason
        ))

        processed += 1
        if processed % 50 == 0:
            logger.info(f"▶️  Processed {processed}/{len(rows)} tokens in replay...")

    logger.info(f"✅ Replay complete: {processed} tokens processed")
    metrics = compute_metrics(signals, opportunity_threshold)
    logger.info(
        f"📊 Results: Filter Precision={metrics.filter_precision:.1%} | "
        f"Opp Recall={metrics.opportunity_recall:.1%} | "
        f"EV/Trade={metrics.ev_per_trade:+.2f}% | "
        f"EV Positive: {'✅' if metrics.ev_positive else '❌'}"
    )
    return metrics
