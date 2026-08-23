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


class WalletRelationshipModel(BaseModel):
    wallet_a: str
    wallet_b: str
    relationship_type: str  # 'DIRECT_FUNDING' | 'SHARED_FUNDER_HOP1' | 'SHARED_FUNDER_HOP2'
    hop_distance: int = 1
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    shared_funding_sol: float = 0.0
    confidence_score: float = 0.0


class MetricSnapshotModel(BaseModel):
    token_address: str
    snapshot_at: datetime = Field(default_factory=datetime.utcnow)
    opportunity_score: float = 0.0
    score_vol_velocity: Optional[float] = None
    score_smart_money: Optional[float] = None
    score_global_fee: Optional[float] = None
    score_holder_curve: Optional[float] = None
    score_social_meta: Optional[float] = None
    market_cap_usd: float = 0.0
    liquidity_usd: float = 0.0
    volume_5m_usd: float = 0.0
    buy_tx_count_5m: int = 0
    sell_tx_count_5m: int = 0
    net_buy_pressure_ratio: float = 0.0
    global_priority_fees_sol: float = 0.0
    bonding_curve_pct: float = 0.0
    unique_holders_count: int = 0
    weights_used: dict[str, float] = Field(default_factory=dict)
    active_components: list[str] = Field(default_factory=list)
    raw_metrics: dict[str, Any] = Field(default_factory=dict)


class SmartMoneyProfileModel(BaseModel):
    wallet_address: str
    tier: str = "SEED"  # 'SEED' | 'ACTIVE' | 'DEMOTED'
    is_active: bool = True
    first_added: datetime = Field(default_factory=datetime.utcnow)
    last_active_at: datetime = Field(default_factory=datetime.utcnow)
    total_trades_recorded: int = 0
    total_volume_sol: float = 0.0
    net_realized_profit_sol: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    source: str = "MANUAL"  # 'MANUAL' | 'AUTO_PROMOTED'
    notes: str = ""


# Convenient alias
SmartMoneyWalletModel = SmartMoneyProfileModel

