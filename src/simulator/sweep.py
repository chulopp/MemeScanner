"""
Grid Sweep — Evaluates all combinations of parameter values in parallel.

Given a parameter grid (dict of param_name → list of values to try),
generates all combinations using itertools.product, runs each as a
ScenarioResult, and returns them sorted by ROI (best first).

Example:
    grid = {"tp0_pct": [30, 40, 50], "stop_loss_pct": [-20, -25, -30]}
    → 9 combinations evaluated simultaneously
"""
from __future__ import annotations

import asyncio
import itertools
from typing import Optional

from src.simulator.config import SimulatorConfig
from src.simulator.dataset import load_trades
from src.simulator.runner import run_scenario, ScenarioResult
from src.utils.logger import logger


async def run_grid_sweep(
    param_grid: dict[str, list],
    trades: Optional[list[dict]] = None,
    max_concurrent: int = 20,
) -> list[ScenarioResult]:
    """
    Run a full grid sweep over all combinations in param_grid.

    Args:
        param_grid:     Dict of {param_name: [value1, value2, ...]} to sweep.
        trades:         Pre-loaded trade list. If None, loads from Supabase.
        max_concurrent: Max parallel scenario evaluations (semaphore limit).

    Returns:
        List of ScenarioResult sorted by roi_pct descending (best first).
    """
    if trades is None:
        trades = await load_trades()

    # Generate all combinations
    param_names = list(param_grid.keys())
    param_values = list(param_grid.values())
    combinations = list(itertools.product(*param_values))
    total = len(combinations)
    logger.info(f"[Sweep] Starting grid sweep: {total} combinations across {len(param_names)} parameters.")

    semaphore = asyncio.Semaphore(max_concurrent)
    results: list[ScenarioResult] = []
    completed = 0

    async def _run_one(combo: tuple) -> ScenarioResult:
        nonlocal completed
        params = dict(zip(param_names, combo))
        config = SimulatorConfig.from_dict(params)
        async with semaphore:
            result = await run_scenario(config=config, trades=trades, include_trades=False)
        completed += 1
        if completed % 10 == 0 or completed == total:
            logger.info(f"[Sweep] Progress: {completed}/{total} done.")
        return result

    # Also always include the v2.1 baseline for comparison
    baseline_config = SimulatorConfig.v21_baseline()
    baseline_task = run_scenario(config=baseline_config, trades=trades, include_trades=False)

    # Run all combinations + baseline concurrently
    all_tasks = [_run_one(combo) for combo in combinations]
    all_tasks.append(baseline_task)

    all_results = await asyncio.gather(*all_tasks, return_exceptions=False)
    results = [r for r in all_results if isinstance(r, ScenarioResult)]

    # Sort by ROI descending
    results.sort(key=lambda r: r.stats.roi_pct, reverse=True)
    logger.info(
        f"[Sweep] Complete! Best: '{results[0].config_label}' → ROI {results[0].stats.roi_pct:+.2f}% | "
        f"Baseline (v2.1): {results[-1].stats.roi_pct:+.2f}%"
    )

    return results


def build_heatmap_data(
    results: list[ScenarioResult],
    param_x: str,
    param_y: str,
    metric: str = "roi_pct",
) -> dict:
    """
    Build 2D heatmap data from sweep results for two chosen parameters.

    Args:
        results:  Sweep results list.
        param_x:  Name of the parameter to use as X axis.
        param_y:  Name of the parameter to use as Y axis.
        metric:   Which metric to show in color ('roi_pct', 'win_rate', etc.)

    Returns:
        Dict with 'x_values', 'y_values', 'matrix' (2D list) for Chart.js.
    """
    x_vals: list = sorted(set(r.config.get(param_x) for r in results if param_x in r.config))
    y_vals: list = sorted(set(r.config.get(param_y) for r in results if param_y in r.config))

    # Build lookup: (x_val, y_val) → metric_value
    lookup: dict[tuple, float] = {}
    for r in results:
        xv = r.config.get(param_x)
        yv = r.config.get(param_y)
        if xv is not None and yv is not None:
            val = getattr(r.stats, metric, 0.0)
            lookup[(xv, yv)] = val

    matrix = []
    for y in y_vals:
        row = []
        for x in x_vals:
            row.append(lookup.get((x, y), None))
        matrix.append(row)

    return {
        "x_label": param_x,
        "y_label": param_y,
        "x_values": x_vals,
        "y_values": y_vals,
        "matrix": matrix,
        "metric": metric,
    }


# Progress tracking for async sweep (used by /api/sweep/status endpoint)
_sweep_progress: dict[str, int | str] = {"completed": 0, "total": 0, "status": "idle"}


def get_sweep_progress() -> dict:
    return dict(_sweep_progress)
