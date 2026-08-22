from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


class SafetyCheckResult(BaseModel):
    token_address: str
    venue: str
    filter_pass: bool
    rejection_reason: Optional[str] = None
    checked_at: datetime = Field(default_factory=datetime.utcnow)

    # Core Security Flags
    mint_authority_renounced: bool = False
    freeze_authority_renounced: bool = False
    lp_locked_or_burned: bool = False
    lp_lock_pct: float = 0.0
    top10_holder_pct: float = 0.0
    honeypot_check_passed: bool = True
    dev_holding_pct: float = 0.0
    sniper_bundle_pct: float = 0.0

    # Ponyin Instant Scalp Heuristic Flags
    instant_scalp_flags_count: int = 0
    scalp_flag_gas_spike: bool = False
    scalp_flag_young_wallet: bool = False
    scalp_flag_low_balance: bool = False
    scalp_flag_pump_anomaly: bool = False

    raw_check_data: dict[str, Any] = Field(default_factory=dict)
