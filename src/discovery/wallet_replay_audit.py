"""
Wallet Replay Audit Tool — Pendekatan 2 (Historical On-Chain Trade Audit)

Fetches all historical BUY/SELL swap transactions for a tracked Smart Money wallet
from Helius, reconstructs round-trip trades (entry + exit), and generates a
performance summary: realized PnL, win rate, avg hold duration, and avg entry/exit prices.

Usage (CLI):
    python -m src.discovery audit-wallet <WALLET_ADDRESS>
    python -m src.discovery audit-wallet <WALLET_ADDRESS> --limit 100

This tool runs entirely on historical Helius transaction data — no OHLCV APIs required.
The entry/exit prices are computed directly from on-chain swap ratios:
    entry_price_sol = sol_spent / tokens_received
    exit_price_sol  = sol_received / tokens_sold
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import httpx

# Allow running from project root
sys.path.insert(0, os.getcwd())
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.config import settings
from src.utils.logger import logger

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SwapEvent:
    """A single BUY or SELL swap event parsed from on-chain Helius data."""
    tx_signature: str
    timestamp: datetime
    direction: str            # 'BUY' | 'SELL'
    token_mint: str
    sol_amount: float         # SOL spent (BUY) or SOL received (SELL)
    token_amount: float       # Tokens received (BUY) or tokens sold (SELL)
    price_sol_per_token: float
    source_platform: str      # e.g., 'JUPITER', 'RAYDIUM', 'PUMPFUN'


@dataclass
class RoundTripTrade:
    """A matched BUY + SELL pair representing a complete trade lifecycle."""
    token_mint: str
    buy_event: SwapEvent
    sell_event: Optional[SwapEvent]      # None if position still open

    @property
    def is_closed(self) -> bool:
        return self.sell_event is not None

    @property
    def realized_pnl_sol(self) -> float:
        if not self.sell_event:
            return 0.0
        return self.sell_event.sol_amount - self.buy_event.sol_amount

    @property
    def return_pct(self) -> float:
        if not self.sell_event or self.buy_event.sol_amount == 0:
            return 0.0
        return (self.realized_pnl_sol / self.buy_event.sol_amount) * 100.0

    @property
    def hold_duration_minutes(self) -> float:
        if not self.sell_event:
            return 0.0
        delta = self.sell_event.timestamp - self.buy_event.timestamp
        return abs(delta.total_seconds()) / 60.0


@dataclass
class WalletAuditReport:
    """Final performance summary for a Smart Money wallet."""
    wallet_address: str
    total_trades: int
    closed_trades: int
    open_positions: int
    total_pnl_sol: float
    win_count: int
    loss_count: int
    win_rate_pct: float
    avg_return_pct: float
    avg_hold_minutes: float
    best_trade_pct: float
    worst_trade_pct: float
    round_trips: list[RoundTripTrade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# On-chain transaction fetcher
# ---------------------------------------------------------------------------

async def _fetch_swap_transactions(
    wallet: str,
    api_key: str,
    limit: int = 100,
) -> list[dict]:
    """Fetch up to `limit` SWAP transactions for `wallet` from Helius API."""
    url = (
        f"https://api.helius.xyz/v0/addresses/{wallet}/transactions"
        f"?api-key={api_key}&type=SWAP&limit={limit}"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"Helius API error: HTTP {resp.status_code}")
        return resp.json()


# ---------------------------------------------------------------------------
# Swap parser
# ---------------------------------------------------------------------------

_SKIP_MINTS = {
    "So11111111111111111111111111111111111111112",
    "So11111111111111111111111111111111111111111",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}

def _parse_swaps(txs: list[dict], wallet: str) -> list[SwapEvent]:
    """Parse a list of Helius transaction objects into SwapEvent records."""
    events: list[SwapEvent] = []

    for tx in txs:
        ts_raw = tx.get("timestamp")
        if not ts_raw:
            continue
        dt = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        sig = tx.get("signature", "")
        source = tx.get("source", "UNKNOWN")

        swap_data = (tx.get("events") or {}).get("swap") or {}
        native_in = (swap_data.get("nativeInput") or {})
        native_out = (swap_data.get("nativeOutput") or {})
        token_in_list = swap_data.get("tokenInputs") or []
        token_out_list = swap_data.get("tokenOutputs") or []

        sol_in = float(native_in.get("amount", 0)) / 1e9
        sol_out = float(native_out.get("amount", 0)) / 1e9

        def _parse_token_amount(tok: dict) -> tuple[str, float]:
            mint = tok.get("mint", "")
            raw = tok.get("rawTokenAmount") or {}
            amt = float(raw.get("tokenAmount", 0) or 0)
            dec = int(raw.get("decimals", 6) or 6)
            return mint, amt / (10 ** dec)

        # ---- BUY: SOL in, token out ----
        if sol_in > 0 and token_out_list:
            mint, token_amt = _parse_token_amount(token_out_list[0])
            if mint and mint not in _SKIP_MINTS and token_amt > 0 and sol_in >= 0.001:
                events.append(SwapEvent(
                    tx_signature=sig,
                    timestamp=dt,
                    direction="BUY",
                    token_mint=mint,
                    sol_amount=sol_in,
                    token_amount=token_amt,
                    price_sol_per_token=sol_in / token_amt,
                    source_platform=source,
                ))

        # ---- SELL: token in, SOL out ----
        elif sol_out > 0 and token_in_list:
            mint, token_amt = _parse_token_amount(token_in_list[0])
            if mint and mint not in _SKIP_MINTS and token_amt > 0 and sol_out >= 0.001:
                events.append(SwapEvent(
                    tx_signature=sig,
                    timestamp=dt,
                    direction="SELL",
                    token_mint=mint,
                    sol_amount=sol_out,
                    token_amount=token_amt,
                    price_sol_per_token=sol_out / token_amt,
                    source_platform=source,
                ))

    return events


# ---------------------------------------------------------------------------
# Trade reconstructor (match BUY → SELL)
# ---------------------------------------------------------------------------

def _match_round_trips(events: list[SwapEvent]) -> list[RoundTripTrade]:
    """
    Match BUY and SELL events into round-trip trade pairs per token mint.
    Strategy: FIFO — oldest buy is matched against oldest sell for the same mint.
    """
    # Separate by token mint
    buys_by_mint: dict[str, list[SwapEvent]] = {}
    sells_by_mint: dict[str, list[SwapEvent]] = {}

    for ev in sorted(events, key=lambda e: e.timestamp):
        if ev.direction == "BUY":
            buys_by_mint.setdefault(ev.token_mint, []).append(ev)
        else:
            sells_by_mint.setdefault(ev.token_mint, []).append(ev)

    round_trips: list[RoundTripTrade] = []
    for mint, buys in buys_by_mint.items():
        sells = sells_by_mint.get(mint, [])
        for i, buy in enumerate(buys):
            sell = sells[i] if i < len(sells) else None
            round_trips.append(RoundTripTrade(
                token_mint=mint,
                buy_event=buy,
                sell_event=sell,
            ))

    return round_trips


# ---------------------------------------------------------------------------
# Audit engine
# ---------------------------------------------------------------------------

async def audit_wallet(wallet_address: str, limit: int = 100) -> WalletAuditReport:
    """
    Full on-chain wallet audit: fetch swaps, reconstruct trades, compute metrics.

    Returns a WalletAuditReport with all trade details and aggregate statistics.
    """
    api_key = settings.helius_api_key
    if not api_key:
        raise ValueError("HELIUS_API_KEY is not configured.")

    logger.info(f"[WalletAudit] Fetching {limit} swap transactions for {wallet_address[:8]}...")
    txs = await _fetch_swap_transactions(wallet_address, api_key, limit)
    logger.info(f"[WalletAudit] Fetched {len(txs)} raw SWAP transactions.")

    events = _parse_swaps(txs, wallet_address)
    logger.info(f"[WalletAudit] Parsed {len(events)} valid BUY/SELL events.")

    round_trips = _match_round_trips(events)
    closed = [rt for rt in round_trips if rt.is_closed]
    open_pos = [rt for rt in round_trips if not rt.is_closed]

    total_pnl = sum(rt.realized_pnl_sol for rt in closed)
    wins = [rt for rt in closed if rt.realized_pnl_sol > 0]
    losses = [rt for rt in closed if rt.realized_pnl_sol <= 0]
    win_rate = (len(wins) / len(closed) * 100.0) if closed else 0.0
    avg_return = (sum(rt.return_pct for rt in closed) / len(closed)) if closed else 0.0
    avg_hold = (sum(rt.hold_duration_minutes for rt in closed) / len(closed)) if closed else 0.0
    returns = [rt.return_pct for rt in closed]
    best = max(returns) if returns else 0.0
    worst = min(returns) if returns else 0.0

    return WalletAuditReport(
        wallet_address=wallet_address,
        total_trades=len(round_trips),
        closed_trades=len(closed),
        open_positions=len(open_pos),
        total_pnl_sol=round(total_pnl, 4),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate_pct=round(win_rate, 1),
        avg_return_pct=round(avg_return, 2),
        avg_hold_minutes=round(avg_hold, 1),
        best_trade_pct=round(best, 2),
        worst_trade_pct=round(worst, 2),
        round_trips=round_trips,
    )


# ---------------------------------------------------------------------------
# CLI entry point (called from src/discovery/__main__.py)
# ---------------------------------------------------------------------------

def print_audit_report(report: WalletAuditReport):
    """Print a formatted audit report to stdout."""
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"  WALLET AUDIT REPORT")
    print(f"  Wallet: {report.wallet_address}")
    print(sep)
    print(f"  Total Trades Reconstructed : {report.total_trades}")
    print(f"  Closed (Buy+Sell matched)  : {report.closed_trades}")
    print(f"  Open Positions (Unsold)    : {report.open_positions}")
    print(f"  -----------------------------------------------")
    print(f"  Total Realized PnL         : {report.total_pnl_sol:+.4f} SOL")
    print(f"  Win Rate                   : {report.win_rate_pct:.1f}%  ({report.win_count}W / {report.loss_count}L)")
    print(f"  Avg Return per Trade       : {report.avg_return_pct:+.2f}%")
    print(f"  Avg Hold Duration          : {report.avg_hold_minutes:.1f} minutes")
    print(f"  Best Trade                 : {report.best_trade_pct:+.2f}%")
    print(f"  Worst Trade                : {report.worst_trade_pct:+.2f}%")
    print(f"{sep}")

    if report.round_trips:
        print(f"\n  {'Token Mint':<46} {'Dir':>6} {'SOL':>8} {'Return%':>9} {'Hold(m)':>8} {'Platform':<12}")
        print(f"  {'-'*100}")
        for rt in report.round_trips[:20]:  # Show first 20
            direction = "BUY+SELL" if rt.is_closed else "BUY OPEN"
            sol = rt.realized_pnl_sol if rt.is_closed else -rt.buy_event.sol_amount
            ret = f"{rt.return_pct:+.1f}%" if rt.is_closed else "OPEN"
            hold = f"{rt.hold_duration_minutes:.1f}" if rt.is_closed else "-"
            print(
                f"  {rt.token_mint:<46} {direction:>8} {sol:>+8.4f}  {ret:>9} {hold:>8}  "
                f"{rt.buy_event.source_platform:<12}"
            )
    print()


async def _cli_main(wallet_address: str, limit: int):
    report = await audit_wallet(wallet_address, limit)
    print_audit_report(report)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Audit a Smart Money wallet's on-chain trade history.")
    parser.add_argument("wallet", help="Solana wallet address to audit")
    parser.add_argument("--limit", type=int, default=100, help="Max transactions to fetch (default: 100)")
    args = parser.parse_args()
    asyncio.run(_cli_main(args.wallet, args.limit))
