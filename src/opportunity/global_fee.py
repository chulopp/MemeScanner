import statistics
from typing import Optional, Any
from src.config import settings
from src.utils.solana_rpc import solana_rpc
from src.utils.logger import logger


class GlobalFeeResult:
    def __init__(
        self,
        score: float,
        median_fee_micro_lamports: float,
        max_fee_micro_lamports: float,
        p90_fee_micro_lamports: float,
        valid_fee_sample_count: int,
        is_wash_trade_suspected: bool = False,
        is_successful: bool = True,
        raw_data: Optional[dict[str, Any]] = None
    ):
        self.score = score
        self.median_fee_micro_lamports = median_fee_micro_lamports
        self.max_fee_micro_lamports = max_fee_micro_lamports
        self.p90_fee_micro_lamports = p90_fee_micro_lamports
        self.valid_fee_sample_count = valid_fee_sample_count
        self.is_wash_trade_suspected = is_wash_trade_suspected
        self.is_successful = is_successful
        self.raw_data = raw_data or {}


class GlobalFeeUrgencyEngine:
    """
    Evaluates Global Fee Urgency & Priority Fee intensity.
    Verifies genuine transaction urgency via priority fees and filters zero-fee wash trading.
    """

    async def calculate_fee_urgency(
        self,
        mint_address: Optional[str] = None,
        extra_accounts: Optional[list[str]] = None
    ) -> GlobalFeeResult:
        """
        Fetches prioritization fees for target token accounts (or general fallback) and scores urgency.
        """
        try:
            target_addrs = []
            if mint_address:
                target_addrs.append(mint_address)
            if extra_accounts:
                target_addrs.extend(extra_accounts)

            # Query token-specific priority fees first if address provided
            raw_fees = []
            if target_addrs:
                raw_fees = await solana_rpc.get_recent_prioritization_fees(target_addrs)

            # Fallback to general RPC priority fees if token-specific returns empty
            if not raw_fees:
                raw_fees = await solana_rpc.get_recent_prioritization_fees()

            if not raw_fees:
                # Default neutral score if RPC returns empty
                return GlobalFeeResult(
                    score=50.0,
                    median_fee_micro_lamports=5000.0,
                    max_fee_micro_lamports=10000.0,
                    p90_fee_micro_lamports=8000.0,
                    valid_fee_sample_count=0,
                    is_wash_trade_suspected=False,
                    is_successful=True,
                    raw_data={"note": "Default baseline used"}
                )

            # Filter zero / wash trade fees below threshold
            min_thresh = settings.global_fee_wash_filter_min_fee
            valid_fees = [f for f in raw_fees if f >= min_thresh]
            zero_fee_ratio = (len(raw_fees) - len(valid_fees)) / max(len(raw_fees), 1)

            is_wash = zero_fee_ratio > 0.85 and len(raw_fees) > 10

            if not valid_fees:
                return GlobalFeeResult(
                    score=10.0 if is_wash else 30.0,
                    median_fee_micro_lamports=0.0,
                    max_fee_micro_lamports=0.0,
                    p90_fee_micro_lamports=0.0,
                    valid_fee_sample_count=0,
                    is_wash_trade_suspected=is_wash,
                    is_successful=True,
                    raw_data={"zero_fee_ratio": zero_fee_ratio}
                )

            median_fee = float(statistics.median(valid_fees))
            max_fee = float(max(valid_fees))
            sorted_fees = sorted(valid_fees)
            p90_idx = int(len(sorted_fees) * 0.90)
            p90_fee = float(sorted_fees[min(p90_idx, len(sorted_fees) - 1)])

            # Normalization scale [HIPOTESIS_AWAL]:
            # Baseline quiet Solana fee: ~5,000 - 10,000 micro-lamports -> Score ~40-50
            # High competitive urgency: ~50,000 - 150,000 micro-lamports -> Score ~75-90
            # Extreme rush / high tip: >= 250,000 micro-lamports -> Score 100
            if median_fee <= 5000.0:
                normalized_score = max(20.0, (median_fee / 5000.0) * 40.0)
            elif median_fee <= 50000.0:
                normalized_score = 40.0 + ((median_fee - 5000.0) / 45000.0) * 40.0  # 40 -> 80
            else:
                normalized_score = 80.0 + min(((median_fee - 50000.0) / 200000.0) * 20.0, 20.0)  # 80 -> 100

            # Apply wash trading penalty if >70% transactions had zero/sub-threshold fees
            if is_wash:
                normalized_score = max(normalized_score * 0.3, 5.0)

            return GlobalFeeResult(
                score=round(normalized_score, 2),
                median_fee_micro_lamports=round(median_fee, 2),
                max_fee_micro_lamports=round(max_fee, 2),
                p90_fee_micro_lamports=round(p90_fee, 2),
                valid_fee_sample_count=len(valid_fees),
                is_wash_trade_suspected=is_wash,
                is_successful=True,
                raw_data={
                    "total_samples": len(raw_fees),
                    "valid_samples": len(valid_fees),
                    "zero_fee_ratio": round(zero_fee_ratio, 2),
                    "median_fee": median_fee,
                    "p90_fee": p90_fee,
                    "max_fee": max_fee
                }
            )
        except Exception as e:
            logger.debug(f"Error evaluating global fee urgency: {e}")
            return GlobalFeeResult(
                score=50.0,
                median_fee_micro_lamports=0.0,
                max_fee_micro_lamports=0.0,
                p90_fee_micro_lamports=0.0,
                valid_fee_sample_count=0,
                is_successful=False,
                raw_data={"error": str(e)}
            )


global_fee_engine = GlobalFeeUrgencyEngine()
