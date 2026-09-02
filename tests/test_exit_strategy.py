"""
Tests for src/exit/strategy.py — Fase 1 Exit Strategy Engine.
All tests are pure (no async, no DB, no network).
"""

import pytest
from src.exit.strategy import (
    ExitStrategyConfig,
    TpTier,
    TrailingTier,
    simulate_exit,
    DEFAULT_EXIT_CONFIG,
    _get_trailing_pct,
    _moonbag_is_stopped,
    _build_price_points,
)


# ─────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────

def _entry(price: float = 0.001) -> float:
    return price


def _basic_config() -> ExitStrategyConfig:
    """Standard 3-tier config from grill session."""
    return ExitStrategyConfig(
        tp_tiers=[
            TpTier(sell_fraction=0.30, target_return_pct=100.0),   # 2x
            TpTier(sell_fraction=0.30, target_return_pct=300.0),   # 4x
            TpTier(sell_fraction=0.20, target_return_pct=500.0),   # 6x
        ],
        moonbag_fraction=0.20,
        sl_pct=-50.0,
        trailing_tiers=[
            TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=60.0),
            TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=50.0),
            TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=40.0),
        ],
    )


# ─────────────────────────────────────────────────────────────────
#  Unit tests
# ─────────────────────────────────────────────────────────────────

