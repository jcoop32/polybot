#!/usr/bin/env python3
"""
Paper Copy Trader — Watch a Polymarket wallet and simulate copying its trades.

Polls the Polymarket Data API for a target wallet's recent trades and
paper-trades every new BUY/SELL detected. No real orders are placed.

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

from perf import (
    create_session,
    update_ui,
    ui_renderer,
    calc_taker_fee,
)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

def csv_path_for_wallet(wallet: str) -> str:
    """Generate a per-wallet CSV filename."""
    short = wallet[:6] + "..." + wallet[-4:]
    return f"csv_logs/paper_copy_{short}.csv"
POLL_INTERVAL = 1  # seconds between polls

# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

bot_start_time: float = time.time()
feed_status: str = "starting"
last_poll_time: float = 0.0
polls_completed: int = 0

# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Record of a single paper-copied trade."""
    trade_id: str
    timestamp: str
    token_id: str
    market_title: str
    slug: str
    side: str                # BUY or SELL
    outcome: str             # Up, Down, Yes, No, etc.
    original_price: float    # price the target trader paid
    original_size_usd: float # how much they wagered
    paper_amount: float      # our paper bet
    taker_fee: float
    pnl: float
    balance_after: float

@dataclass
class PaperPosition:
    """An open paper position."""
    token_id: str
    side: str
    entry_price: float
    amount: float
    shares: float
    market_title: str
    slug: str
    outcome: str
    opened_at: str

