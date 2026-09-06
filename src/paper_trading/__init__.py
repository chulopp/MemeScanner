"""
Paper Trading Module — Fase 5 & 6 (Paper Trading Live)
Automated signal recording, multi-timeframe outcome resolution, Telegram notifications,
Virtual Portfolio & Multi-Exit Strategy Optimizer, and live position tracking.

Paper Trading Live additions:
  - PositionTracker: real-time virtual position management (30s polling, TP/SL/trailing)
  - CheckpointReporter: structured evaluation reports with per-source breakdown
"""

from src.paper_trading.price_fetcher import fetch_price, PriceSnapshot
from src.paper_trading.signal_recorder import record_signal
from src.paper_trading.outcome_worker import outcome_worker, OutcomeWorker
from src.paper_trading.telegram_notifier import telegram_notifier, TelegramNotifier
from src.paper_trading.evaluator import evaluate_paper_trading
from src.paper_trading.portfolio_simulator import (
    portfolio_simulator,
    PortfolioSimulator,
    StrategyMatrixResult,
    MilestoneHitRate,
    TradeSimulationRecord
)
from src.paper_trading.position_tracker import position_tracker, PositionTracker, FROZEN_PARAMS
from src.paper_trading.checkpoint_reporter import generate_checkpoint_report, run_checkpoint

__all__ = [
    "fetch_price",
    "PriceSnapshot",
    "record_signal",
    "outcome_worker",
    "OutcomeWorker",
    "telegram_notifier",
    "TelegramNotifier",
    "evaluate_paper_trading",
    "portfolio_simulator",
    "PortfolioSimulator",
    "StrategyMatrixResult",
    "MilestoneHitRate",
    "TradeSimulationRecord",
    # Paper Trading Live
    "position_tracker",
    "PositionTracker",
    "FROZEN_PARAMS",
    "generate_checkpoint_report",
    "run_checkpoint",
]

