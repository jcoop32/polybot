#!/usr/bin/env python3
"""
PolySniper Paper Trader — Simulate trades across BTC 5-min candles.

Runs the exact same logic as sniper.py but never places real orders.
Instead, it records every decision and tracks simulated P&L.

Results are displayed live in the terminal and logged to
paper_trades.csv for later analysis.

Usage:
    pip install websockets aiohttp
    python paper_trade.py                  # Run until Ctrl+C
    python paper_trade.py --candles 20     # Run for 20 candles
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from perf import (
    json_loads, json_dumps, create_session,
    orderbook_ws_stream, get_cached_orderbook, fetch_orderbook_rest,
    update_ui, ui_renderer, adaptive_sleep,
)


# ─────────────────────────────────────────────────────────────
# CONFIGURATION (mirrors sniper.py exactly)
# ─────────────────────────────────────────────────────────────

STARTING_BALANCE = 100.0      # Starting paper bankroll
MAX_BUY_PRICE = 0.97          # Don't buy if best ask is above this
MIN_BUY_PRICE = 0.10          # Don't buy if best ask is below this (phantom liquidity)
SAFE_BUFFER_USD = 20.0        # BTC must be this far from candle open to trade
DEFAULT_TRADE_WINDOW = 15     # Buy in last N seconds (overridable via --window)
MAX_TRADE_USD = 5.0           # Simulated trade size per candle
MIN_ASK_SIZE_USD = 5.0        # Minimum orderbook depth in USD at best ask
MAX_SPREAD = 0.05             # Max acceptable bid-ask spread

# Confidence-based position sizing multipliers
CONFIDENCE_SIZING = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}  # LOW = small trade

# Risk management
DRAWDOWN_THRESHOLD = 0.50     # Pause trading if balance < 50% of peak
DRAWDOWN_COOLDOWN = 3         # Candles to sit out after drawdown trigger
LOSS_STREAK_LIMIT = 3         # Skip next candle after this many consecutive losses

# BTC price staleness
PRICE_STALE_SECONDS = 10      # Warn if price hasn't updated in this many seconds
PRICE_STALE_RECONNECT = 30    # Force WS reconnect if no update in this many seconds

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

# Price feed — Binance WebSocket primary, CoinCap HTTP fallback
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
COINCAP_URL = "https://api.coincap.io/v2/assets/bitcoin"

CSV_FILE = "paper_trades_v3.csv"

# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

btc_price: float = 0.0
btc_price_updated: float = 0.0  # timestamp of last successful price update
price_feed_status: str = "connecting"  # "ws_live", "http_fallback", "stale", "connecting"


# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Record of a single simulated trade."""
    candle_num: int
    slug: str
    title: str
    candle_start: str
    candle_end: str
    open_price: float
    close_price: float
    delta: float
    actual_direction: str       # "UP" or "DOWN"
    decision: str               # "BUY_UP", "BUY_DOWN", "SKIP_*"
    side_bought: str            # "UP", "DOWN", or ""
    buy_price: float            # Ask price we would have paid
    bet_amount: float           # USDC wagered
    won: bool
    pnl: float                  # Profit/loss for this trade
    pnl_pct: float              # P&L as percentage
    balance_after: float = 0.0  # Balance after this trade
    # ── Rich context fields ──
    ask_size: float = 0.0              # Size available at best ask
    book_depth_usd: float = 0.0        # Total USD depth at best ask
    opposing_ask_price: float = 0.0    # What the other side's best ask was
    spread: float = 0.0                # Spread on the side we looked at (ask - bid)
    btc_high: float = 0.0              # BTC high during this candle
    btc_low: float = 0.0               # BTC low during this candle
    btc_volatility: float = 0.0        # high - low range during candle
    decision_btc_price: float = 0.0    # BTC price at moment of decision
    decision_delta: float = 0.0        # Delta at moment of decision
    decision_remaining: float = 0.0    # Seconds remaining when decision was made
    confidence: str = ""               # Confidence label: HIGH / MEDIUM / LOW


@dataclass
class Stats:
    """Running statistics across all trades."""
    balance: float = STARTING_BALANCE
    trades: list[Trade] = field(default_factory=list)
    total_candles: int = 0
    total_trades: int = 0       # Candles where we would have bought
    total_skips: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_wagered: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    current_streak: int = 0     # +N for win streak, -N for loss streak
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    peak_balance: float = STARTING_BALANCE
    valley_balance: float = STARTING_BALANCE
    cooldown_remaining: int = 0  # Candles to skip (drawdown breaker)
    shadow_wins: int = 0         # Skipped candles where we would have won
    shadow_losses: int = 0       # Skipped candles where we would have lost

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_trades * 100) if self.total_trades > 0 else 0

    @property
    def roi(self) -> float:
        return (self.total_pnl / self.total_wagered * 100) if self.total_wagered > 0 else 0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0

    @property
    def balance_change_pct(self) -> float:
        return ((self.balance - STARTING_BALANCE) / STARTING_BALANCE * 100)

    def record(self, trade: Trade):
        self.trades.append(trade)
        self.total_candles += 1

        if trade.decision.startswith("BUY"):
            self.total_trades += 1
            self.total_wagered += trade.bet_amount
            self.total_pnl += trade.pnl
            self.balance += trade.pnl

            self.peak_balance = max(self.peak_balance, self.balance)
            self.valley_balance = min(self.valley_balance, self.balance)

            if trade.won:
                self.wins += 1
                self.current_streak = max(0, self.current_streak) + 1
                self.longest_win_streak = max(self.longest_win_streak, self.current_streak)
            else:
                self.losses += 1
                self.current_streak = min(0, self.current_streak) - 1
                self.longest_loss_streak = max(self.longest_loss_streak, abs(self.current_streak))

            self.best_trade = max(self.best_trade, trade.pnl)
            self.worst_trade = min(self.worst_trade, trade.pnl)
        else:
            self.total_skips += 1
            # Shadow tracking: would we have won if we traded?
            if trade.actual_direction and trade.decision_delta != 0:
                predicted = "UP" if trade.decision_delta > 0 else "DOWN"
                if predicted == trade.actual_direction:
                    self.shadow_wins += 1
                else:
                    self.shadow_losses += 1


