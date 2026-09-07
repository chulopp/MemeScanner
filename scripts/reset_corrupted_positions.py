"""
Script to mark corrupted positions ($WOFI and $MANIFEST) as CORRUPTED_RESET in Supabase.
"""
import asyncio
from datetime import datetime, timezone
from src.database.client import db_manager
from src.utils.logger import logger

CORRUPTED_SYMBOLS = ["WOFI", "MANIFEST", "HOOD"]

async def main():
    db_manager.connect()
    now_utc = datetime.now(tz=timezone.utc).isoformat()
    
    for sym in CORRUPTED_SYMBOLS:
        positions = await db_manager.query("paper_trade_positions", filters={"symbol": f"eq.{sym}"})
        if not positions:
            logger.info(f"No position found for {sym}")
            continue
        for pos in positions:
            pos_id = pos["id"]
            ok = await db_manager.update(
                "paper_trade_positions",
                {
                    "exit_reason": "CORRUPTED_RESET",
                    "realized_return_pct": 0.0,
                    "exit_price_usd": pos.get("entry_price_usd", 0.0),
                    "exit_time": now_utc,
                    "updated_at": now_utc,
                },
                filters={"id": f"eq.{pos_id}"}
            )
            logger.info(f"Reset corrupted position {sym} ({pos_id}): ok={ok}")

if __name__ == "__main__":
    asyncio.run(main())
