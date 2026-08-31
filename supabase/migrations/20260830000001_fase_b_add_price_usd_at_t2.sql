-- Migration: Fase B -- Add price_usd_at_t2 column to backtest_tokens (R9 decision)
--
-- Purpose:
--   Store the token price at T+2 minutes after listing. This is the actual
--   entry price used in the two-stage delayed scoring pipeline (Fase B).
--   NULL = T+2 price not captured (t0_fallback=True in backtest EV calculation).
--
-- The backtest gate metric (ev_per_trade_t2) uses ONLY rows where this is non-NULL.
-- Rows with NULL fall back to T=0 price for backward-compatible ev_per_trade only.

ALTER TABLE backtest_tokens
    ADD COLUMN IF NOT EXISTS price_usd_at_t2 FLOAT DEFAULT NULL;

COMMENT ON COLUMN backtest_tokens.price_usd_at_t2 IS
    'Token price in USD at T+2 minutes after listing. '
    'Captured by data_collector._fetch_t2_price() via bonding curve on-chain (primary) '
    'or DexScreener/Helius fallback. '
    'NULL means T+2 price was unavailable (t0_fallback=True in backtest). '
    'Gate metric ev_per_trade_t2 uses only non-NULL rows (R9/R14 decisions).';