# ─────────────────────────────────────────────────────────────
# BTC PRICE STREAM — Binance WebSocket + HTTP Fallback
# ─────────────────────────────────────────────────────────────

async def btc_price_stream(session: aiohttp.ClientSession):
    """Stream BTC/USD via Binance WebSocket with staleness detection & auto-reconnect."""
    global btc_price, btc_price_updated, price_feed_status
    reconnect_delay = 1
    max_reconnect = 30

    while True:
        try:
            async with session.ws_connect(
                BINANCE_WS_URL,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as ws:
                reconnect_delay = 1
                price_feed_status = "ws_live"
                while True:
                    try:
                        msg = await asyncio.wait_for(
                            ws.__anext__(), timeout=PRICE_STALE_RECONNECT
                        )
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        price_feed_status = "stale"
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json_loads(msg.data)
                        btc_price = float(data["p"])
                        btc_price_updated = time.time()
                        price_feed_status = "ws_live"
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        price_feed_status = "stale"
                        break
        except Exception:
            price_feed_status = "stale"

        try:
            async with session.get(COINCAP_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    btc_price = float(data["data"]["priceUsd"])
                    btc_price_updated = time.time()
                    price_feed_status = "http_fallback"
        except Exception:
            pass

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect)


# ─────────────────────────────────────────────────────────────
# MARKET DISCOVERY
# ─────────────────────────────────────────────────────────────

async def discover_market(session: aiohttp.ClientSession) -> Optional[dict]:
    """Find current or next BTC Up/Down 5-min market by slug."""
    now_ts = time.time()

    for offset in [0, 300]:
        candle_end = math.ceil(now_ts / 300) * 300 + offset
        slug = f"btc-updown-5m-{candle_end}"

        try:
            url = f"{GAMMA_API}/events/slug/{slug}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    continue
                event = await resp.json()
        except Exception:
            continue

        markets = event.get("markets", [])
        if not markets:
            continue

        market = markets[0]
        start_str = market.get("eventStartTime")
        end_str = market.get("endDate")
        if not start_str or not end_str:
            continue

        event_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        event_end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        if event_end <= datetime.now(timezone.utc):
            continue

        outcomes_str = market.get("outcomes", "[]")
        tokens_str = market.get("clobTokenIds", "[]")

        outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
        clob_tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str

        if len(outcomes) < 2 or len(clob_tokens) < 2:
            continue

        up_idx, down_idx = 0, 1
        for i, outcome in enumerate(outcomes):
            if outcome.lower() == "up":
                up_idx = i
            elif outcome.lower() == "down":
                down_idx = i

        return {
            "slug": slug,
            "title": event.get("title", ""),
            "event_start": event_start,
            "event_end": event_end,
            "token_up_id": clob_tokens[up_idx],
            "token_down_id": clob_tokens[down_idx],
        }

    return None


# ─────────────────────────────────────────────────────────────
# ORDERBOOK
# ─────────────────────────────────────────────────────────────

async def fetch_orderbook(
    session: aiohttp.ClientSession, token_id: str
) -> dict:
    """Get orderbook: WS cache first, REST fallback."""
    ob = get_cached_orderbook(token_id)
    if ob["ask_price"] > 0:
        return ob
    return await fetch_orderbook_rest(session, token_id)


async def fetch_best_ask(
    session: aiohttp.ClientSession, token_id: str
) -> tuple[float, float]:
    """Convenience wrapper returning (ask_price, ask_size)."""
    ob = await fetch_orderbook(session, token_id)
    return ob["ask_price"], ob["ask_size"]


# ─────────────────────────────────────────────────────────────
# CSV LOGGING
# ─────────────────────────────────────────────────────────────

def init_csv(path: str):
    """Create CSV file with headers if it doesn't exist."""
    if not Path(path).exists():
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "candle_num", "timestamp", "slug", "title",
                "candle_start", "candle_end",
                "btc_open", "btc_close", "btc_delta",
                "actual_direction", "decision", "side_bought",
                "buy_price", "bet_amount", "won", "pnl", "pnl_pct",
                "cumulative_pnl", "balance", "win_rate",
            ])


def log_trade_csv(path: str, trade: Trade, stats: Stats):
    """Append one trade to CSV."""
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade.candle_num,
            datetime.now(timezone.utc).isoformat(),
            trade.slug,
            trade.title,
            trade.candle_start,
            trade.candle_end,
            f"{trade.open_price:.2f}",
            f"{trade.close_price:.2f}",
            f"{trade.delta:.2f}",
            trade.actual_direction,
            trade.decision,
            trade.side_bought,
            f"{trade.buy_price:.4f}",
            f"{trade.bet_amount:.2f}",
            trade.won,
            f"{trade.pnl:.4f}",
            f"{trade.pnl_pct:.2f}",
            f"{stats.total_pnl:.4f}",
            f"{stats.balance:.4f}",
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