@dataclass
class Stats:
    """Running statistics across all paper trades."""
    balance: float = 100.0
    starting_balance: float = 100.0
    trades: list[Trade] = field(default_factory=list)
    total_copied: int = 0
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
                "slug", "side", "outcome",
                "original_price", "original_size_usd",
                "paper_amount", "taker_fee",
                "pnl", "balance_after",
                "cumulative_pnl", "total_copied", "win_rate",
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
            trade.slug,
            trade.side,
            trade.outcome,
            f"{trade.original_price:.4f}",
            f"{trade.original_size_usd:.2f}",
            f"{trade.paper_amount:.2f}",
            f"{trade.taker_fee:.4f}",
            f"{trade.pnl:.4f}",
            f"{trade.balance_after:.4f}",
            f"{stats.realized_pnl:.4f}",
            stats.total_copied,
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
    pseudonym: str = state.get("pseudonym", "")
    positions: dict = state.get("positions", {})
    trade_amount: float = state.get("trade_amount", 5.0)

    if not stats:
        return ""

    bal_c = GREEN if stats.balance >= stats.starting_balance else RED
    pnl_c = GREEN if stats.realized_pnl >= 0 else RED
    pnl_s = "+" if stats.realized_pnl >= 0 else ""

    # Status indicator
    if feed_status == "polling":
        status_dot = f"{GREEN}●{R}"
        status_text = "POLLING"
    elif feed_status == "starting":
        status_dot = f"{YELLOW}●{R}"
        status_text = "STARTING"
    else:
        status_dot = f"{RED}●{R}"
        status_text = "ERROR"

    # ── Header ──
    w(f"  {B}┌{'─' * 62}┐{R}\n")
    w(f"  {B}│{R}  📋 {B}PAPER COPY TRADER{R}"
      f"                  {D}Simulated{R}    {B}│{R}\n")

    # Show pseudonym if known
    label = pseudonym if pseudonym else f"{wallet[:20]}...{wallet[-6:]}"
    pad = max(1, 55 - len(label))
    w(f"  {B}│{R}  {D}👁️  {label}{R}{' ' * pad}{B}│{R}\n")
    w(f"  {B}├{'─' * 62}┤{R}\n")
    uptime_str = format_uptime(bot_start_time)
    w(f"  {B}│{R}  💰 {bal_c}{B}${stats.balance:.4f}{R}"
      f"     Bet: ${trade_amount:.2f}"
      f"     🕐 {uptime_str}"
      f"{' ' * max(1, 22 - len(uptime_str))}{B}│{R}\n")
    w(f"  {B}└{'─' * 62}┘{R}\n")

    # ── Feed Status ──
    age = int(time.time() - last_poll_time) if last_poll_time > 0 else 0
    w(f"\n  {status_dot} {status_text}  "
      f"{D}(every {POLL_INTERVAL}s · last {age}s ago · #{polls_completed}){R}\n")

    # ── Open Positions ──
    if positions:
        w(f"\n  {B}Open Positions ({len(positions)}){R}\n")
        w(f"  {D}{'─' * 62}{R}\n")
        for tid, pos in list(positions.items())[:5]:
            w(f"  {CYAN}►{R} {pos.market_title[:45]}\n")
            w(f"    {D}Entry ${pos.entry_price:.4f}  "
              f"{pos.outcome}  "
              f"${pos.amount:.2f}{R}\n")
        if len(positions) > 5:
            w(f"  {D}... and {len(positions) - 5} more{R}\n")
    else:
        w(f"\n  {D}No open positions{R}\n")

    # ── Stats Grid ──
    if stats.total_copied > 0:
        wr = stats.win_rate
        wr_c = GREEN if wr >= 50 else YELLOW
        w(f"\n  {D}{'─' * 62}{R}\n")
        w(f"  Copied {B}{stats.total_copied}{R}"
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
        for t in stats.trades[-6:]:
            side_c = GREEN if t.side == "BUY" else MAGENTA
            pnl_str = ""
            if t.pnl != 0:
                tc = GREEN if t.pnl > 0 else RED
                ps = "+" if t.pnl > 0 else ""
                pnl_str = f"  {tc}{ps}${t.pnl:.4f}{R}"
            title_short = t.market_title[:30] if len(t.market_title) > 30 else t.market_title
            w(f"  {side_c}{t.side:>4}{R}  "
              f"${t.original_price:.2f}  "
              f"${t.paper_amount:.2f}  "
              f"{t.outcome:>5}  "
              f"{title_short}"
              f"{pnl_str}\n")

    w(f"\n  {D}Ctrl+C for report{R}\n")

    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# PAPER TRADE PROCESSING
# ─────────────────────────────────────────────────────────────

def process_trade(
    activity: dict,
    stats: Stats,
    positions: dict,
    trade_amount: float,
) -> Trade | None:
    """Process a single activity event from the Data API into a paper trade."""

    tx_hash = activity.get("transactionHash", "")
    token_id = activity.get("asset", "")
    side = activity.get("side", "BUY").upper()
    price = float(activity.get("price", 0.5))
    usdc_size = float(activity.get("usdcSize", 0))
    title = activity.get("title", "Unknown Market")
    slug = activity.get("slug", "")
    outcome = activity.get("outcome", "")
    timestamp = activity.get("timestamp", 0)
    ts_str = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat() if timestamp else ""

    pnl = 0.0
    taker_fee = 0.0
    paper_amount = 0.0

    if side == "BUY":
        paper_amount = min(trade_amount, stats.balance)
        if paper_amount < 0.01:
            return None

        fee_rate = calc_taker_fee(price)
        taker_fee = paper_amount * fee_rate
        shares = paper_amount / price if price > 0 else 0

        stats.balance -= (paper_amount + taker_fee)
        stats.buys += 1
        stats.total_wagered += paper_amount

        # Track the position keyed by token_id
        positions[token_id] = PaperPosition(
            token_id=token_id,
            side="BUY",
            entry_price=price,
            amount=paper_amount,
            shares=shares,
            market_title=title,
            slug=slug,
            outcome=outcome,
            opened_at=ts_str,
        )

    elif side == "SELL":
        if token_id in positions:
            pos = positions.pop(token_id)
            paper_amount = pos.amount

            fee_rate = calc_taker_fee(price)
            taker_fee = paper_amount * fee_rate

            proceeds = pos.shares * price
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
            # Sell without a tracked buy — log but don't affect balance
            paper_amount = 0
        stats.sells += 1

    stats.peak_balance = max(stats.peak_balance, stats.balance)
    stats.valley_balance = min(stats.valley_balance, stats.balance)

    trade = Trade(
        trade_id=tx_hash[:16] if tx_hash else str(int(time.time())),
        timestamp=ts_str,
        token_id=token_id,
        market_title=title,
        slug=slug,
        side=side,
        outcome=outcome,
        original_price=price,
        original_size_usd=usdc_size,
        paper_amount=paper_amount,
        taker_fee=taker_fee,
        pnl=pnl,
        balance_after=stats.balance,
    )

    stats.trades.append(trade)
    stats.total_copied += 1

    return trade


# ─────────────────────────────────────────────────────────────
# POLL LOOP
# ─────────────────────────────────────────────────────────────

async def poll_trades(
    session: aiohttp.ClientSession,
    wallet: str,
    stats: Stats,
    positions: dict,
    trade_amount: float,
    csv_file: str,
):
    """Poll the Data API for new trades from the target wallet."""
    global feed_status, last_poll_time, polls_completed

    seen_txns: set[str] = set()
    pseudonym = ""

    # Initial fetch to seed the "seen" set so we don't replay history
    try:
        url = f"{DATA_API}/activity?user={wallet}&limit=50"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                for item in data:
                    tx = item.get("transactionHash", "")
                    if tx:
                        seen_txns.add(tx)
                    # Grab pseudonym from first response
                    if not pseudonym and item.get("pseudonym"):
                        pseudonym = item["pseudonym"]
                        update_ui(pseudonym=pseudonym)
                feed_status = "polling"
                last_poll_time = time.time()
                polls_completed += 1
    except Exception:
        pass

    update_ui(stats=stats, positions=positions)

    # Main poll loop
    while True:
        await asyncio.sleep(POLL_INTERVAL)

        try:
            url = f"{DATA_API}/activity?user={wallet}&limit=20"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    feed_status = "error"
                    update_ui(stats=stats, positions=positions)
                    continue

                data = await resp.json()

            feed_status = "polling"
            last_poll_time = time.time()
            polls_completed += 1

            # Process new trades (they come newest-first, reverse to process chronologically)
            new_trades = []
            for item in reversed(data):
                tx = item.get("transactionHash", "")
                if not tx or tx in seen_txns:
                    continue
                if item.get("type") != "TRADE":
                    continue

                seen_txns.add(tx)
                new_trades.append(item)

                # Grab pseudonym if not yet known
                if not pseudonym and item.get("pseudonym"):
                    pseudonym = item["pseudonym"]
                    update_ui(pseudonym=pseudonym)

            for item in new_trades:
                trade = process_trade(item, stats, positions, trade_amount)
                if trade:
                    log_trade_csv(csv_file, trade, stats)

            update_ui(stats=stats, positions=positions)

        except asyncio.CancelledError:
            raise
        except Exception:
            feed_status = "error"
            update_ui(stats=stats, positions=positions)


# ─────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────

def print_final_report(stats: Stats, wallet: str, positions: dict, csv_file: str):
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
    print(f"  ├─ Total copied:       {stats.total_copied}")
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
        avg = stats.realized_pnl / closed
        print(f"  ├─ Average P&L:        ${avg:.4f} / trade")
        print(f"  ├─ Best trade:         {GREEN}+${stats.best_trade:.4f}{R}")
        print(f"  └─ Worst trade:        {RED}${stats.worst_trade:.4f}{R}")
    elif stats.total_copied > 0:
        print(f"  {YELLOW}Only buys detected — no closed positions to evaluate.{R}")
    else:
        print(f"  {YELLOW}No trades were copied during this session.{R}")

    print()

    # Trade log
    if stats.trades:
        print(f"  {B}Trade Log{R}")
        print(f"  {'#':>3}  {'Side':>5}  {'Price':>7}  "
              f"{'Paper$':>7}  {'Orig$':>7}  {'P&L':>10}  {'Balance':>10}  {'Market'}")
        print(f"  {'─' * 75}")
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
                  f"${t.original_price:>5.2f}  "
                  f"${t.paper_amount:>5.2f}  "
                  f"${t.original_size_usd:>5.2f}  "
                  f"{pnl_str}  "
                  f"{bc}${t.balance_after:>8.4f}{R}  "
                  f"{t.market_title[:25]}")

    print()
    print(f"  {D}Full log saved to: {csv_file}{R}")
    print(f"{B}{'═' * 64}{R}")
    print()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main(wallet: str, starting_balance: float, trade_amount: float):
    """Run paper copy trader — poll wallet activity and simulate trades."""
    global bot_start_time
    bot_start_time = time.time()

    csv_file = csv_path_for_wallet(wallet)

    session = create_session()
    shutdown = asyncio.Event()

    stats = Stats(
        balance=starting_balance,
        starting_balance=starting_balance,
        peak_balance=starting_balance,
        valley_balance=starting_balance,
    )
    positions: dict[str, PaperPosition] = {}

    update_ui(
        stats=stats,
        wallet=wallet,
        positions=positions,
        trade_amount=trade_amount,
        pseudonym="",
    )

    ui_task = asyncio.create_task(ui_renderer(build_screen, shutdown))
    init_csv(csv_file)

    print(f"📋 Paper Copy Trader starting...")
    print(f"👁️  Watching wallet: {wallet}")
    print(f"💰 Starting balance: ${starting_balance:.2f}")
    print(f"🎯 Trade amount: ${trade_amount:.2f}")
    print(f"🔄 Polling every {POLL_INTERVAL}s\n")

    try:
        await poll_trades(session, wallet, stats, positions, trade_amount, csv_file)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutdown.set()
        ui_task.cancel()
        await session.close()
        print_final_report(stats, wallet, positions, csv_file)


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
    parser.add_argument(
        "--poll-interval", "-p",
        type=int,
        default=1,
        help="Seconds between API polls (default: 1)",
    )
    args = parser.parse_args()

    POLL_INTERVAL = args.poll_interval

    try:
        asyncio.run(main(
            wallet=args.wallet,
            starting_balance=args.balance,
            trade_amount=args.amount,
        ))
    except KeyboardInterrupt:
        print(f"\n👋 Goodbye!")