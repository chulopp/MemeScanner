import asyncio
import sys
sys.path.insert(0, ".")
from src.database.client import db_manager

async def clean():
    db_manager.connect()
    c = db_manager._client
    
    # 1. Inspect ZCATWIF
    res = c.table("paper_trade_positions").select("*").eq("token_address", "F3hJ64M6xTmHYGgqkcgVPZx7sVMxb9FqUHBeXby3pump").execute()
    print("Found rows:", len(res.data))
    for r in res.data:
        print("Row:", r["id"], r["symbol"], r["exit_reason"], r["realized_return_pct"])
        if r.get("exit_reason") == "TP3" and (r.get("realized_return_pct") or 0) > 1000:
            print(f"Updating corrupted row {r['id']}...")
            c.table("paper_trade_positions").update({
                "exit_reason": "CORRUPTED_RESET",
                "realized_return_pct": 0.0,
                "exit_price_usd": r.get("entry_price_usd"),
                "skipped_reason": "RESET_CORRUPTED_PRICE_FALLBACK"
            }).eq("id", r["id"]).execute()
            print("Updated successfully!")

    # Verify
    res_after = c.table("paper_trade_positions").select("*").eq("token_address", "F3hJ64M6xTmHYGgqkcgVPZx7sVMxb9FqUHBeXby3pump").execute()
    print("After update:")
    for r in res_after.data:
        print("Row:", r["id"], r["symbol"], r["exit_reason"], r["realized_return_pct"])

if __name__ == "__main__":
    asyncio.run(clean())
