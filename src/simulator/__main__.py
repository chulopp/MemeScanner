"""
CLI Entry Point for MemeScanner Offline Simulator.

Usage:
    python -m src.simulator serve [--port 8421]
    python -m src.simulator run [--config param=value ...]
    python -m src.simulator sweep --grid param=v1,v2 ...
    python -m src.simulator validate
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from src.database.client import db_manager
from src.utils.logger import logger


async def _cmd_validate():
    """Validate simulator against v2.1 paper trade data — result should be close to actual."""
    from src.simulator.config import SimulatorConfig
    from src.simulator.dataset import load_trades, get_dataset_meta
    from src.simulator.runner import run_scenario

    await db_manager.initialize()
    trades = await load_trades()
    meta = get_dataset_meta(trades)
    print(f"\n📊 Dataset: {meta['total']} trades | {meta['date_first']} → {meta['date_last']}")
    print(f"   Versions: {meta['versions']}")
    print(f"   Avg MFE: {meta['avg_mfe_pct']:.1f}% | Max MFE: {meta['max_mfe_pct']:.1f}%")

    # v2.1 only subset for validation
    v21_trades = [t for t in trades if t.get("parameter_version") == "v2.1"]
    print(f"\n🔬 Validating on {len(v21_trades)} v2.1 trades...")

    config = SimulatorConfig.v21_baseline()
    result = await run_scenario(config=config, trades=v21_trades, include_trades=False)
    s = result.stats
    print(f"\n✅ Simulation Result (v2.1 Baseline):")
    print(f"   ROI:            {s.roi_pct:+.2f}%")
    print(f"   Final Equity:   ${s.final_equity:.2f}")
    print(f"   Win Rate:       {s.win_rate:.1%} ({s.win_count}W / {s.loss_count}L)")
    print(f"   Max Drawdown:   {s.max_drawdown_pct:.1f}%")
    print(f"   Avg MFE Capt:   {s.avg_mfe_captured_pct:.1%}")
    print(f"   Trades Entered: {s.win_count + s.loss_count} / {result.dataset_total} total")
    print(f"   Runtime:        {result.runtime_ms:.0f}ms")
    print(f"\n📌 Expected (actual paper trade v2.1): ROI ≈ -19.39%, Win Rate ≈ 13.5%")
    print(f"   (Small delta is expected due to price path model approximation)")


async def _cmd_run(params: dict):
    """Run a single scenario with given parameters."""
    from src.simulator.config import SimulatorConfig
    from src.simulator.dataset import load_trades
    from src.simulator.runner import run_scenario

    await db_manager.initialize()
    trades = await load_trades()

    config = SimulatorConfig.from_dict(params)
    result = await run_scenario(config=config, trades=trades, include_trades=False)
    s = result.stats
    print(f"\n🎯 Scenario: {result.config_label}")
    print(f"   ROI:            {s.roi_pct:+.2f}% (${s.realized_pnl_usd:+.2f})")
    print(f"   Win Rate:       {s.win_rate:.1%} ({s.win_count}W / {s.loss_count}L)")
    print(f"   Max Drawdown:   {s.max_drawdown_pct:.1f}%")
    print(f"   Avg MFE Capt:   {s.avg_mfe_captured_pct:.1%}")
    print(f"   Runtime:        {result.runtime_ms:.0f}ms")


async def _cmd_sweep(param_grid: dict[str, list]):
    """Run a grid sweep and print top 10 results."""
    from src.simulator.dataset import load_trades
    from src.simulator.sweep import run_grid_sweep

    await db_manager.initialize()
    trades = await load_trades()
    results = await run_grid_sweep(param_grid, trades=trades)

    print(f"\n🏆 Grid Sweep Leaderboard (Top 10 of {len(results)} scenarios):")
    print(f"{'Rank':<5} {'Label':<45} {'ROI':>8} {'WinRate':>8} {'MaxDD':>7} {'Entered':>8}")
    print("-" * 85)
    for i, r in enumerate(results[:10], 1):
        s = r.stats
        print(
            f"{i:<5} {r.config_label[:45]:<45} "
            f"{s.roi_pct:>+7.2f}% {s.win_rate:>7.1%} "
            f"{s.max_drawdown_pct:>6.1f}% {s.win_count + s.loss_count:>8}"
        )


async def _cmd_serve(port: int):
    """Start the FastAPI web dashboard."""
    import uvicorn
    from src.simulator.api.server import app

    await db_manager.initialize()
    # Pre-load dataset
    from src.simulator.dataset import load_trades
    trades = await load_trades()
    logger.info(f"[Simulator] Dataset pre-loaded: {len(trades)} trades.")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info")
    server = uvicorn.Server(config)
    print(f"\n🚀 MemeScanner Simulator running at http://localhost:{port}")
    print(f"   Dataset: {len(trades)} trades loaded")
    print(f"   Press CTRL+C to stop.\n")
    await server.serve()


def main():
    parser = argparse.ArgumentParser(
        prog="python -m src.simulator",
        description="MemeScanner Offline Backtester & Scenario Simulator",
    )
    subparsers = parser.add_subparsers(dest="command")

    # serve
    serve_p = subparsers.add_parser("serve", help="Start web dashboard")
    serve_p.add_argument("--port", type=int, default=8421)

    # validate
    subparsers.add_parser("validate", help="Validate simulator against v2.1 paper trade data")

    # run
    run_p = subparsers.add_parser("run", help="Run a single scenario")
    run_p.add_argument("params", nargs="*", help="param=value pairs, e.g. tp0_pct=35 stop_loss_pct=-25")

    # sweep
    sweep_p = subparsers.add_parser("sweep", help="Run a grid sweep")
    sweep_p.add_argument("--grid", nargs="+", required=True,
                         help="param=v1,v2,v3 pairs, e.g. --grid tp0_pct=30,40,50 stop_loss_pct=-20,-30")

    args = parser.parse_args()

    if args.command == "serve":
        asyncio.run(_cmd_serve(args.port))

    elif args.command == "validate":
        asyncio.run(_cmd_validate())

    elif args.command == "run":
        params = {}
        for token in (args.params or []):
            key, _, val = token.partition("=")
            try:
                params[key] = float(val)
            except ValueError:
                params[key] = val
        asyncio.run(_cmd_run(params))

    elif args.command == "sweep":
        param_grid: dict[str, list] = {}
        for token in args.grid:
            key, _, vals_str = token.partition("=")
            try:
                vals = [float(v) for v in vals_str.split(",")]
            except ValueError:
                vals = vals_str.split(",")
            param_grid[key] = vals
        asyncio.run(_cmd_sweep(param_grid))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
