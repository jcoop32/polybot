#!/usr/bin/env python3
"""
Paper Copy Trader — Watch a Polymarket wallet and simulate copying its trades.

Connects to the Polymarket live data WebSocket, subscribes to a target wallet's
trade channel, and paper-trades every detected BUY/SELL. No real orders are placed.

Usage:
    python copy_trader.py --wallet 0xTargetAddress
    python copy_trader.py -w 0xTargetAddress --balance 50 --amount 2
"""

import argparse
import asyncio
import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import websockets

from perf import (
    create_session,
    fetch_orderbook_rest,
    update_ui,
    ui_renderer,
    calc_taker_fee,
)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

LIVE_DATA_WS = "wss://ws-live-data.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CSV_FILE = "csv_logs/paper_copy_trades.csv"

# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

bot_start_time: float = time.time()
ws_status: str = "connecting"

# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Record of a single paper-copied trade."""
    trade_id: str
    timestamp: str
    token_id: str
    market_title: str        # resolved from Gamma API if possible
    side: str                # BUY or SELL
    fill_price: float        # best ask/bid at time of detection
    amount: float            # paper amount wagered
    taker_fee: float
    pnl: float               # immediate estimated P&L (on sell) or 0 (on buy)
    balance_after: float

@dataclass
class PaperPosition:
    """An open paper position."""
    token_id: str
    side: str                # always BUY
    entry_price: float
    amount: float
    shares: float
    market_title: str
    opened_at: str

@dataclass
class Stats:
    """Running statistics across all paper trades."""
    balance: float = 100.0
    starting_balance: float = 100.0
    trades: list[Trade] = field(default_factory=list)
    total_trades: int = 0
    buys: int = 0
    sells: int = 0
    realized_pnl: float = 0.0
    total_wagered: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    wins: int = 0
    losses: int = 0
    peak_balance: float = 100.0
    valley_balance: float = 100.0

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return (self.wins / closed * 100) if closed > 0 else 0

    @property
    def roi(self) -> float:
        return (self.realized_pnl / self.total_wagered * 100) if self.total_wagered > 0 else 0

    @property
    def balance_change_pct(self) -> float:
        return ((self.balance - self.starting_balance) / self.starting_balance * 100)


# ─────────────────────────────────────────────────────────────
# MARKET TITLE RESOLUTION
# ─────────────────────────────────────────────────────────────

_title_cache: dict[str, str] = {}

async def resolve_market_title(session: aiohttp.ClientSession, token_id: str) -> str:
    """Try to resolve a token_id to a human-readable market title via Gamma API."""
    if token_id in _title_cache:
        return _title_cache[token_id]

    try:
        url = f"{GAMMA_API}/markets?clob_token_ids={token_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data and len(data) > 0:
                    title = data[0].get("question", data[0].get("title", token_id[:16]))
                    _title_cache[token_id] = title
                    return title
    except Exception:
        pass

    short = f"...{token_id[-8:]}"
    _title_cache[token_id] = short
    return short


# ─────────────────────────────────────────────────────────────
# CSV LOGGING
# ─────────────────────────────────────────────────────────────

