-- ==============================================================================
-- Migration: 20260823000001_create_backtest_schema.sql
-- Description: Backtesting schema (backtest_tokens and backtest_runs)
-- ==============================================================================

-- Enable pgcrypto for gen_random_uuid() if not already available
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- 1. BACKTEST_TOKENS (Dataset of historical and live tokens collected for calibration)
CREATE TABLE IF NOT EXISTS public.backtest_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_address TEXT NOT NULL UNIQUE,
    symbol TEXT,
    name TEXT,
    launch_venue TEXT DEFAULT 'pump_fun'::text,
    listed_at TIMESTAMPTZ,
    price_usd_at_listing FLOAT8,
    price_usd_24h FLOAT8,
    liquidity_usd FLOAT8,
    volume_24h_usd FLOAT8,
    label TEXT,
    label_return_pct FLOAT8,
    raw_dexscreener JSONB,
    collected_at TIMESTAMPTZ DEFAULT now(),
    launch_price_usd FLOAT8,
    price_24h_usd FLOAT8,
    is_resolved BOOLEAN DEFAULT false,
    resolution_due_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ
);

-- 2. BACKTEST_RUNS (Optimization and calibration experiment results)
CREATE TABLE IF NOT EXISTS public.backtest_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at TIMESTAMPTZ DEFAULT now(),
    dataset_size INT4,
    runner_count INT4,
    dead_count INT4,
    neutral_count INT4,
    params JSONB,
    filter_precision FLOAT8,
    opportunity_recall FLOAT8,
    ev_per_trade FLOAT8,
    is_optimal BOOLEAN DEFAULT false,
    notes TEXT,
    oos_ev_per_trade FLOAT8,
    oos_filter_precision FLOAT8,
    oos_opportunity_recall FLOAT8,
    fold_results JSONB
);

-- Row Level Security (RLS) Configuration
ALTER TABLE public.backtest_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.backtest_runs ENABLE ROW LEVEL SECURITY;

-- Service Role Full Access Policies
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'backtest_tokens' AND policyname = 'service_role_full'
    ) THEN
        CREATE POLICY "service_role_full" ON public.backtest_tokens FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_policies WHERE tablename = 'backtest_runs' AND policyname = 'service_role_full'
    ) THEN
        CREATE POLICY "service_role_full" ON public.backtest_runs FOR ALL TO service_role USING (true) WITH CHECK (true);
    END IF;
END $$;
