"""
Exit Strategy Engine — Fase 1
Multi-tier Take Profit + Moonbag Adaptive Trailing Stop + Stop Loss.
"""
from src.exit.strategy import (
    ExitStrategyConfig,
    ExitResult,
    PartialExitEvent,
    simulate_exit,
    DEFAULT_EXIT_CONFIG,
)

__all__ = [
    "ExitStrategyConfig",
    "ExitResult",
    "PartialExitEvent",
    "simulate_exit",
    "DEFAULT_EXIT_CONFIG",
]