def format_countdown(seconds: float) -> str:
    if seconds <= 0:
        return "EXPIRED"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m {s:02d}s" if m > 0 else f"{s}s"


def clear():
    print("\033[2J\033[H", end="", flush=True)


def print_trade_result(trade: Trade):
    """Print a detailed result for one candle."""
    c = GREEN if trade.won else RED
    icon = "✅" if trade.won else "❌"
    sign = "+" if trade.pnl >= 0 else ""

    if trade.decision.startswith("BUY"):
        print(f"  {icon} {c}Trade #{trade.candle_num}{R}")
        print(f"     BTC: ${trade.open_price:,.2f} → ${trade.close_price:,.2f} "
              f"({'+' if trade.delta >= 0 else ''}${trade.delta:,.2f})")
        print(f"     Direction: {trade.actual_direction} | Bought: {trade.side_bought} "
              f"@ ${trade.buy_price:.4f}")
        print(f"     Bet: ${trade.bet_amount:.2f}  |  "
              f"P&L: {c}{sign}${trade.pnl:.4f}{R} ({sign}{trade.pnl_pct:.1f}%)")
        bal_c = GREEN if trade.balance_after >= STARTING_BALANCE else RED
        print(f"     Balance: {bal_c}${trade.balance_after:.4f}{R}")
    else:
        reason = trade.decision.replace("SKIP_", "")
        print(f"  ⏭️  {YELLOW}Candle #{trade.candle_num} — SKIPPED ({reason}){R}")
        print(f"     BTC: ${trade.open_price:,.2f} → ${trade.close_price:,.2f}")


def print_stats(stats: Stats):
    """Print the running stats dashboard."""
    streak_c = GREEN if stats.current_streak > 0 else (RED if stats.current_streak < 0 else D)
    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_sign = "+" if stats.total_pnl >= 0 else ""
    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    bal_sign = "+" if stats.balance_change_pct >= 0 else ""

    print()
    print(f"  {B}{'─' * 58}{R}")
    print(f"  {B}📊 RUNNING STATS  │  💰 Balance: {bal_c}${stats.balance:.4f}{R} "
          f"({bal_c}{bal_sign}{stats.balance_change_pct:.1f}%{R})")
    print(f"  {B}{'─' * 58}{R}")
    print(f"  │ Candles:    {stats.total_candles:<6}  "
          f"│ Trades:    {stats.total_trades:<6}  "
          f"│ Skips: {stats.total_skips}")
    print(f"  │ Wins:       {GREEN}{stats.wins}{R}{'':>{5 - len(str(stats.wins))}}  "
          f"│ Losses:    {RED}{stats.losses}{R}{'':>{5 - len(str(stats.losses))}}  "
          f"│ Win Rate: {B}{stats.win_rate:.1f}%{R}")
    print(f"  │ Total P&L:  {pnl_c}{pnl_sign}${stats.total_pnl:.4f}{R}{'':>{1}}  "
          f"│ ROI:       {pnl_c}{pnl_sign}{stats.roi:.1f}%{R}{'':>{1}}  "
          f"│ Avg P&L: ${stats.avg_pnl:.4f}")
    print(f"  │ Wagered:    ${stats.total_wagered:.2f}{'':>{1}}  "
          f"│ Best:      {GREEN}+${stats.best_trade:.4f}{R}{'':>{1}}  "
          f"│ Worst: {RED}${stats.worst_trade:.4f}{R}")
    print(f"  │ Streak:     {streak_c}{stats.current_streak:+d}{R}{'':{4}}  "
          f"│ Peak:      {GREEN}${stats.peak_balance:.4f}{R}{'':1}  "
          f"│ Valley: {RED}${stats.valley_balance:.4f}{R}")
    shadow_total = stats.shadow_wins + stats.shadow_losses
    if shadow_total > 0:
        shadow_wr = stats.shadow_wins / shadow_total * 100
        print(f"  │ {D}Shadow:     {stats.shadow_wins}W/{stats.shadow_losses}L ({shadow_wr:.0f}% of skips would have won){R}")
    print(f"  {B}{'─' * 58}{R}")


