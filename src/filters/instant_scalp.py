from typing import Any
from src.utils.solana_rpc import solana_rpc
from src.utils.logger import logger


class InstantScalpFilter:
    """
    Implements Ponyin 4-Filter Heuristics (Instant Scalping rules):
    1. Global Gas Fee Spike (Anomali kemacetan gas fee)
    2. Holder Age (Top holder wallet didanai < 1 hari yang lalu)
    3. Holder Balance (Top holder saldo SOL < 0.2 SOL)
    4. Deployment Pump Anomaly (MC < $3k dipompa instan / initial buy anomali)
    """

    async def evaluate(
        self,
        mint_address: str,
        top_holder_pubkeys: list[str],
        initial_buy_tokens: float,
        total_supply: float = 1_000_000_000.0,
        initial_sol_liquidity: float = 30.0
    ) -> dict[str, Any]:
        flags = {
            "scalp_flag_gas_spike": False,
            "scalp_flag_young_wallet": False,
            "scalp_flag_low_balance": False,
            "scalp_flag_pump_anomaly": False,
            "flags_count": 0,
            "details": {}
        }

        # 1. Global Gas Fee Check
        try:
            fees = await solana_rpc.get_recent_prioritization_fees()
            if fees:
                avg_fee = sum(fees) / len(fees)
                max_fee = max(fees)
                # Anomaly threshold: average priority fee > 150,000 micro-lamports or max > 500,000
                if avg_fee > 150_000 or max_fee > 500_000:
                    flags["scalp_flag_gas_spike"] = True
                    flags["details"]["gas_spike"] = f"Avg: {avg_fee:.0f}, Max: {max_fee:.0f}"
        except Exception as e:
            logger.debug(f"Error checking gas fees: {e}")

        # 2 & 3. Check Top Holders Wallet Age & Balance (sample up to 3 non-contract holders)
        if top_holder_pubkeys:
            young_wallets = 0
            low_balance_wallets = 0
            checked_count = 0

            for pubkey in top_holder_pubkeys[:3]:
                if not pubkey or len(pubkey) < 32:
                    continue
                checked_count += 1
                try:
                    # Check age
                    age_days = await solana_rpc.get_wallet_age_days(pubkey)
                    if 0.0 <= age_days < 1.0:
                        young_wallets += 1

                    # Check SOL balance
                    balance_sol = await solana_rpc.get_sol_balance(pubkey)
                    if balance_sol < 0.2:
                        low_balance_wallets += 1
                except Exception as e:
                    logger.debug(f"Error evaluating holder {pubkey}: {e}")

            if checked_count > 0 and (young_wallets / checked_count) >= 0.5:
                flags["scalp_flag_young_wallet"] = True
                flags["details"]["young_wallets"] = f"{young_wallets}/{checked_count} < 1 day old"

            if checked_count > 0 and (low_balance_wallets / checked_count) >= 0.5:
                flags["scalp_flag_low_balance"] = True
                flags["details"]["low_balance"] = f"{low_balance_wallets}/{checked_count} < 0.2 SOL"

        # 4. Deployment Pump Anomaly Check
        dev_buy_ratio = (initial_buy_tokens / total_supply) if total_supply > 0 else 0
        if dev_buy_ratio > 0.15 or (initial_sol_liquidity < 5.0 and dev_buy_ratio > 0.05):
            flags["scalp_flag_pump_anomaly"] = True
            flags["details"]["pump_anomaly"] = f"Initial buy {dev_buy_ratio*100:.1f}% on low liquidity"

        # Count active flags
        count = sum([
            flags["scalp_flag_gas_spike"],
            flags["scalp_flag_young_wallet"],
            flags["scalp_flag_low_balance"],
            flags["scalp_flag_pump_anomaly"]
        ])
        flags["flags_count"] = count

        return flags


instant_scalp_filter = InstantScalpFilter()
