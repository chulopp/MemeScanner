"""
MemeScanner Offline Backtester & Scenario Simulator
====================================================
Simulates exit parameter combinations on historical paper_trade_positions data
to find the sweet spot without waiting for live paper trade cycles.

Modules:
    config   — SimulatorConfig dataclass (all tunable parameters)
    dataset  — Load & cache closed trades from Supabase
    price_model — Price path model (entry → MFE → exit) for trigger evaluation
    exit_engine — Core trade simulation engine
    portfolio   — Portfolio accounting ($100 starting capital)
    runner      — Single-scenario runner
    sweep       — Grid sweep: evaluate all parameter combinations
    api         — FastAPI web dashboard server
"""
