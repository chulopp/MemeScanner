from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.filters.schemas import SafetyCheckResult
from src.filters.instant_scalp import instant_scalp_filter
from src.database.client import db_manager
from src.utils.logger import logger


# Fase C (Q13): Deployer profiling gate thresholds
# Reject only when evidence is strong enough to avoid false positives
DEPLOYER_MIN_LAUNCHES = 3       # Need at least 3 launches before judging
DEPLOYER_MAX_WIN_RATE = 10.0   # Serial rugger: < 10% win rate


class PumpSafetyFilter:
    """Hard safety filter tailored for Pump.fun bonding curve tokens."""

    async def evaluate(self, event: RawTokenEvent) -> SafetyCheckResult:
        rejections = []
        total_supply = event.total_supply if event.total_supply > 0 else 1_000_000_000.0
        dev_buy_pct = (event.initial_buy_amount / total_supply) * 100.0

        # 1. Check Dev Initial Holding Threshold (Hipotesis Awal <= 10%)
        if dev_buy_pct > settings.max_dev_buy_pct:
            rejections.append(
                f"Dev initial allocation too high: {dev_buy_pct:.1f}% > {settings.max_dev_buy_pct:.1f}%"
            )

        # 2. Check Deployer Wallet Past History (Fase C: win-rate based, not rug count)
        deployer = event.deployer_wallet_address
        if deployer:
            try:
                deployer_profile = await db_manager.query(
                    "deployer_profiles",
                    filters={"wallet_address": f"eq.{deployer}"},
                    limit=1
                )
                if deployer_profile:
                    profile = deployer_profile[0]
                    total = int(profile.get("total_tokens_launched") or 0)
                    win_rate_raw = profile.get("win_rate_pct")
                    win_rate = float(win_rate_raw) if win_rate_raw is not None else 100.0
                    dead_count = int(profile.get("dead_count") or 0)


                    # Gate: reject only when evidence is statistically meaningful
                    if total >= DEPLOYER_MIN_LAUNCHES and win_rate < DEPLOYER_MAX_WIN_RATE:
                        rejections.append(
                            f"Serial rugger: {total} launches, {win_rate:.0f}% win rate, {dead_count} dead"
                        )
                        logger.debug(
                            f"Deployer {deployer[:8]}... flagged: "
                            f"{total} launches, {win_rate:.1f}% win rate"
                        )
            except Exception as e:
                # Don't block the filter if DB is unavailable
                logger.debug(f"Deployer profile lookup failed for {deployer[:8]}: {e}")

        # 3. Check Instant Scalping Heuristics (Ponyin Rules)
        # Sample deployer as initial top holder
        top_holders = [deployer] if deployer else []
        scalp_results = await instant_scalp_filter.evaluate(
            mint_address=event.token_address,
            top_holder_pubkeys=top_holders,
            initial_buy_tokens=event.initial_buy_amount,
            total_supply=total_supply,
            initial_sol_liquidity=event.initial_sol_liquidity
        )

        flags_count = scalp_results.get("flags_count", 0)
        if flags_count >= 2:
            rejections.append(f"Instant Scalp Risk: {flags_count} flags triggered ({scalp_results.get('details')})")

        filter_pass = len(rejections) == 0
        rejection_reason = " | ".join(rejections) if rejections else None

        return SafetyCheckResult(
            token_address=event.token_address,
            venue="pump_fun",
            filter_pass=filter_pass,
            rejection_reason=rejection_reason,
            mint_authority_renounced=True,    # Built-in to Pump.fun smart contract
            freeze_authority_renounced=True,  # Built-in to Pump.fun smart contract
            lp_locked_or_burned=False,        # Bonding curve active, not yet migrated
            lp_lock_pct=0.0,
            top10_holder_pct=dev_buy_pct,
            honeypot_check_passed=True,
            dev_holding_pct=dev_buy_pct,
            sniper_bundle_pct=0.0,
            instant_scalp_flags_count=flags_count,
            scalp_flag_gas_spike=scalp_results.get("scalp_flag_gas_spike", False),
            scalp_flag_young_wallet=scalp_results.get("scalp_flag_young_wallet", False),
            scalp_flag_low_balance=scalp_results.get("scalp_flag_low_balance", False),
            scalp_flag_pump_anomaly=scalp_results.get("scalp_flag_pump_anomaly", False),
            raw_check_data={
                "dev_buy_pct": dev_buy_pct,
                "scalp_details": scalp_results
            }
        )


pump_safety_filter = PumpSafetyFilter()
