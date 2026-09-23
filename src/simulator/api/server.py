"""
FastAPI Web Dashboard Server for MemeScanner Offline Simulator.

Endpoints:
    GET  /                    → Serve index.html
    GET  /api/trades/meta     → Dataset metadata (counts, date range)
    GET  /api/defaults        → SimulatorConfig default values
    POST /api/run             → Run a single scenario
    POST /api/sweep           → Run a grid sweep
    GET  /api/sweep/status    → Sweep progress
    POST /api/heatmap         → Build heatmap data from sweep results
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.database.client import db_manager
from src.simulator.config import SimulatorConfig, SWEEP_DEFAULTS
from src.simulator.dataset import load_trades, get_dataset_meta, clear_cache
from src.simulator.runner import run_scenario
from src.simulator.sweep import run_grid_sweep, build_heatmap_data, get_sweep_progress

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="MemeScanner Offline Simulator",
    description="Backtesting & scenario simulation dashboard for exit parameter optimization",
    version="1.0.0",
)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ── In-memory sweep state ─────────────────────────────────────────────────────
_last_sweep_results: list[dict] = []
_sweep_lock = asyncio.Lock()
_sweep_running = False


# ── API Models ────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    config: dict[str, Any] = {}
    include_trades: bool = True


class SweepRequest(BaseModel):
    param_grid: dict[str, list]  # e.g. {"tp0_pct": [30,40,50], "stop_loss_pct": [-20,-30]}


class HeatmapRequest(BaseModel):
    param_x: str
    param_y: str
    metric: str = "roi_pct"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the main dashboard HTML."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return HTMLResponse(content=index_path.read_text(encoding="utf-8"))


@app.get("/api/trades/meta")
async def get_trades_meta():
    """Return metadata about the loaded dataset."""
    trades = await load_trades()
    meta = get_dataset_meta(trades)
    return JSONResponse(content=meta)


@app.get("/api/defaults")
async def get_defaults():
    """Return default SimulatorConfig values and sweep suggestions."""
    cfg = SimulatorConfig.v21_baseline()
    return JSONResponse(content={
        "defaults": cfg.to_dict(),
        "sweep_suggestions": SWEEP_DEFAULTS,
    })


@app.post("/api/run")
async def run_single_scenario(request: RunRequest):
    """
    Run a single scenario with the provided config overrides.
    Returns full ScenarioResult including equity curve and per-trade details.
    """
    try:
        config = SimulatorConfig.from_dict(request.config)
        trades = await load_trades()
        result = await run_scenario(config=config, trades=trades, include_trades=request.include_trades)
        return JSONResponse(content=result.to_api_dict(include_trades=request.include_trades))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sweep")
async def run_sweep(request: SweepRequest):
    """
    Run a grid sweep over all combinations of the provided param_grid.
    Returns sorted leaderboard (best ROI first).
    """
    global _last_sweep_results, _sweep_running

    if _sweep_running:
        raise HTTPException(status_code=409, detail="A sweep is already running. Please wait.")

    if not request.param_grid:
        raise HTTPException(status_code=400, detail="param_grid cannot be empty.")

    # Validate all parameter names
    valid_params = set(SimulatorConfig.__dataclass_fields__.keys())
    for param in request.param_grid:
        if param not in valid_params:
            raise HTTPException(status_code=400, detail=f"Unknown parameter: '{param}'")

    total_combos = 1
    for vals in request.param_grid.values():
        total_combos *= len(vals)

    if total_combos > 500:
        raise HTTPException(
            status_code=400,
            detail=f"Too many combinations: {total_combos}. Limit is 500. Reduce sweep range."
        )

    async with _sweep_lock:
        _sweep_running = True
        try:
            trades = await load_trades()
            results = await run_grid_sweep(request.param_grid, trades=trades)
            _last_sweep_results = [r.to_api_dict(include_trades=False) for r in results]
        finally:
            _sweep_running = False

    return JSONResponse(content={
        "count": len(_last_sweep_results),
        "results": _last_sweep_results,
    })


@app.get("/api/sweep/status")
async def sweep_status():
    """Return current sweep progress and last results count."""
    return JSONResponse(content={
        "running": _sweep_running,
        "progress": get_sweep_progress(),
        "last_results_count": len(_last_sweep_results),
    })


@app.post("/api/heatmap")
async def get_heatmap(request: HeatmapRequest):
    """Build 2D heatmap data from the last sweep results."""
    if not _last_sweep_results:
        raise HTTPException(status_code=404, detail="No sweep results available. Run a sweep first.")

    # Reconstruct minimal objects for heatmap builder
    class _Stub:
        def __init__(self, d):
            self.config = d.get("config", {})
            self.stats = type("S", (), {
                "roi_pct": d.get("roi_pct", 0),
                "win_rate": d.get("win_rate", 0),
                "max_drawdown_pct": d.get("max_drawdown_pct", 0),
                "avg_mfe_captured_pct": d.get("avg_mfe_captured_pct", 0),
            })()

    stubs = [_Stub(d) for d in _last_sweep_results]
    heatmap = build_heatmap_data(stubs, request.param_x, request.param_y, request.metric)
    return JSONResponse(content=heatmap)


@app.post("/api/dataset/refresh")
async def refresh_dataset():
    """Force-refresh the in-memory trade cache from Supabase."""
    clear_cache()
    trades = await load_trades(force_refresh=True)
    meta = get_dataset_meta(trades)
    return JSONResponse(content={"message": f"Dataset refreshed: {len(trades)} trades loaded.", "meta": meta})
