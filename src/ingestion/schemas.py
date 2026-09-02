import re
from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field, field_validator

# Standard Solana base58 pattern: 32-44 base58 characters
SOLANA_PUBKEY_REGEX = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class RawTokenEvent(BaseModel):
    token_address: str
    symbol: str
    name: str
    deployer_wallet_address: Optional[str] = None
    launch_venue: str  # 'pump_fun' | 'raydium'
    launch_timestamp: datetime = Field(default_factory=datetime.utcnow)
    initial_buy_amount: float = 0.0  # Tokens bought by dev or initial pool
    total_supply: float = 1_000_000_000.0  # Default 1B for pump.fun
    initial_sol_liquidity: float = 0.0
    bonding_curve_address: Optional[str] = None
    pool_address: Optional[str] = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    # Pintu B attribution — populated by WalletTrackerListener
    source: str = "NEW_PAIR"  # 'NEW_PAIR' | 'WALLET_TRACKER'
    triggered_by_wallet: Optional[str] = None  # Smart Money wallet address that triggered this signal
    triggered_by_wallet_sol_spent: float = 0.0  # SOL the Smart Money wallet spent on this buy
    triggered_by_tx_signature: Optional[str] = None  # Transaction signature for audit trail

    @field_validator("token_address")
    @classmethod
    def validate_token_address(cls, v: str) -> str:
        v_clean = str(v).strip()
        if not SOLANA_PUBKEY_REGEX.match(v_clean):
            raise ValueError(f"Invalid Solana token_address: '{v}' is not a valid base58 public key (length 32-44).")
        return v_clean

    @field_validator("deployer_wallet_address")
    @classmethod
    def validate_deployer_address(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v_clean = str(v).strip()
        if not v_clean:
            return None
        if not SOLANA_PUBKEY_REGEX.match(v_clean):
            # If deployer is invalid, we don't necessarily crash the whole token event, but normalize to None
            return None
        return v_clean

