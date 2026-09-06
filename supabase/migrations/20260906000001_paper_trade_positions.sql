-- ==============================================================================
-- Migration: 20260906000001_paper_trade_positions.sql
-- Description: Paper Trading Live — virtual position tracking with MFE & exit quality
-- ==============================================================================

-- paper_trade_positions: Satu baris per posisi virtual (dibuka + ditutup)
-- Setiap posisi mencatat kualitas entry (MFE) terpisah dari kualitas exit (captured_ratio)
-- signal_source membedakan sinyal dari Pintu A (token baru) vs Pintu B (wallet tracker)

CREATE TABLE IF NOT EXISTS public.paper_trade_positions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Token identity
    token_address           VARCHAR(64) NOT NULL,
    symbol                  VARCHAR(32),

    -- Signal provenance
    signal_source           VARCHAR(16) NOT NULL DEFAULT 'PINTU_A',   -- 'PINTU_A' | 'PINTU_B'
    paper_signal_id         UUID,                                       -- FK ke paper_signals.id (nullable for resilience)
    opportunity_score_at_entry FLOAT8 NOT NULL DEFAULT 0.0,

    -- Entry
    entry_price_usd         FLOAT8 NOT NULL DEFAULT 0.0,
    entry_time              TIMESTAMPTZ NOT NULL DEFAULT now(),
    position_size_usd       FLOAT8 NOT NULL DEFAULT 2.0,

    -- Real-time MFE tracking (diperbarui tiap 30s selama posisi aktif)
    price_high_ever_seen    FLOAT8 NOT NULL DEFAULT 0.0,              -- price puncak yang pernah tercatat

    -- Exit
    exit_price_usd          FLOAT8,
    exit_time               TIMESTAMPTZ,
    exit_reason             VARCHAR(32),                               -- 'TP1' | 'TP2' | 'TP3' | 'SL' | 'TRAILING' | 'TIMEOUT' | 'OPEN'

    -- Computed at close
    realized_return_pct     FLOAT8,
    mfe_pct                 FLOAT8,                                    -- ((price_high - entry) / entry) * 100
    captured_ratio          FLOAT8,                                    -- realized_return_pct / mfe_pct

    -- Duration
    hold_duration_minutes   FLOAT8,

    -- Frozen parameter version for cross-epoch comparison
    parameter_version       VARCHAR(32) NOT NULL DEFAULT 'v1.0',

    -- Skipped entries (signal valid tapi tidak bisa dibuka)
    skipped_reason          VARCHAR(32),                               -- NULL | 'SKIPPED_CAPACITY' | 'DUPLICATE'

    -- Timestamps
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_ptp_token_address ON public.paper_trade_positions (token_address);
CREATE INDEX IF NOT EXISTS idx_ptp_signal_source ON public.paper_trade_positions (signal_source);
CREATE INDEX IF NOT EXISTS idx_ptp_exit_reason   ON public.paper_trade_positions (exit_reason);
CREATE INDEX IF NOT EXISTS idx_ptp_entry_time    ON public.paper_trade_positions (entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_ptp_skipped       ON public.paper_trade_positions (skipped_reason) WHERE skipped_reason IS NOT NULL;

-- RLS: allow service_role full access (same pattern as other tables)
ALTER TABLE public.paper_trade_positions ENABLE ROW LEVEL SECURITY;
