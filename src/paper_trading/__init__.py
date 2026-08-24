"""
Paper Trading Module — Fase 5 & 6
Automated signal recording, multi-timeframe outcome resolution, Telegram notifications,
and Virtual Portfolio & Multi-Exit Strategy Optimizer.
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
    "TradeSimulationRecord"
]
