"""
SimulatorConfig — All tunable exit & entry parameters for the offline simulator.

Default values mirror FROZEN_PARAMS in position_tracker.py (v2.1).
Every parameter here can be varied independently in a scenario run or grid sweep.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class SimulatorConfig:
    """
    Full set of parameters that can be varied in a simulation run.
    Defaults match FROZEN_PARAMS v2.1 exactly.
    """

    # ── Entry Filter ──────────────────────────────────────────────
    opportunity_threshold: float = 60.0      # Min score to enter a position

    # ── Stop Loss ─────────────────────────────────────────────────
    stop_loss_pct: float = -30.0             # Base SL from entry
    breakeven_trigger_pct: float = 50.0      # Activate breakeven SL when return >= this
    breakeven_sl_pct: float = -10.0          # SL level once breakeven is active

    # ── Time-Decay ────────────────────────────────────────────────
    time_decay_tighten_minutes: float = 15.0  # Phase 2: tighten SL after this many minutes
    time_decay_exit_minutes: float = 30.0     # Phase 3: force exit after this many minutes
    time_decay_mfe_threshold_pct: float = 15.0# Time-decay only fires if MFE < this
    time_decay_sl_tighten_pct: float = -15.0  # Tightened SL in phase 2

    # ── Take Profit Tiers ─────────────────────────────────────────
    tp0_pct: float = 50.0
    tp0_sell_fraction: float = 0.15          # Fraction of TOTAL position sold at TP0
    tp1_pct: float = 100.0
    tp1_sell_fraction: float = 0.25
    tp2_pct: float = 300.0
    tp2_sell_fraction: float = 0.25
    tp3_pct: float = 500.0
    tp3_sell_fraction: float = 0.15
    moonbag_fraction: float = 0.20           # Remainder after all TPs (must = 1 - sum of fractions)

    # ── Trailing Stop ─────────────────────────────────────────────
    trailing_start_return_pct: float = 500.0  # Trailing activates when return >= this (default: after TP3)
    trailing_tier1_pct: float = 25.0          # Trail % from ATH when ATH_return < 200%
    trailing_tier2_pct: float = 35.0          # Trail % from ATH when ATH_return 200-500%
    trailing_tier3_pct: float = 45.0          # Trail % from ATH when ATH_return > 500%

    # ── Timing & Sizing ───────────────────────────────────────────
    max_hold_hours: float = 2.0
    position_risk_pct: float = 2.0           # % of current equity allocated per trade

    # ── Label ─────────────────────────────────────────────────────
    label: str = ""                          # Auto-generated if empty

    def __post_init__(self):
        if not self.label:
            self.label = self.to_label()

    def to_label(self) -> str:
        """Generate a short human-readable label for this config."""
        parts = []
        if self.opportunity_threshold != 60.0:
            parts.append(f"thresh={self.opportunity_threshold:.0f}")
        if self.tp0_pct != 50.0:
            parts.append(f"tp0={self.tp0_pct:.0f}%")
        if self.tp1_pct != 100.0:
            parts.append(f"tp1={self.tp1_pct:.0f}%")
        if self.stop_loss_pct != -30.0:
            parts.append(f"sl={self.stop_loss_pct:.0f}%")
        if self.trailing_start_return_pct != 500.0:
            parts.append(f"trail@{self.trailing_start_return_pct:.0f}%")
        if self.time_decay_exit_minutes != 30.0:
            parts.append(f"decay={self.time_decay_exit_minutes:.0f}m")
        if self.max_hold_hours != 2.0:
            parts.append(f"hold={self.max_hold_hours:.1f}h")
        if self.position_risk_pct != 2.0:
            parts.append(f"size={self.position_risk_pct:.1f}%")
        return " | ".join(parts) if parts else "v2.1 Baseline"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("label", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SimulatorConfig":
        """Build config from a flat dict (e.g., from API request or CLI args)."""
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    @classmethod
    def v21_baseline(cls) -> "SimulatorConfig":
        """Returns the exact v2.1 production config (all defaults)."""
        return cls(label="v2.1 Baseline")


# ── Search space definitions for grid sweep ──────────────────────────────────

SWEEP_DEFAULTS: dict[str, list] = {
    "opportunity_threshold":     [55.0, 60.0, 65.0, 70.0],
    "stop_loss_pct":             [-20.0, -25.0, -30.0, -35.0],
    "breakeven_trigger_pct":     [30.0, 40.0, 50.0],
    "breakeven_sl_pct":          [-5.0, -10.0, -15.0],
    "time_decay_exit_minutes":   [20.0, 25.0, 30.0, 45.0],
    "time_decay_mfe_threshold_pct": [10.0, 15.0, 20.0],
    "tp0_pct":                   [30.0, 40.0, 50.0],
    "tp0_sell_fraction":         [0.10, 0.15, 0.25, 0.30],
    "tp1_pct":                   [75.0, 100.0, 150.0],
    "tp1_sell_fraction":         [0.20, 0.25, 0.30],
    "trailing_start_return_pct": [50.0, 100.0, 200.0, 300.0, 500.0],
    "trailing_tier1_pct":        [20.0, 25.0, 30.0, 35.0],
    "max_hold_hours":            [1.0, 2.0, 4.0],
    "position_risk_pct":         [1.0, 2.0, 3.0],
}
