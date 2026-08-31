-- Migration: Fase C -- Create deployer_profiles table (Q13 decision)
--
-- Purpose:
--   Track per-deployer wallet performance history across all launched tokens.
--   Used by pump_safety.py to reject serial ruggers based on win rate
--   (more nuanced than raw rug count — Q13 decision).
--
-- Gate: Reject if total_tokens_launched >= 3 AND win_rate_pct < 10%
-- (i.e., serial rugger with strong historical evidence)

CREATE TABLE IF NOT EXISTS deployer_profiles (
    wallet_address       TEXT PRIMARY KEY,
    total_tokens_launched INT    DEFAULT 0,
    runner_count         INT    DEFAULT 0,
    dead_count           INT    DEFAULT 0,
    neutral_count        INT    DEFAULT 0,
    avg_return_pct       FLOAT  DEFAULT 0.0,
    win_rate_pct         FLOAT  DEFAULT 0.0,    -- runner_count / total_tokens_launched * 100
    last_launch_at       TIMESTAMPTZ,
    updated_at           TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE deployer_profiles IS
    'Per-deployer wallet performance tracking for Fase C deployer profiling. '
    'Populated when backtest_tokens rows resolve (is_resolved=True). '
    'Used by pump_safety.py to reject serial ruggers (total>=3 AND win_rate<10%).';

COMMENT ON COLUMN deployer_profiles.win_rate_pct IS
    'Percentage of launched tokens that became runners. '
    'Gate: < 10% with >= 3 launches triggers rejection in pump_safety.';

-- Index for fast lookup in pump_safety filter (called on every new token)
CREATE INDEX IF NOT EXISTS idx_deployer_profiles_wallet ON deployer_profiles (wallet_address);
