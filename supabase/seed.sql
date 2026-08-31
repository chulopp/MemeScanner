-- ==============================================================================
-- Seed Data: supabase/seed.sql
-- Description: Verified initial seed list of Smart Money wallets
-- ==============================================================================

-- 1. Insert parent wallet entries
INSERT INTO public.wallets (wallet_address, reputation_score, rug_count_history, total_tokens_launched, tags)
VALUES
    ('2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f', 0.85, 0, 0, '["SMART_MONEY", "EARLY_ACCUMULATOR"]'::jsonb),
    ('2T5NgDDidkvhJQg8AHDi74uCFwgp25pYFMRZXBaCUNBH', 0.90, 0, 0, '["SMART_MONEY", "RUNNER_SNIPER"]'::jsonb),
    ('2X4H5Y9C4Fy6Pf3wpq8Q4gMvLcWvfrrwDv2bdR8AAwQv', 0.80, 0, 0, '["SMART_MONEY", "HIGH_CONVICTION"]'::jsonb),
    ('4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk', 0.95, 0, 0, '["SMART_MONEY", "BREAKOUT_TRADER"]'::jsonb),
    ('4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9', 0.85, 0, 0, '["SMART_MONEY", "FAST_SWING"]'::jsonb),
    ('87rRdssFiTJKY4MGARa4G5vQ31hmR7MxSmhzeaJ5AAxJ', 0.90, 0, 0, '["SMART_MONEY", "SCALP_TRADER"]'::jsonb)
ON CONFLICT (wallet_address) DO NOTHING;

-- 2. Insert smart money profile metrics
INSERT INTO public.smart_money_profiles (
    wallet_address,
    net_realized_profit_sol,
    total_volume_sol,
    total_trades_recorded,
    win_rate_pct,
    profit_factor,
    is_active,
    tier,
    source,
    notes
)
VALUES
    ('2fg5QD1eD7rzNNCsvnhmXFm5hqNgwTTG8p7kQ6f3rx6f', 45.0, 210.0, 28, 60.7, 2.15, true, 'ACTIVE', 'USER_CURATED', 'User-verified early memecoin accumulation wallet'),
    ('2T5NgDDidkvhJQg8AHDi74uCFwgp25pYFMRZXBaCUNBH', 62.5, 340.0, 34, 64.7, 2.30, true, 'ACTIVE', 'USER_CURATED', 'User-verified runner sniper'),
    ('2X4H5Y9C4Fy6Pf3wpq8Q4gMvLcWvfrrwDv2bdR8AAwQv', 38.2, 180.0, 22, 59.1, 1.95, true, 'ACTIVE', 'USER_CURATED', 'User-verified high conviction early buyer'),
    ('4BdKaxN8G6ka4GYtQQWk4G4dZRUTX2vQH9GcXdBREFUk', 89.0, 450.0, 41, 68.3, 2.50, true, 'ACTIVE', 'USER_CURATED', 'User-verified pump.fun breakout trader'),
    ('4vw54BmAogeRV3vPKWyFet5yf8DTLcREzdSzx4rw9Ud9', 54.8, 290.0, 31, 61.3, 2.10, true, 'ACTIVE', 'USER_CURATED', 'User-verified fast swing trader'),
    ('87rRdssFiTJKY4MGARa4G5vQ31hmR7MxSmhzeaJ5AAxJ', 71.4, 380.0, 37, 64.9, 2.25, true, 'ACTIVE', 'USER_CURATED', 'User-verified high win rate scalp trader')
ON CONFLICT (wallet_address) DO UPDATE SET
    net_realized_profit_sol = EXCLUDED.net_realized_profit_sol,
    total_volume_sol = EXCLUDED.total_volume_sol,
    total_trades_recorded = EXCLUDED.total_trades_recorded,
    win_rate_pct = EXCLUDED.win_rate_pct,
    profit_factor = EXCLUDED.profit_factor,
    is_active = EXCLUDED.is_active,
    tier = EXCLUDED.tier,
    notes = EXCLUDED.notes;