def init_csv(path: str):
    """Create CSV file with headers if it doesn't exist."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not Path(path).exists():
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "trade_id", "timestamp", "token_id", "market_title",
                "side", "fill_price", "amount", "taker_fee",
                "pnl", "balance_after",
                "cumulative_pnl", "total_trades", "win_rate",
            ])


def log_trade_csv(path: str, trade: Trade, stats: Stats):
    """Append one trade to CSV."""
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade.trade_id,
            trade.timestamp,
            trade.token_id[:16],
            trade.market_title,
            trade.side,
            f"{trade.fill_price:.4f}",
            f"{trade.amount:.2f}",
            f"{trade.taker_fee:.4f}",
            f"{trade.pnl:.4f}",
            f"{trade.balance_after:.4f}",
            f"{stats.realized_pnl:.4f}",
            stats.total_trades,
            f"{stats.win_rate:.1f}",
        ])


# ─────────────────────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────────────────────

R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"


def format_uptime(start: float) -> str:
    elapsed = int(time.time() - start)
    h = elapsed // 3600
    m = (elapsed % 3600) // 60
    s = elapsed % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"


def build_screen(state: dict) -> str:
    """Build the entire dashboard into a single string for the UI renderer."""
    buf = io.StringIO()
    w = buf.write

    stats: Stats | None = state.get("stats")
    wallet: str = state.get("wallet", "")
    positions: dict = state.get("positions", {})
    trade_amount: float = state.get("trade_amount", 5.0)

    if not stats:
        return ""

    bal_c = GREEN if stats.balance >= stats.starting_balance else RED
    pnl_c = GREEN if stats.realized_pnl >= 0 else RED
    pnl_s = "+" if stats.realized_pnl >= 0 else ""

    # Status indicator
    if ws_status == "connected":
        status_dot = f"{GREEN}●{R}"
    elif ws_status == "reconnecting":
        status_dot = f"{YELLOW}●{R}"
    else:
        status_dot = f"{RED}●{R}"

    # ── Header ──
    w(f"  {B}┌{'─' * 62}┐{R}\n")
    w(f"  {B}│{R}  📋 {B}PAPER COPY TRADER{R}"
      f"                  {D}Simulated{R}    {B}│{R}\n")
    w(f"  {B}│{R}  {D}Watching: {wallet[:20]}...{wallet[-6:]}{R}"
      f"{' ' * max(1, 30 - len(wallet[:20]))}{B}│{R}\n")
    w(f"  {B}├{'─' * 62}┤{R}\n")
    uptime_str = format_uptime(bot_start_time)
    w(f"  {B}│{R}  💰 {bal_c}{B}${stats.balance:.4f}{R}"
      f"     Bet: ${trade_amount:.2f}"
      f"     🕐 {uptime_str}"
      f"{' ' * max(1, 22 - len(uptime_str))}{B}│{R}\n")
    w(f"  {B}└{'─' * 62}┘{R}\n")

    # ── Connection Status ──
    w(f"\n  {status_dot} WebSocket  {B}{ws_status.upper()}{R}\n")

    # ── Open Positions ──
    if positions:
        w(f"\n  {B}Open Positions ({len(positions)}){R}\n")
        w(f"  {D}{'─' * 62}{R}\n")
        for tid, pos in list(positions.items())[:5]:
            w(f"  {CYAN}►{R} {pos.market_title[:40]}\n")
            w(f"    {D}Entry ${pos.entry_price:.4f}  "
              f"Shares {pos.shares:.2f}  "
              f"${pos.amount:.2f}{R}\n")
    else:
        w(f"\n  {D}No open positions{R}\n")

    # ── Stats Grid ──
    if stats.total_trades > 0:
        wr = stats.win_rate
        wr_c = GREEN if wr >= 50 else YELLOW
        w(f"\n  {D}{'─' * 62}{R}\n")
        w(f"  Trades {B}{stats.total_trades}{R}"
          f"  │  Buys {CYAN}{stats.buys}{R}"
          f"  Sells {MAGENTA}{stats.sells}{R}"
          f"  │  P&L {pnl_c}{B}{pnl_s}${stats.realized_pnl:.4f}{R}\n")
        closed = stats.wins + stats.losses
        if closed > 0:
            w(f"  W{GREEN} {stats.wins}{R} L{RED} {stats.losses}{R}"
              f"  │  WR {wr_c}{B}{wr:.0f}%{R}"
              f"  │  ROI {pnl_c}{stats.roi:.1f}%{R}\n")
        w(f"  {D}{'─' * 62}{R}\n")

    # ── Recent Trades ──
    if stats.trades:
        w(f"\n  {B}Recent Trades{R}\n")
        for t in stats.trades[-5:]:
            side_c = GREEN if t.side == "BUY" else MAGENTA
            pnl_str = ""
            if t.pnl != 0:
                tc = GREEN if t.pnl > 0 else RED
                ps = "+" if t.pnl > 0 else ""
                pnl_str = f"  {tc}{ps}${t.pnl:.4f}{R}"
            w(f"  {side_c}{t.side:>4}{R}  "
              f"${t.fill_price:.4f}  "
              f"${t.amount:.2f}  "
              f"{t.market_title[:30]}"
              f"{pnl_str}\n")

    w(f"\n  {D}Ctrl+C for report{R}\n")

    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# PAPER TRADE EXECUTION
# ─────────────────────────────────────────────────────────────

async def paper_execute(
    session: aiohttp.ClientSession,
    trade_id: str,
    token_id: str,
    side: str,
    stats: Stats,
    positions: dict,
    trade_amount: float,
):
    """Simulate a copied trade using current orderbook prices."""
    global ws_status

    timestamp = datetime.now(timezone.utc).isoformat()
    market_title = await resolve_market_title(session, token_id)

    # Fetch current orderbook for realistic fill price
    ob = await fetch_orderbook_rest(session, token_id)
    fill_price = ob["ask_price"] if side == "BUY" else ob["bid_price"]

    # If we can't get a price, use a fallback
    if fill_price <= 0:
        fill_price = 0.50  # assume 50/50

    pnl = 0.0
    taker_fee = 0.0
    amount = 0.0

    if side == "BUY":
        amount = min(trade_amount, stats.balance)
        if amount < 0.01:
            return  # no balance

        fee_rate = calc_taker_fee(fill_price)
        taker_fee = amount * fee_rate
        shares = amount / fill_price if fill_price > 0 else 0

        stats.balance -= (amount + taker_fee)
        stats.buys += 1
        stats.total_wagered += amount

        # Track position
        positions[token_id] = PaperPosition(
            token_id=token_id,
            side="BUY",
            entry_price=fill_price,
            amount=amount,
            shares=shares,
            market_title=market_title,
            opened_at=timestamp,
        )

    elif side == "SELL":
        if token_id in positions:
            pos = positions.pop(token_id)
            amount = pos.amount

            fee_rate = calc_taker_fee(fill_price)
            taker_fee = pos.amount * fee_rate

            # Sell proceeds: shares * fill_price
            proceeds = pos.shares * fill_price
            pnl = proceeds - pos.amount - taker_fee

            stats.balance += (proceeds - taker_fee)
            stats.realized_pnl += pnl

            if pnl > 0:
                stats.wins += 1
            else:
                stats.losses += 1

            stats.best_trade = max(stats.best_trade, pnl)
            stats.worst_trade = min(stats.worst_trade, pnl)
        else:
            # Sell without a tracked position — just log it
            amount = trade_amount
            stats.sells += 1

            trade = Trade(
                trade_id=trade_id,
                timestamp=timestamp,
                token_id=token_id,
                market_title=market_title,
                side=side,
                fill_price=fill_price,
                amount=0,
                taker_fee=0,
                pnl=0,
                balance_after=stats.balance,
            )
            stats.trades.append(trade)
            stats.total_trades += 1
            log_trade_csv(CSV_FILE, trade, stats)

            update_ui(stats=stats, positions=positions)
            return

        stats.sells += 1

    stats.peak_balance = max(stats.peak_balance, stats.balance)
    stats.valley_balance = min(stats.valley_balance, stats.balance)

    trade = Trade(
        trade_id=trade_id,
        timestamp=timestamp,
        token_id=token_id,
        market_title=market_title,
        side=side,
        fill_price=fill_price,
        amount=amount,
        taker_fee=taker_fee,
        pnl=pnl,
        balance_after=stats.balance,
    )

    stats.trades.append(trade)
    stats.total_trades += 1

    log_trade_csv(CSV_FILE, trade, stats)
    update_ui(stats=stats, positions=positions)


# ─────────────────────────────────────────────────────────────
# WEBSOCKET WATCHER
# ─────────────────────────────────────────────────────────────

processed_trades: set = set()

async def watch_trader(
    session: aiohttp.ClientSession,
    wallet: str,
    stats: Stats,
    positions: dict,
    trade_amount: float,
):
    """Persistent WebSocket listening to Polymarket RTDS for a specific user."""
    global ws_status

    while True:
        try:
            ws_status = "connecting"
            update_ui(stats=stats, positions=positions)

            async with websockets.connect(LIVE_DATA_WS, ping_interval=20) as ws:
                # Subscribe to the target user's trade channel
                sub_msg = {
                    "type": "subscribe",
                    "channel": "user",
                    "address": wallet.lower(),
                }
                await ws.send(json.dumps(sub_msg))
                ws_status = "connected"
                update_ui(stats=stats, positions=positions)

                async for msg in ws:
                    data = json.loads(msg)

                    # Filter for matched trade events
                    if data.get("event") == "trade" and data.get("status") == "MATCHED":
                        trade_id = data.get("id", "")

                        # Deduplication
                        if trade_id in processed_trades:
                            continue
                        processed_trades.add(trade_id)

                        side = data.get("side", "BUY").upper()
                        token_id = data.get("asset_id", "")

                        if not token_id:
                            continue

                        # Fire paper execution async so we don't block the stream
                        asyncio.create_task(
                            paper_execute(
                                session, trade_id, token_id, side,
                                stats, positions, trade_amount,
                            )
                        )

        except websockets.ConnectionClosed:
            ws_status = "reconnecting"
            update_ui(stats=stats, positions=positions)
        except Exception:
            ws_status = "reconnecting"
            update_ui(stats=stats, positions=positions)

        await asyncio.sleep(2)


# ─────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────

def print_final_report(stats: Stats, wallet: str, positions: dict):
    """Print comprehensive final report."""
    print("\033[2J\033[H", end="", flush=True)
    pnl_c = GREEN if stats.realized_pnl >= 0 else RED
    pnl_s = "+" if stats.realized_pnl >= 0 else ""
    bal_c = GREEN if stats.balance >= stats.starting_balance else RED
    bal_s = "+" if stats.balance_change_pct >= 0 else ""

    print()
    print(f"{B}{'═' * 64}{R}")
    print(f"{B}  📋 PAPER COPY TRADER — FINAL REPORT{R}")
    print(f"{B}{'═' * 64}{R}")
    print()
    print(f"  {D}Target wallet: {wallet}{R}")
    print(f"  {D}Uptime: {format_uptime(bot_start_time)}{R}")
    print()
    print(f"  {B}Bankroll{R}")
    print(f"  ├─ Starting balance:   ${stats.starting_balance:.2f}")
    print(f"  ├─ Final balance:      {bal_c}{B}${stats.balance:.4f}{R} "
          f"({bal_c}{bal_s}{stats.balance_change_pct:.1f}%{R})")
    print(f"  ├─ Peak balance:       {GREEN}${stats.peak_balance:.4f}{R}")
    print(f"  └─ Valley balance:     {RED}${stats.valley_balance:.4f}{R}")
    print()
    print(f"  {B}Overview{R}")
    print(f"  ├─ Total trades:       {stats.total_trades}")
    print(f"  ├─ Buys:               {stats.buys}")
    print(f"  ├─ Sells:              {stats.sells}")
    print(f"  └─ Open positions:     {len(positions)}")
    print()

    closed = stats.wins + stats.losses
    if closed > 0:
        print(f"  {B}Performance{R}")
        print(f"  ├─ Win rate:           {B}{stats.win_rate:.1f}%{R}  "
              f"({stats.wins}W / {stats.losses}L)")
        print(f"  ├─ Realized P&L:       {pnl_c}{B}{pnl_s}${stats.realized_pnl:.4f}{R}")
        print(f"  ├─ Total wagered:      ${stats.total_wagered:.2f}")
        print(f"  ├─ ROI:                {pnl_c}{pnl_s}{stats.roi:.1f}%{R}")
        if closed > 0:
            avg = stats.realized_pnl / closed
            print(f"  ├─ Average P&L:        ${avg:.4f} / trade")
        print(f"  ├─ Best trade:         {GREEN}+${stats.best_trade:.4f}{R}")
        print(f"  └─ Worst trade:        {RED}${stats.worst_trade:.4f}{R}")
    elif stats.total_trades > 0:
        print(f"  {YELLOW}Only buys detected — no closed positions to evaluate.{R}")
    else:
        print(f"  {YELLOW}No trades were copied during this session.{R}")

    print()

    # Trade log
    if stats.trades:
        print(f"  {B}Trade Log{R}")
        print(f"  {'#':>3}  {'Side':>5}  {'Fill @':>8}  "
              f"{'Amount':>8}  {'P&L':>10}  {'Balance':>10}  {'Market'}")
        print(f"  {'─' * 70}")
        for i, t in enumerate(stats.trades, 1):
            side_c = GREEN if t.side == "BUY" else MAGENTA
            if t.pnl != 0:
                pc = GREEN if t.pnl > 0 else RED
                ps = "+" if t.pnl > 0 else ""
                pnl_str = f"{pc}{ps}${t.pnl:>8.4f}{R}"
            else:
                pnl_str = f"{D}{'—':>10}{R}"
            bc = GREEN if t.balance_after >= stats.starting_balance else RED
            print(f"  {i:>3}  {side_c}{t.side:>5}{R}  "
                  f"${t.fill_price:>6.4f}  "
                  f"${t.amount:>6.2f}  "
                  f"{pnl_str}  "
                  f"{bc}${t.balance_after:>8.4f}{R}  "
                  f"{t.market_title[:25]}")

    print()
    print(f"  {D}Full log saved to: {CSV_FILE}{R}")
    print(f"{B}{'═' * 64}{R}")
    print()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main(wallet: str, starting_balance: float, trade_amount: float):
    """Run paper copy trader — watch wallet and simulate trades."""
    global bot_start_time
    bot_start_time = time.time()

    session = create_session()
    shutdown = asyncio.Event()

    stats = Stats(
        balance=starting_balance,
        starting_balance=starting_balance,
        peak_balance=starting_balance,
        valley_balance=starting_balance,
    )
    positions: dict[str, PaperPosition] = {}

    # Inject wallet and trade_amount into UI state so build_screen can access them
    update_ui(
        stats=stats,
        wallet=wallet,
        positions=positions,
        trade_amount=trade_amount,
    )

    ui_task = asyncio.create_task(ui_renderer(build_screen, shutdown))

    init_csv(CSV_FILE)

    print(f"📋 Paper Copy Trader starting...")
    print(f"👁️  Watching wallet: {wallet}")
    print(f"💰 Starting balance: ${starting_balance:.2f}")
    print(f"🎯 Trade amount: ${trade_amount:.2f}\n")

    try:
        await watch_trader(session, wallet, stats, positions, trade_amount)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutdown.set()
        ui_task.cancel()
        await session.close()
        print_final_report(stats, wallet, positions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Paper Copy Trader — simulate copying a Polymarket wallet's trades"
    )
    parser.add_argument(
        "--wallet", "-w",
        type=str,
        required=True,
        help="Target wallet address to copy trades from (e.g. 0x1234...)",
    )
    parser.add_argument(
        "--balance", "-b",
        type=float,
        default=100.0,
        help="Starting paper balance in USD (default: $100)",
    )
    parser.add_argument(
        "--amount", "-a",
        type=float,
        default=5.0,
        help="Fixed amount to paper-trade per copied trade (default: $5)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(
            wallet=args.wallet,
            starting_balance=args.balance,
            trade_amount=args.amount,
        ))
    except KeyboardInterrupt:
        print(f"\n👋 Goodbye!")