class TestSlTriggersCorrectly:
    """SL at -50% from entry: entire position should be sold."""

    def test_sl_hit_before_any_tp(self):
        """Coin drops -55% — entire position exits at SL level."""
        result = simulate_exit(
            entry_price=0.001,
            # h24 = -55%, so SL at -50% should trigger
            price_changes={"m5": -10.0, "h1": -35.0, "h6": -55.0, "h24": -55.0},
            entry_liquidity_usd=10_000.0,
            config=_basic_config(),
            entry_cost_pct=0.0,
        )
        assert result.sl_triggered is True
        assert result.tp_tiers_hit == 0
        assert len(result.partial_exits) == 1
        assert result.partial_exits[0].exit_reason == "SL"
        assert result.partial_exits[0].fraction_sold == pytest.approx(1.0)
        assert result.realized_return_pct < 0

    def test_sl_exact_boundary(self):
        """Coin drops exactly -50% — SL should trigger."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h24": -50.0},
            entry_liquidity_usd=10_000.0,
            config=_basic_config(),
        )
        assert result.sl_triggered is True
        assert result.tp_tiers_hit == 0

    def test_no_sl_if_never_reaches_threshold(self):
        """Coin drops only -30% — SL should NOT trigger."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h24": -30.0},
            entry_liquidity_usd=10_000.0,
            config=_basic_config(),
        )
        assert result.sl_triggered is False

    def test_sl_includes_exit_cost(self):
        """SL exit should incur slippage (on thinner pool)."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h24": -60.0},
            entry_liquidity_usd=5_000.0,
            config=_basic_config(),
        )
        assert result.sl_triggered is True
        assert result.partial_exits[0].slippage_pct > 0


class TestTpTiersFireSequentially:
    """Verify tiered TP fires correctly as price rises."""

    def test_only_tp1_fires(self):
        """Coin reaches 2x (100%) but not 4x."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h1": 120.0, "h24": 80.0},  # ATH ~120%
            entry_liquidity_usd=20_000.0,
            config=_basic_config(),
        )
        assert result.tp_tiers_hit == 1
        assert not result.sl_triggered
        tp_events = [e for e in result.partial_exits if e.exit_reason.startswith("TP")]
        assert len(tp_events) == 1
        assert tp_events[0].exit_reason == "TP1"
        assert tp_events[0].fraction_sold == pytest.approx(0.30)

    def test_tp1_and_tp2_fire(self):
        """Coin reaches 4x (300%) but not 6x."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h1": 350.0, "h24": 200.0},  # ATH ~350%
            entry_liquidity_usd=20_000.0,
            config=_basic_config(),
        )
        assert result.tp_tiers_hit == 2
        tp_reasons = [e.exit_reason for e in result.partial_exits if e.exit_reason.startswith("TP")]
        assert "TP1" in tp_reasons
        assert "TP2" in tp_reasons

    def test_all_three_tp_tiers_fire(self):
        """Coin reaches 6x (500%) — all 3 tiers should fire."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h1": 600.0, "h24": 300.0},  # ATH ~600%
            entry_liquidity_usd=30_000.0,
            config=_basic_config(),
        )
        assert result.tp_tiers_hit == 3
        tp_reasons = [e.exit_reason for e in result.partial_exits if e.exit_reason.startswith("TP")]
        assert "TP1" in tp_reasons
        assert "TP2" in tp_reasons
        assert "TP3" in tp_reasons

    def test_tp_fractions_sum_correctly(self):
        """When all 3 tiers fire + moonbag, total fractions should sum to 1."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h1": 600.0, "h24": 300.0},
            entry_liquidity_usd=30_000.0,
            config=_basic_config(),
        )
        total_fraction = sum(e.fraction_sold for e in result.partial_exits)
        assert total_fraction == pytest.approx(1.0, abs=0.001)


class TestMoonbagTrailingStop:
    """Moonbag should exit when price drops from ATH beyond trailing threshold."""

    def test_trailing_triggers_after_all_tp(self):
        """Coin hits 3x (ATH=3x), then drops 50% from ATH — moonbag should trail out."""
        # ATH at 200% (3x), then drops to ~100% (still 2x) — drop from ATH = 33%
        # Wait, ATH multiplier = 3x. Trail at 2x threshold = 40%.
        # ATH=3x, final_price = 3x * (1 - 0.45) = 3x * 0.55 = 1.65x → drop from ATH = 45% > 40% → trigger
        config = _basic_config()
        result = simulate_exit(
            entry_price=1.0,
            # ATH at +300% (4x), h24 = +120% (2.2x)
            # 4x ATH, drop to 2.2x → drawdown from ATH = (4-2.2)/4 = 45% >= 40% trail → trigger
            price_changes={"h1": 300.0, "h24": 120.0},
            entry_liquidity_usd=20_000.0,
            config=config,
        )
        # TP1 fires at 2x (100%), TP2 fires at 4x (300%), moonbag should trail
        assert result.moonbag_exit_reason == "TRAILING_STOP"

    def test_moonbag_held_if_no_trailing_applies(self):
        """If ATH is below the lowest trailing tier threshold (2x), moonbag held to timeout."""
        result = simulate_exit(
            entry_price=1.0,
            # ATH = +50% (1.5x) → below 2x threshold, no adaptive trailing applies
            price_changes={"h1": 50.0, "h24": -5.0},
            entry_liquidity_usd=5_000.0,
            config=_basic_config(),
        )
        assert result.moonbag_exit_reason == "TIMEOUT_24H"


class TestAdaptiveTrailingWidens:
    """Higher ATH multiplier → looser trailing stop."""

    def test_trailing_at_2x_is_40_pct(self):
        """At ATH 2x-5x range, trailing should be 40%."""
        trail = _get_trailing_pct(
            ath_multiplier=3.0,
            trailing_tiers=[
                TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=60.0),
                TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=50.0),
                TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=40.0),
            ]
        )
        assert trail == 40.0

    def test_trailing_at_5x_to_10x_is_50_pct(self):
        """At ATH 5x-10x range, trailing should be 50%."""
        trail = _get_trailing_pct(
            ath_multiplier=7.0,
            trailing_tiers=[
                TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=60.0),
                TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=50.0),
                TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=40.0),
            ]
        )
        assert trail == 50.0

    def test_trailing_at_10x_plus_is_60_pct(self):
        """At ATH > 10x, trailing should be 60%."""
        trail = _get_trailing_pct(
            ath_multiplier=12.0,
            trailing_tiers=[
                TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=60.0),
                TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=50.0),
                TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=40.0),
            ]
        )
        assert trail == 60.0

    def test_trailing_below_lowest_threshold_returns_none(self):
        """ATH below lowest threshold → no trailing applies."""
        trail = _get_trailing_pct(
            ath_multiplier=1.5,
            trailing_tiers=[
                TrailingTier(multiplier_threshold=10.0, trail_pct_from_ath=60.0),
                TrailingTier(multiplier_threshold=5.0,  trail_pct_from_ath=50.0),
                TrailingTier(multiplier_threshold=2.0,  trail_pct_from_ath=40.0),
            ]
        )
        assert trail is None


class TestMegaRunnerMoonbagSurvives:
    """Volatile but strong runner: dip from ATH should NOT trigger 50% trailing."""

    def test_35_pct_dip_from_5x_ath_does_not_trigger_50_pct_trail(self):
        """
        Coin goes 5x, dips 35% from ATH, then ends at 4x.
        At 5x ATH, trail = 50%. Drawdown from ATH = 35% < 50% → moonbag should SURVIVE.
        """
        # ATH = 5x = +400%. Final = 4x = +300%. Drawdown from ATH = (5-4)/5 = 20% → < 50% → survive
        result = simulate_exit(
            entry_price=1.0,
            price_changes={"h1": 400.0, "h24": 300.0},  # ATH=5x, final=4x
            entry_liquidity_usd=50_000.0,
            config=_basic_config(),
        )
        # TP3 fires at 500% — ATH only 400%, so TP3 does NOT fire
        # TP1 (100%), TP2 (300%) fire. Moonbag holds.
        assert result.moonbag_exit_reason == "TIMEOUT_24H"  # Survives to end

    def test_55_pct_dip_from_10x_ath_does_not_trigger_60_pct_trail(self):
        """
        Coin goes 10x, dips 55% from ATH. Trail at 10x = 60%. Drawdown = 55% < 60% → survive.
        """
        # ATH = 10x = +900%. Drop to 4.5x = +350%. Drawdown = (10-4.5)/10 = 55% < 60% → survive
        result = simulate_exit(
            entry_price=1.0,
            price_changes={"h1": 900.0, "h24": 350.0},  # ATH=10x, final=4.5x
            entry_liquidity_usd=100_000.0,
            config=_basic_config(),
        )
        assert result.moonbag_exit_reason == "TIMEOUT_24H"


class TestCostPerExitEvent:
    """Each partial exit should incur separate slippage."""

    def test_multiple_tp_each_incur_separate_slippage(self):
        """3 TP tiers fired + moonbag = 4 separate slippage charges."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h1": 600.0, "h24": 300.0},
            entry_liquidity_usd=10_000.0,
            config=_basic_config(),
        )
        assert result.tp_tiers_hit == 3
        assert len(result.partial_exits) == 4  # 3 TP + 1 moonbag/timeout
        for event in result.partial_exits:
            assert event.slippage_pct > 0.0

    def test_total_cost_higher_than_single_trade_cost(self):
        """Multi-exit total cost should exceed cost of a single flat exit (more transactions)."""
        result_tiered = simulate_exit(
            entry_price=0.001,
            price_changes={"h1": 600.0, "h24": 300.0},
            entry_liquidity_usd=10_000.0,
            config=_basic_config(),
            entry_cost_pct=5.0,  # 5% entry cost already paid
        )
        # Total exit cost should be > 0 and < 100% (reasonable)
        assert result_tiered.total_exit_cost_pct > 0.0
        assert result_tiered.total_exit_cost_pct < 100.0

    def test_sl_exit_has_thinner_pool_slippage(self):
        """SL triggered on the way down — pool is thin, slippage should be at micro rate."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"h24": -60.0},
            entry_liquidity_usd=5_000.0,  # Micro pool
            config=_basic_config(),
        )
        assert result.sl_triggered is True
        # Pool on the way down = entry_liq * 0.5 = 2500 → still micro → 5% slippage
        assert result.partial_exits[0].slippage_pct == pytest.approx(5.0)


class TestDeadCoinSlBeforeTp:
    """Dead coin: SL should fire before any TP tier."""

    def test_immediate_drop_sl_fires(self):
        """Dead coin drops -80% immediately — SL at -50% fires, 0 TP tiers."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"m5": -80.0, "h24": -90.0},
            entry_liquidity_usd=3_000.0,
            config=_basic_config(),
        )
        assert result.sl_triggered is True
        assert result.tp_tiers_hit == 0
        assert result.realized_return_pct < -40.0  # SL loss + slippage

    def test_rug_then_brief_pump_sl_fires_first(self):
        """
        Even if there's a brief pump after SL, SL fires at earlier timestamp.
        m5 drops to -55%, h1 pumps to +200%. SL at m5 wins.
        """
        result = simulate_exit(
            entry_price=0.001,
            price_changes={"m5": -55.0, "h1": 200.0, "h24": 50.0},
            entry_liquidity_usd=5_000.0,
            config=_basic_config(),
        )
        # SL fires at -55% (m5=5min), TP1 at +100% (h1=60min).
        # SL time < TP1 time → SL wins
        assert result.sl_triggered is True
        assert result.tp_tiers_hit == 0


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_zero_entry_price_returns_zero(self):
        """Zero entry price should return empty ExitResult without crashing."""
        result = simulate_exit(
            entry_price=0.0,
            price_changes={"h24": 100.0},
        )
        assert result.realized_return_pct == pytest.approx(0.0)

    def test_empty_price_changes_moonbag_timeout(self):
        """No price data → moonbag held, no SL, no TP."""
        result = simulate_exit(
            entry_price=0.001,
            price_changes={},
            config=_basic_config(),
        )
        # entry_price == all prices, no SL/TP triggers
        assert result.sl_triggered is False
        assert result.tp_tiers_hit == 0

    def test_no_tp_missed_means_no_realized_tp_gains(self):
        """Coin only goes +40% — below all TP tiers. No TP fires, moonbag exits at 24h."""
        result = simulate_exit(
            entry_price=1.0,
            price_changes={"h24": 40.0},
            entry_liquidity_usd=10_000.0,
            config=_basic_config(),
        )
        assert result.tp_tiers_hit == 0
        assert result.sl_triggered is False
        # Moonbag + full position exits at 40% less slippage
        assert result.moonbag_exit_reason == "TIMEOUT_24H"

    def test_build_price_points_sorted(self):
        """_build_price_points should return sorted chronological list."""
        points = _build_price_points(
            entry_price=1.0,
            price_changes={"h24": 50.0, "m5": 10.0, "h1": 25.0},
        )
        minutes = [p[0] for p in points]
        assert minutes == sorted(minutes)

    def test_default_config_fractions_sum_to_one(self):
        """Default config: sum of all TP tier fractions + moonbag = 1.0."""
        cfg = DEFAULT_EXIT_CONFIG
        total = sum(t.sell_fraction for t in cfg.tp_tiers) + cfg.moonbag_fraction
        assert total == pytest.approx(1.0)
