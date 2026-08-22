from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


class TokenModel(BaseModel):
    token_address: str
    symbol: Optional[str] = None
    name: Optional[str] = None
    deployer_wallet_address: Optional[str] = None
    launch_timestamp: datetime = Field(default_factory=datetime.utcnow)
    launch_venue: str  # 'pump_fun' | 'raydium'
    status: str = "INGESTED"  # 'INGESTED' | 'PASSED_SAFETY' | 'REJECTED'
    initial_metadata: dict[str, Any] = Field(default_factory=dict)


class FilterResultModel(BaseModel):
    token_address: str
    checked_at: datetime = Field(default_factory=datetime.utcnow)
    mint_authority_renounced: bool = False
    freeze_authority_renounced: bool = False
    lp_locked_or_burned: bool = False
    lp_lock_pct: float = 0.0
    top10_holder_pct: float = 0.0
    honeypot_check_passed: bool = True
    dev_holding_pct: float = 0.0
    sniper_bundle_pct: float = 0.0
    instant_scalp_flags_count: int = 0
    filter_pass: bool
    rejection_reason: Optional[str] = None
    raw_check_data: dict[str, Any] = Field(default_factory=dict)


class WalletModel(BaseModel):
    wallet_address: str
    first_seen: datetime = Field(default_factory=datetime.utcnow)
    reputation_score: float = 0.0
    rug_count_history: int = 0
    total_tokens_launched: int = 0
    tags: list[str] = Field(default_factory=list)
