#!/usr/bin/env python3
"""
validate_smart_money_db.py — Pre-launch validation script (Phase 2)

Runs a series of checks before relaunching paper trading:
  1. Smart Money wallet count in DB
  2. Helius WebSocket connectivity test
  3. DexScreener fallback API reachability
  4. Parameter version check (should be v1.3)
  5. Recent PINTU_B signal count (7 days)

Usage:
  python scripts/validate_smart_money_db.py

Run this from the project root BEFORE starting memescanner.service.
"""

import asyncio
import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import websockets

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

OK = f"{GREEN}OK{RESET}"
FAIL = f"{RED}FAIL{RESET}"
WARN = f"{YELLOW}WARN{RESET}"
INFO = f"{BLUE}INFO{RESET}"


async def check_wallet_count(db_manager) -> bool:
    print(f"\n[1/5] Smart Money Wallet Count")
    try:
        wallets = await db_manager.get_smart_money_wallets(active_only=True)
        count = len(wallets)
        if count == 0:
            print(f"  [{FAIL}] 0 active wallets found in DB!")
            print(f"  [{WARN}] Action Required: Add wallets to smart_money_profiles table.")
            print(f"          Pintu B will be INACTIVE until wallets are seeded.")
            return False
        elif count < 10:
            print(f"  [{WARN}] Only {count} active wallet(s) — consider adding more (target: >= 50)")
            return True
        else:
            print(f"  [{OK}] {count} active Smart Money wallet(s) found")
            for w in wallets[:3]:
                addr = w.get("wallet_address", "")
                label = w.get("label", "unlabeled")
                print(f"       • {addr[:12]}... ({label})")
            if count > 3:
                print(f"       • ... and {count - 3} more")
            return True
    except Exception as e:
        print(f"  [{FAIL}] DB query failed: {e}")
        return False


async def check_helius_ws(helius_ws_url: str) -> bool:
    print(f"\n[2/5] Helius WebSocket Connectivity")
    if not helius_ws_url or "your-api-key" in helius_ws_url:
        print(f"  [{FAIL}] Helius WS URL not configured")
        return False
    try:
        import json
        async with websockets.connect(helius_ws_url, open_timeout=10) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "slotSubscribe", "params": []
            }))
            response = await asyncio.wait_for(ws.recv(), timeout=5.0)
            print(f"  [{OK}] Helius WebSocket connected successfully")
            print(f"         Response: {str(response)[:80]}...")
            return True
    except asyncio.TimeoutError:
        print(f"  [{WARN}] Helius WS connected but no response in 5s (may still work)")
        return True
    except Exception as e:
        err_str = str(e).lower()
        if "429" in err_str or "too many" in err_str:
            print(f"  [{WARN}] Helius WS got 429 (rate limited) — bot will use exponential backoff")
            print(f"          This is expected on free tier. Bot will auto-reconnect.")
        else:
            print(f"  [{FAIL}] Helius WS connection failed: {e}")
        return False


async def check_dexscreener_fallback() -> bool:
    print(f"\n[3/5] DexScreener Fallback API")
    # Test with USDC mint (always has pairs)
    test_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    url = f"https://api.dexscreener.com/latest/dex/tokens/{test_mint}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                pairs = resp.json().get("pairs", [])
                print(f"  [{OK}] DexScreener API reachable ({len(pairs)} pairs returned for test query)")
                return True
            else:
                print(f"  [{FAIL}] DexScreener returned HTTP {resp.status_code}")
                return False
    except Exception as e:
        print(f"  [{FAIL}] DexScreener unreachable: {e}")
        return False


async def check_parameter_version(db_manager) -> bool:
    print(f"\n[4/5] Parameter Version Check")
    from src.paper_trading.position_tracker import FROZEN_PARAMS
    version = FROZEN_PARAMS.get("parameter_version", "unknown")
    stagnancy_mins = FROZEN_PARAMS.get("stagnancy_check_minutes", "NOT SET")
    stagnancy_mfe = FROZEN_PARAMS.get("stagnancy_mfe_threshold_pct", "NOT SET")

    if version == "v1.3":
        print(f"  [{OK}] FROZEN_PARAMS version: {version}")
        print(f"         Stagnancy window: {stagnancy_mins}m | MFE threshold: {stagnancy_mfe}%")
        return True
    else:
        print(f"  [{FAIL}] FROZEN_PARAMS version is '{version}', expected 'v1.3'")
        print(f"          Make sure position_tracker.py changes are deployed!")
        return False


async def check_recent_pintu_b(db_manager) -> bool:
    print(f"\n[5/5] Recent Pintu B Signal Count (last 7 days)")
    try:
        from datetime import datetime, timezone, timedelta
        since = (datetime.now(tz=timezone.utc) - timedelta(days=7)).isoformat()
        rows = await db_manager.query(
            "paper_trade_positions",
            filters={"signal_source": "eq.PINTU_B"},
            limit=500
        )
        recent = [r for r in rows if r.get("entry_time", "") >= since]
        total = len(rows)
        recent_count = len(recent)
        if total == 0:
            print(f"  [{WARN}] 0 Pintu B positions ever — Pintu B has never fired")
            print(f"          Expected if wallets were never seeded. Seed wallets before relaunch.")
        else:
            print(f"  [{INFO}] {total} total Pintu B positions (all time)")
            print(f"  [{INFO}] {recent_count} Pintu B positions in last 7 days")
        return True
    except Exception as e:
        print(f"  [{WARN}] Could not query Pintu B history: {e}")
        return True  # Non-critical


async def main():
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  MemeScanner — Pre-Launch Validator (Phase 2 / v1.3)")
    print(f"{sep}")

    from src.config import settings
    from src.database.client import db_manager

    if not db_manager._connected:
        db_manager.connect()

    results = []
    results.append(await check_wallet_count(db_manager))
    results.append(await check_helius_ws(settings.helius_ws_url))
    results.append(await check_dexscreener_fallback())
    results.append(await check_parameter_version(db_manager))
    results.append(await check_recent_pintu_b(db_manager))

    passed = sum(results)
    total = len(results)

    print(f"\n{sep}")
    if passed == total:
        print(f"  [{OK}] All {total} checks passed — READY TO RELAUNCH!")
        print(f"\n  Run: sudo systemctl start memescanner")
    elif passed >= 3:
        print(f"  [{WARN}] {passed}/{total} checks passed — review warnings above")
        print(f"\n  Bot can launch but monitor Pintu B activity closely.")
        print(f"  Run: sudo systemctl start memescanner")
    else:
        print(f"  [{FAIL}] {passed}/{total} checks passed — DO NOT launch yet!")
        print(f"\n  Fix the issues above before relaunching.")
    print(f"{sep}\n")


if __name__ == "__main__":
    asyncio.run(main())
