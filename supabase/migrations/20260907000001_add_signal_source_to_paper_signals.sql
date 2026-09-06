-- ==============================================================================
-- Migration: 20260907000001_add_signal_source_to_paper_signals.sql
-- Description: Add signal_source column to paper_signals for Pintu A vs Pintu B tracking
-- ==============================================================================

ALTER TABLE public.paper_signals 
ADD COLUMN IF NOT EXISTS signal_source VARCHAR(16) DEFAULT 'PINTU_A';

CREATE INDEX IF NOT EXISTS idx_paper_signals_source ON public.paper_signals (signal_source);