def print_waiting(market: dict, remaining: float, candle_open: float, stats: Stats,
                  trade_window: int = DEFAULT_TRADE_WINDOW):
    """Print the live waiting screen while a candle is running."""
    now = datetime.now(timezone.utc)
    event_start = market["event_start"]
    to_start = (event_start - now).total_seconds()

    delta = btc_price - candle_open if candle_open > 0 else 0
    delta_sign = "+" if delta >= 0 else ""
    dir_c = GREEN if delta >= 0 else RED
    direction = "UP 📈" if delta >= 0 else "DOWN 📉"

    abs_delta = abs(delta)
    if abs_delta < SAFE_BUFFER_USD:
        verdict = f"{YELLOW}RISKY (delta ${abs_delta:,.0f} < ${SAFE_BUFFER_USD:.0f} buffer){R}"
    elif remaining > trade_window:
        verdict = f"{CYAN}MONITORING — execution in {int(remaining - trade_window)}s{R}"
    else:
        verdict = f"{RED}{B}🔥 EXECUTION WINDOW{R}"

    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    bet_size = min(MAX_TRADE_USD, stats.balance)

    clear()
    print(f"{B}{'═' * 64}{R}")
    print(f"{B}  📝 POLYSNIPER — PAPER TRADING{R}")
    print(f"{B}{'═' * 64}{R}")
    print(f"  {D}{market['title']}{R}")
    print(f"  💰 Balance: {bal_c}{B}${stats.balance:.4f}{R}  "
          f"│  Next bet: ${bet_size:.2f}")
    print()

    if to_start > 0:
        print(f"  ⏳ Candle starts in {B}{format_countdown(to_start)}{R}")
    else:
        bar_len = 30
        filled = max(0, int((remaining / 300) * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        cd = format_countdown(remaining)
        cd_c = f"{RED}{B}" if remaining <= trade_window else B
        print(f"  ⏱️  Remaining: {cd_c}{cd}{R}  [{bar}]")

    print()
    # Show staleness warning if price hasn't updated recently
    stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else 0
    stale_warn = f"  {RED}⚠️  Price data is {int(stale_age)}s stale!{R}" if stale_age > PRICE_STALE_SECONDS else ""
    if price_feed_status == "ws_live":
        feed_icon = f"{GREEN}🟢 WS Live{R}"
    elif price_feed_status == "http_fallback":
        feed_icon = f"{YELLOW}🟡 HTTP Fallback{R}"
    else:
        feed_icon = f"{RED}🔴 {price_feed_status.upper()}{R}"
    print(f"  ₿ BTC:  {B}${btc_price:>12,.2f}{R}  {feed_icon}{stale_warn}")
    if candle_open > 0:
        print(f"  📌 Open: ${candle_open:>12,.2f}    "
              f"Delta: {dir_c}{delta_sign}${delta:>10,.2f}{R}    "
              f"{dir_c}{direction}{R}")
    print(f"  🎯 {verdict}")

    if stats.total_candles > 0:
        pnl_c = GREEN if stats.total_pnl >= 0 else RED
        pnl_s = "+" if stats.total_pnl >= 0 else ""
        print()
        print(f"  {D}─── Session: {stats.total_trades} trades | "
              f"{stats.win_rate:.0f}% win rate | "
              f"P&L: {pnl_s}${stats.total_pnl:.4f} ───{R}")

    # ── Last Trade Recap ──
    if stats.trades:
        lt = stats.trades[-1]
        print()
        print(f"  {B}{'─' * 64}{R}")
        print(f"  {B}📋 LAST TRADE (Candle #{lt.candle_num}){R}")
        print(f"  {B}{'─' * 64}{R}")

        if lt.decision.startswith("BUY"):
            result_c = GREEN if lt.won else RED
            result_icon = "✅ WON" if lt.won else "❌ LOST"
            pnl_sign = "+" if lt.pnl >= 0 else ""

            print(f"  │ Result:     {result_c}{B}{result_icon}{R}")
            print(f"  │ Side:       Bought {B}{lt.side_bought}{R} @ "
                  f"${lt.buy_price:.4f}  →  Actual: {lt.actual_direction}")
            print(f"  │ Bet:        ${lt.bet_amount:.2f}  │  "
                  f"P&L: {result_c}{pnl_sign}${lt.pnl:.4f}{R} "
                  f"({result_c}{pnl_sign}{lt.pnl_pct:.1f}%{R})")
            print(f"  │ BTC Move:   ${lt.open_price:,.2f} → ${lt.close_price:,.2f} "
                  f"({'+' if lt.delta >= 0 else ''}${lt.delta:,.2f})")
            if lt.btc_volatility > 0:
                print(f"  │ Volatility: ${lt.btc_volatility:,.2f} range "
                      f"(H: ${lt.btc_high:,.2f}  L: ${lt.btc_low:,.2f})")
            if lt.spread > 0:
                print(f"  │ Spread:     ${lt.spread:.4f}  │  "
                      f"Book depth: ${lt.book_depth_usd:.2f}")
            if lt.opposing_ask_price > 0:
                print(f"  │ Other side: ${lt.opposing_ask_price:.4f} ask")
            if lt.confidence:
                conf_c = GREEN if lt.confidence == "HIGH" else (YELLOW if lt.confidence == "MEDIUM" else RED)
                print(f"  │ Confidence: {conf_c}{lt.confidence}{R}  │  "
                      f"Decided at T-{lt.decision_remaining:.0f}s "
                      f"(BTC ${lt.decision_btc_price:,.2f}, "
                      f"delta {'+' if lt.decision_delta >= 0 else ''}${lt.decision_delta:,.2f})")
        else:
            reason = lt.decision.replace("SKIP_", "")
            print(f"  │ Decision:   {YELLOW}SKIPPED — {reason}{R}")
            print(f"  │ BTC Move:   ${lt.open_price:,.2f} → ${lt.close_price:,.2f} "
                  f"({'+' if lt.delta >= 0 else ''}${lt.delta:,.2f})")
            if lt.btc_volatility > 0:
                print(f"  │ Volatility: ${lt.btc_volatility:,.2f} range")
            if lt.actual_direction and lt.buy_price > 0:
                hypo_pnl = (MAX_TRADE_USD / lt.buy_price) - MAX_TRADE_USD
                hypo_c = GREEN if hypo_pnl > 0 else RED
                print(f"  │ If traded:  {hypo_c}Would have {'won' if hypo_pnl > 0 else 'lost'} "
                      f"~${abs(hypo_pnl):.4f}{R}")

        print(f"  {B}{'─' * 64}{R}")

    print()
    print(f"  {D}Ctrl+C to stop and see final report{R}")
    print(f"{B}{'═' * 64}{R}")


# ─────────────────────────────────────────────────────────────
# BUILD SCREEN (for decoupled UI renderer)
# ─────────────────────────────────────────────────────────────

def build_screen(state: dict) -> str:
    """Build the entire dashboard into a single string for the UI renderer."""
    import io
    buf = io.StringIO()
    w = buf.write

    market = state.get("market")
    remaining = state.get("remaining", 0)
    candle_open = state.get("candle_open", 0)
    stats = state.get("stats")
    trade_window = state.get("trade_window", DEFAULT_TRADE_WINDOW)

    if not market or not stats:
        return ""

    now = datetime.now(timezone.utc)
    event_start = market["event_start"]
    to_start = (event_start - now).total_seconds()

    delta = btc_price - candle_open if candle_open > 0 else 0
    delta_sign = "+" if delta >= 0 else ""
    dir_c = GREEN if delta >= 0 else RED
    direction = "UP 📈" if delta >= 0 else "DOWN 📉"
    abs_delta = abs(delta)

    if abs_delta < SAFE_BUFFER_USD:
        verdict = f"{YELLOW}RISKY (delta ${abs_delta:,.0f} < ${SAFE_BUFFER_USD:.0f} buffer){R}"
    elif remaining > trade_window:
        verdict = f"{CYAN}MONITORING — execution in {int(remaining - trade_window)}s{R}"
    else:
        verdict = f"{RED}{B}🔥 EXECUTION WINDOW{R}"

    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    bet_size = min(MAX_TRADE_USD, stats.balance)

    w(f"{B}{'═' * 64}{R}\n")
    w(f"{B}  📝 POLYSNIPER — $100 PAPER TRADING{R}\n")
    w(f"{B}{'═' * 64}{R}\n")
    w(f"  {D}{market['title']}{R}\n")
    w(f"  💰 Balance: {bal_c}{B}${stats.balance:.4f}{R}  "
      f"│  Next bet: ${bet_size:.2f}\n")
    w("\n")

    if to_start > 0:
        w(f"  ⏳ Candle starts in {B}{format_countdown(to_start)}{R}\n")
    else:
        bar_len = 30
        filled = max(0, int((remaining / 300) * bar_len))
        bar = "█" * filled + "░" * (bar_len - filled)
        cd = format_countdown(remaining)
        cd_c = f"{RED}{B}" if remaining <= trade_window else B
        w(f"  ⏱️  Remaining: {cd_c}{cd}{R}  [{bar}]\n")

    w("\n")
    stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else 0
    stale_warn = f"  {RED}⚠️  Price data is {int(stale_age)}s stale!{R}" if stale_age > PRICE_STALE_SECONDS else ""
    if price_feed_status == "ws_live":
        feed_icon = f"{GREEN}🟢 WS Live{R}"
    elif price_feed_status == "http_fallback":
        feed_icon = f"{YELLOW}🟡 HTTP Fallback{R}"
    else:
        feed_icon = f"{RED}🔴 {price_feed_status.upper()}{R}"
    w(f"  ₿ BTC:  {B}${btc_price:>12,.2f}{R}  {feed_icon}{stale_warn}\n")
    if candle_open > 0:
        w(f"  📌 Open: ${candle_open:>12,.2f}    "
          f"Delta: {dir_c}{delta_sign}${delta:>10,.2f}{R}    "
          f"{dir_c}{direction}{R}\n")
    w(f"  🎯 {verdict}\n")

    if stats.total_candles > 0:
        pnl_c = GREEN if stats.total_pnl >= 0 else RED
        pnl_s = "+" if stats.total_pnl >= 0 else ""
        w("\n")
        w(f"  {D}─── Session: {stats.total_trades} trades | "
          f"{stats.win_rate:.0f}% win rate | "
          f"P&L: {pnl_s}${stats.total_pnl:.4f} ───{R}\n")

    if stats.trades:
        lt = stats.trades[-1]
        w("\n")
        w(f"  {B}{'─' * 64}{R}\n")
        w(f"  {B}📋 LAST TRADE (Candle #{lt.candle_num}){R}\n")
        w(f"  {B}{'─' * 64}{R}\n")
        if lt.decision.startswith("BUY"):
            result_c = GREEN if lt.won else RED
            result_icon = "✅ WON" if lt.won else "❌ LOST"
            pnl_sign = "+" if lt.pnl >= 0 else ""
            w(f"  │ Result:     {result_c}{B}{result_icon}{R}\n")
            w(f"  │ Bet:        ${lt.bet_amount:.2f}  │  "
              f"P&L: {result_c}{pnl_sign}${lt.pnl:.4f}{R}\n")
        else:
            reason = lt.decision.replace("SKIP_", "")
            w(f"  │ Decision:   {YELLOW}SKIPPED — {reason}{R}\n")
        w(f"  {B}{'─' * 64}{R}\n")

    w("\n")
    w(f"  {D}Ctrl+C to stop and see final report{R}\n")
    w(f"{B}{'═' * 64}{R}\n")

    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# CANDLE SIMULATION
# ─────────────────────────────────────────────────────────────

async def simulate_candle(
    session: aiohttp.ClientSession,
    market: dict,
    candle_num: int,
    stats: Stats,
    trade_window: int = DEFAULT_TRADE_WINDOW,
) -> Trade:
    """
    Run through one full 5-minute candle, mirroring sniper.py logic.

    - Records candle open price
    - Monitors BTC price continuously
    - At T-15s, checks orderbook and makes buy/skip decision
    - Waits until candle closes to determine actual winner
    - Calculates P&L

    Returns a Trade with full details.
    """
    event_start = market["event_start"]
    event_end = market["event_end"]
    token_up = market["token_up_id"]
    token_down = market["token_down_id"]

    candle_open = 0.0
    decision = ""
    side_bought = ""
    buy_price = 0.0
    order_simulated = False

    # Rich context tracking
    ask_size = 0.0
    book_depth_usd = 0.0
    opposing_ask_price = 0.0
    spread = 0.0
    btc_high = 0.0
    btc_low = float('inf')
    decision_btc_price = 0.0
    decision_delta = 0.0
    decision_remaining = 0.0
    confidence = ""

    # ── Launch WS orderbook streams for both tokens ──
    ws_stop = asyncio.Event()
    ws_up_task = asyncio.create_task(orderbook_ws_stream(token_up, ws_stop))
    ws_dn_task = asyncio.create_task(orderbook_ws_stream(token_down, ws_stop))

    # ── Wait for candle to start ──
    while True:
        now = datetime.now(timezone.utc)
        to_start = (event_start - now).total_seconds()
        if to_start <= 0:
            break
        update_ui(market=market, remaining=300, candle_open=0,
                  stats=stats, trade_window=trade_window)
        await asyncio.sleep(1)

    # Record open price
    candle_open = btc_price
    btc_high = btc_price
    btc_low = btc_price

    # ── Main candle loop ──
    while True:
        now = datetime.now(timezone.utc)
        remaining = (event_end - now).total_seconds()

        if remaining <= 0:
            break

        # Track high/low
        if btc_price > 0:
            btc_high = max(btc_high, btc_price)
            btc_low = min(btc_low, btc_price)

        # Show live status
        # Update UI state (rendered by background task)
        update_ui(market=market, remaining=remaining, candle_open=candle_open,
                  stats=stats, trade_window=trade_window)

        # ── Sniper decision within trade window ──
        if remaining <= trade_window and not order_simulated:
            decision_btc_price = btc_price
            decision_remaining = remaining

            # Check price staleness before any decision
            stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else float('inf')
            if stale_age > PRICE_STALE_RECONNECT:
                decision = "SKIP_STALE_PRICE"
                order_simulated = True
            else:
                delta = btc_price - candle_open
                abs_delta = abs(delta)
                decision_delta = delta

                if abs_delta < SAFE_BUFFER_USD:
                    decision = "SKIP_RISKY"
                    order_simulated = True
                else: 
                    if delta > 0:
                        winning_side = "UP"
                        winning_token = token_up
                        losing_token = token_down
                    else:
                        winning_side = "DOWN"
                        winning_token = token_down
                        losing_token = token_up

                    # Fetch both orderbooks in parallel
                    ob, opp_ob = await asyncio.gather(
                        fetch_orderbook(session, winning_token),
                        fetch_orderbook(session, losing_token),
                    )
                    ask_price = ob["ask_price"]
                    ask_size = ob["ask_size"]
                    book_depth_usd = ob["depth_usd"]
                    spread = ob["spread"]
                    opposing_ask_price = opp_ob["ask_price"]

                    # Calculate confidence based on multiple signals
                    confidence_score = 0
                    if abs_delta >= SAFE_BUFFER_USD * 2:
                        confidence_score += 2
                    elif abs_delta >= SAFE_BUFFER_USD:
                        confidence_score += 1
                    if ask_price > 0 and ask_price <= 0.94:
                        confidence_score += 2
                    elif ask_price <= 0.96:
                        confidence_score += 1
                    if book_depth_usd >= 100:
                        confidence_score += 1
                    if opposing_ask_price > 0 and opposing_ask_price >= 0.15:
                        confidence_score += 1
                    if confidence_score >= 4:
                        confidence = "HIGH"
                    elif confidence_score >= 2:
                        confidence = "MEDIUM"
                    else:
                        confidence = "LOW"

                    if ask_price == 0:
                        decision = "SKIP_NO_ASKS"
                        order_simulated = True
                    elif ask_price > MAX_BUY_PRICE:
                        decision = "SKIP_EXPENSIVE"
                        buy_price = ask_price
                        order_simulated = True
                    elif ask_price < MIN_BUY_PRICE:
                        decision = "SKIP_CHEAP"
                        buy_price = ask_price
                        order_simulated = True
                    elif ask_size * ask_price < MIN_ASK_SIZE_USD:
                        decision = "SKIP_THIN_BOOK"
                        buy_price = ask_price
                        order_simulated = True
                    elif spread > MAX_SPREAD:
                        decision = "SKIP_WIDE_SPREAD"
                        buy_price = ask_price
                        order_simulated = True
                    else:
                        decision = f"BUY_{winning_side}"
                        side_bought = winning_side
                        buy_price = ask_price
                        order_simulated = True

        await adaptive_sleep(remaining, trade_window)

    # ── Candle closed — cleanup WS streams ──
    ws_stop.set()
    ws_up_task.cancel()
    ws_dn_task.cancel()

    # ── Determine actual result ──
    close_price = btc_price
    delta = close_price - candle_open
    actual_direction = "UP" if delta >= 0 else "DOWN"

    # If we didn't enter the execution window (very short candle or joined late)
    if not order_simulated:
        decision = "SKIP_RISKY"

    # ── Calculate P&L ──
    won = False
    pnl = 0.0
    pnl_pct = 0.0
    bet_amount = 0.0

    if decision.startswith("BUY"):
        sizing_mult = CONFIDENCE_SIZING.get(confidence, 1.0)
        bet_amount = min(MAX_TRADE_USD * sizing_mult, stats.balance)
        shares = bet_amount / buy_price if buy_price > 0 else 0

        if side_bought == actual_direction:
            payout = shares * 1.0
            pnl = payout - bet_amount
            pnl_pct = (pnl / bet_amount) * 100
            won = True
        else:
            pnl = -bet_amount
            pnl_pct = -100.0
            won = False

    btc_volatility = btc_high - btc_low if btc_low < float('inf') else 0.0
    balance_after = stats.balance + pnl

    trade = Trade(
        candle_num=candle_num,
        slug=market["slug"],
        title=market["title"],
        candle_start=event_start.strftime("%H:%M:%S UTC"),
        candle_end=event_end.strftime("%H:%M:%S UTC"),
        open_price=candle_open,
        close_price=close_price,
        delta=delta,
        actual_direction=actual_direction,
        decision=decision,
        side_bought=side_bought,
        buy_price=buy_price,
        bet_amount=bet_amount,
        won=won,
        pnl=pnl,
        pnl_pct=pnl_pct,
        balance_after=balance_after,
        # Rich context
        ask_size=ask_size,
        book_depth_usd=book_depth_usd,
        opposing_ask_price=opposing_ask_price,
        spread=spread,
        btc_high=btc_high,
        btc_low=btc_low if btc_low < float('inf') else 0.0,
        btc_volatility=btc_volatility,
        decision_btc_price=decision_btc_price,
        decision_delta=decision_delta,
        decision_remaining=decision_remaining,
        confidence=confidence,
    )

    return trade


# ─────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────

def print_final_report(stats: Stats):
    """Print comprehensive final report after all candles."""
    clear()
    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_s = "+" if stats.total_pnl >= 0 else ""
    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    bal_s = "+" if stats.balance_change_pct >= 0 else ""

    print()
    print(f"{B}{'═' * 64}{R}")
    print(f"{B}  📋 PAPER TRADING — FINAL REPORT{R}")
    print(f"{B}{'═' * 64}{R}")
    print()
    print(f"  {B}Bankroll{R}")
    print(f"  ├─ Starting balance:   ${STARTING_BALANCE:.2f}")
    print(f"  ├─ Final balance:      {bal_c}{B}${stats.balance:.4f}{R} "
          f"({bal_c}{bal_s}{stats.balance_change_pct:.1f}%{R})")
    print(f"  ├─ Peak balance:       {GREEN}${stats.peak_balance:.4f}{R}")
    print(f"  └─ Valley balance:     {RED}${stats.valley_balance:.4f}{R}")
    print()
    print(f"  {B}Overview{R}")
    print(f"  ├─ Candles observed:   {stats.total_candles}")
    print(f"  ├─ Trades simulated:   {stats.total_trades}")
    print(f"  ├─ Candles skipped:    {stats.total_skips}")
    print(f"  └─ Trade frequency:    {(stats.total_trades / stats.total_candles * 100):.0f}%"
          if stats.total_candles > 0 else "")
    print()

    if stats.total_trades > 0:
        print(f"  {B}Performance{R}")
        print(f"  ├─ Win rate:           {B}{stats.win_rate:.1f}%{R}  "
              f"({stats.wins}W / {stats.losses}L)")
        print(f"  ├─ Total P&L:          {pnl_c}{B}{pnl_s}${stats.total_pnl:.4f}{R}")
        print(f"  ├─ Total wagered:      ${stats.total_wagered:.2f}")
        print(f"  ├─ ROI:                {pnl_c}{pnl_s}{stats.roi:.1f}%{R}")
        print(f"  ├─ Average P&L:        ${stats.avg_pnl:.4f} / trade")
        print(f"  ├─ Best trade:         {GREEN}+${stats.best_trade:.4f}{R}")
        print(f"  └─ Worst trade:        {RED}${stats.worst_trade:.4f}{R}")
        print()
        print(f"  {B}Streaks{R}")
        print(f"  ├─ Current streak:     {stats.current_streak:+d}")
        print(f"  ├─ Best win streak:    {stats.longest_win_streak}")
        print(f"  └─ Worst loss streak:  {stats.longest_loss_streak}")
    else:
        print(f"  {YELLOW}No trades were executed (all candles skipped).{R}")

    # Shadow stats
    shadow_total = stats.shadow_wins + stats.shadow_losses
    if shadow_total > 0:
        shadow_wr = stats.shadow_wins / shadow_total * 100
        print()
        print(f"  {B}Shadow Analysis (skipped candles){R}")
        print(f"  ├─ Would-have-won:     {GREEN}{stats.shadow_wins}{R}")
        print(f"  ├─ Would-have-lost:    {RED}{stats.shadow_losses}{R}")
        print(f"  └─ Shadow win rate:    {B}{shadow_wr:.1f}%{R}")

    print()

    # Trade log summary
    if stats.trades:
        print(f"  {B}Trade Log{R}")
        print(f"  {'#':>3}  {'Direction':>9}  {'Decision':>15}  {'Buy @':>8}  "
              f"{'P&L':>10}  {'Balance':>10}  {'Result':>6}")
        print(f"  {'─' * 70}")
        for t in stats.trades:
            if t.decision.startswith("BUY"):
                result_icon = f"{GREEN}WIN{R}" if t.won else f"{RED}LOSS{R}"
                sign = "+" if t.pnl >= 0 else ""
                c = GREEN if t.won else RED
                bc = GREEN if t.balance_after >= STARTING_BALANCE else RED
                print(f"  {t.candle_num:>3}  {t.actual_direction:>9}  "
                      f"{t.decision:>15}  ${t.buy_price:>6.4f}  "
                      f"{c}{sign}${t.pnl:>8.4f}{R}  "
                      f"{bc}${t.balance_after:>8.4f}{R}  {result_icon}")
            else:
                print(f"  {t.candle_num:>3}  {t.actual_direction:>9}  "
                      f"{YELLOW}{t.decision:>15}{R}  {'—':>8}  "
                      f"{'—':>10}  {'—':>10}  {D}SKIP{R}")

    print()
    print(f"  {D}Full log saved to: {CSV_FILE}{R}")
    print(f"{B}{'═' * 64}{R}")
    print()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main(max_candles: int | None = None, trade_window: int = DEFAULT_TRADE_WINDOW):
    """Run paper trader across multiple candles."""
    session = create_session()
    shutdown = asyncio.Event()
    price_task = asyncio.create_task(btc_price_stream(session))
    ui_task = asyncio.create_task(ui_renderer(build_screen, shutdown))

    print("⏳ Fetching live BTC price...")
    elapsed = 0
    while btc_price == 0.0:
        await asyncio.sleep(0.5)
        elapsed += 1
        if elapsed % 6 == 0:
            print("   Still waiting for price data...")
    print(f"✅ BTC Price: ${btc_price:,.2f}")
    print(f"💰 Starting balance: ${STARTING_BALANCE:.2f}")
    print(f"🎯 Execution window: last {trade_window}s of each candle\n")

    init_csv(CSV_FILE)
    stats = Stats()
    candle_num = 0

    try:
        while True:
            if max_candles and candle_num >= max_candles:
                break

            # ── Drawdown circuit breaker ──
            if stats.cooldown_remaining > 0:
                stats.cooldown_remaining -= 1
                print(f"\n  {YELLOW}⏸️  Drawdown cooldown: sitting out "
                      f"({stats.cooldown_remaining + 1} candles remaining){R}")
                market = await discover_market(session)
                if market:
                    candle_num += 1
                await asyncio.sleep(5)
                continue

            # ── Consecutive loss cooldown ──
            if stats.current_streak <= -LOSS_STREAK_LIMIT:
                print(f"\n  {YELLOW}🧲 Loss streak cooldown: {abs(stats.current_streak)} "
                      f"consecutive losses — skipping 1 candle{R}")
                stats.current_streak = -(LOSS_STREAK_LIMIT - 1)
                market = await discover_market(session)
                if market:
                    candle_num += 1
                await asyncio.sleep(5)
                continue

            market = await discover_market(session)
            if market is None:
                print("⚠️  No market found. Retrying in 15s...")
                await asyncio.sleep(15)
                continue

            candle_num += 1

            trade = await simulate_candle(session, market, candle_num, stats, trade_window)

            stats.record(trade)
            log_trade_csv(CSV_FILE, trade, stats)

            # ── Check drawdown after recording ──
            if (stats.peak_balance > 0 and
                    stats.balance < stats.peak_balance * DRAWDOWN_THRESHOLD):
                stats.cooldown_remaining = DRAWDOWN_COOLDOWN
                print(f"\n  {RED}🛑 DRAWDOWN BREAKER: balance ${stats.balance:.4f} < "
                      f"50% of peak ${stats.peak_balance:.4f} — pausing {DRAWDOWN_COOLDOWN} candles{R}")

            # Show result
            clear()
            print(f"\n{B}{'═' * 64}{R}")
            print(f"{B}  📝 CANDLE #{candle_num} RESULT{R}")
            print(f"{B}{'═' * 64}{R}")
            print()
            print_trade_result(trade)
            print_stats(stats)
            print()

            await asyncio.sleep(3)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        shutdown.set()
        price_task.cancel()
        ui_task.cancel()
        await session.close()
        print_final_report(stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PolySniper Paper Trader")
    parser.add_argument(
        "--candles", "-n",
        type=int,
        default=None,
        help="Number of candles to simulate (default: run until Ctrl+C)",
    )
    parser.add_argument(
        "--window", "-w",
        type=int,
        default=DEFAULT_TRADE_WINDOW,
        help=f"Execution window in seconds before candle close (default: {DEFAULT_TRADE_WINDOW})",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(max_candles=args.candles, trade_window=args.window))
    except KeyboardInterrupt:
        # Final report is printed inside main()'s finally block
        print(f"\n👋 Goodbye!")
