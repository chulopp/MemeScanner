import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Helius RPC & WS
    helius_api_key: str = Field(default="", validation_alias="HELIUS_API_KEY")
    helius_rpc_url: str = Field(
        default="https://mainnet.helius-rpc.com/?api-key=",
        validation_alias="HELIUS_RPC_URL"
    )
    helius_ws_url: str = Field(
        default="wss://mainnet.helius-rpc.com/?api-key=",
        validation_alias="HELIUS_WS_URL"
    )

    # Upstash Redis
    upstash_redis_rest_url: str = Field(default="", validation_alias="UPSTASH_REDIS_REST_URL")
    upstash_redis_rest_token: str = Field(default="", validation_alias="UPSTASH_REDIS_REST_TOKEN")

    # Supabase
    supabase_url: str = Field(default="", validation_alias="SUPABASE_URL")
    supabase_key: str = Field(default="", validation_alias="SUPABASE_KEY")
    supabase_service_key: str = Field(default="", validation_alias="SUPABASE_SERVICE_KEY")

    @property
    def effective_supabase_key(self) -> str:
        """Prefers service_role key for backend DB operations, falls back to standard key."""
        return self.supabase_service_key or self.supabase_key

    # PumpPortal
    pumpportal_ws_url: str = Field(
        default="wss://pumpportal.fun/api/data",
        validation_alias="PUMPPORTAL_WS_URL"
    )

    # Safety Filter Thresholds (Hipotesis Awal)
    max_dev_buy_pct: float = Field(default=10.0, validation_alias="MAX_DEV_BUY_PCT")
    max_sniper_bundle_pct: float = Field(default=25.0, validation_alias="MAX_SNIPER_BUNDLE_PCT")
    min_lp_locked_pct: float = Field(default=90.0, validation_alias="MIN_LP_LOCKED_PCT")
    max_top10_holders_pct: float = Field(default=30.0, validation_alias="MAX_TOP10_HOLDERS_PCT")
    rpc_concurrency_limit: int = Field(default=15, validation_alias="RPC_CONCURRENCY_LIMIT")


settings = Settings()

# Post-processing helper to build complete URLs if needed
if settings.helius_api_key:
    if not settings.helius_rpc_url or settings.helius_rpc_url.endswith("="):
        settings.helius_rpc_url = f"https://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
    if not settings.helius_ws_url or settings.helius_ws_url.endswith("="):
        settings.helius_ws_url = f"wss://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
