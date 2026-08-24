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

    # --- Fase 3: Opportunity Scoring Weights [HIPOTESIS_AWAL — WAJIB DIKALIBRASI DI FASE 4] ---
    score_w_vol_velocity: float = Field(default=0.35, validation_alias="SCORE_W_VOL_VELOCITY")      # Bobot Volume Velocity [HIPOTESIS_AWAL]
    score_w_smart_money: float = Field(default=0.30, validation_alias="SCORE_W_SMART_MONEY")        # Bobot Smart Money [HIPOTESIS_AWAL]
    score_w_global_fee: float = Field(default=0.15, validation_alias="SCORE_W_GLOBAL_FEE")          # Bobot Global Fee Urgency [HIPOTESIS_AWAL]
    score_w_holder_curve: float = Field(default=0.10, validation_alias="SCORE_W_HOLDER_CURVE")      # Bobot Holder Curve [HIPOTESIS_AWAL]
    score_w_social_meta: float = Field(default=0.10, validation_alias="SCORE_W_SOCIAL_META")        # Bobot Social Meta [HIPOTESIS_AWAL]

    # --- Volume Velocity & Buy/Sell Ratio Thresholds [HIPOTESIS_AWAL] ---
    vol_velocity_buy_sell_ratio_max: float = Field(default=5.0, validation_alias="VOL_VELOCITY_BUY_SELL_RATIO_MAX")  # Ratio >= 5.0 -> 100 skor
    vol_velocity_window_seconds: int = Field(default=300, validation_alias="VOL_VELOCITY_WINDOW_SECONDS")            # Window 5 menit

    # --- Smart Money Promotion & Demotion Thresholds [HIPOTESIS_AWAL] ---
    smart_money_min_trades: int = Field(default=20, validation_alias="SMART_MONEY_MIN_TRADES")
    smart_money_min_net_profit_sol: float = Field(default=15.0, validation_alias="SMART_MONEY_MIN_NET_PROFIT_SOL")
    smart_money_min_profit_factor: float = Field(default=1.8, validation_alias="SMART_MONEY_MIN_PROFIT_FACTOR")
    smart_money_demotion_days: int = Field(default=14, validation_alias="SMART_MONEY_DEMOTION_DAYS")

    # --- Global Fee Urgency & Wash Trade Filter [HIPOTESIS_AWAL] ---
    global_fee_wash_filter_min_fee: int = Field(default=1000, validation_alias="GLOBAL_FEE_WASH_FILTER_MIN_FEE")  # micro-lamports threshold

    # --- Fase 5: Paper Trading & Telegram ---
    opportunity_threshold: float = Field(default=60.0, validation_alias="OPPORTUNITY_THRESHOLD")  # HYPOTHESIS_INIT
    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", validation_alias="TELEGRAM_CHAT_ID")


settings = Settings()

# Post-processing helper to build complete URLs if needed
if settings.helius_api_key:
    if not settings.helius_rpc_url or settings.helius_rpc_url.endswith("="):
        settings.helius_rpc_url = f"https://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
    if not settings.helius_ws_url or settings.helius_ws_url.endswith("="):
        settings.helius_ws_url = f"wss://mainnet.helius-rpc.com/?api-key={settings.helius_api_key}"
