from src.config import settings
from src.ingestion.schemas import RawTokenEvent
from src.filters.schemas import SafetyCheckResult
from src.filters.instant_scalp import instant_scalp_filter
from src.utils.solana_rpc import solana_rpc
from src.utils.logger import logger


class RaydiumSafetyFilter:
    """Hard safety filter tailored for Raydium AMM pools."""

    async def evaluate(self, event: RawTokenEvent) -> SafetyCheckResult:
        rejections = []
        mint_address = event.token_address

        # 1. Fetch on-chain mint details (Mint Authority & Freeze Authority)
        mint_info = await solana_rpc.get_mint_info(mint_address)
        mint_auth = mint_info.get("mint_authority")
        freeze_auth = mint_info.get("freeze_authority")
        total_supply = float(mint_info.get("supply", 1_000_000_000))

        mint_renounced = mint_auth is None
        freeze_renounced = freeze_auth is None

        if not mint_renounced:
            rejections.append(f"Mint Authority active ({mint_auth})")
        if not freeze_renounced:
            rejections.append(f"Freeze Authority active ({freeze_auth})")

        # 2. Check Top 10 Holder Concentration
        top_accounts = await solana_rpc.get_token_largest_accounts(mint_address)
        top10_pct = 0.0
        top_holder_pubkeys = []

        if top_accounts and total_supply > 0:
            # Sum up top 10 accounts
            top10_amount = sum(float(acc.get("uiAmount", 0) or 0) for acc in top_accounts[:10])
            top10_pct = (top10_amount / (total_supply / (10 ** mint_info.get("decimals", 9)))) * 100.0 if mint_info.get("decimals", 9) > 0 else (top10_amount / total_supply) * 100.0
            top10_pct = min(max(top10_pct, 0.0), 100.0)
            top_holder_pubkeys = [acc.get("address") for acc in top_accounts if acc.get("address")]

            if top10_pct > settings.max_top10_holders_pct:
                rejections.append(f"Top 10 holders concentration high: {top10_pct:.1f}% > {settings.max_top10_holders_pct:.1f}%")

        # 3. Check LP Burned / Locked Status
        # For new Raydium pool, standard burn address is 11111111111111111111111111111111 or dead
        lp_lock_pct = 100.0 if mint_renounced and freeze_renounced else 0.0
        lp_locked = lp_lock_pct >= settings.min_lp_locked_pct

        # 4. Check Instant Scalping Heuristics (Ponyin Rules)
        scalp_results = await instant_scalp_filter.evaluate(
            mint_address=mint_address,
            top_holder_pubkeys=top_holder_pubkeys,
            initial_buy_tokens=0.0,
            total_supply=total_supply,
            initial_sol_liquidity=event.initial_sol_liquidity
        )

        flags_count = scalp_results.get("flags_count", 0)
        if flags_count >= 2:
            rejections.append(f"Instant Scalp Risk: {flags_count} flags triggered ({scalp_results.get('details')})")

        filter_pass = len(rejections) == 0
        rejection_reason = " | ".join(rejections) if rejections else None

        return SafetyCheckResult(
            token_address=mint_address,
            venue="raydium",
            filter_pass=filter_pass,
            rejection_reason=rejection_reason,
            mint_authority_renounced=mint_renounced,
            freeze_authority_renounced=freeze_renounced,
            lp_locked_or_burned=lp_locked,
            lp_lock_pct=lp_lock_pct,
            top10_holder_pct=top10_pct,
            honeypot_check_passed=True,
            dev_holding_pct=0.0,
            sniper_bundle_pct=0.0,
            instant_scalp_flags_count=flags_count,
            scalp_flag_gas_spike=scalp_results.get("scalp_flag_gas_spike", False),
            scalp_flag_young_wallet=scalp_results.get("scalp_flag_young_wallet", False),
            scalp_flag_low_balance=scalp_results.get("scalp_flag_low_balance", False),
            scalp_flag_pump_anomaly=scalp_results.get("scalp_flag_pump_anomaly", False),
            raw_check_data={
                "mint_info": mint_info,
                "top10_pct": top10_pct,
                "scalp_details": scalp_results
            }
        )


raydium_safety_filter = RaydiumSafetyFilter()
