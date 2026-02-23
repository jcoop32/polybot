#!/usr/bin/env python3
"""
Paper Trade Maker — One-Sided Quoting Strategy

Simulates a MAKER strategy on BTC 5-min candle markets:
  - Predicts direction using BTC delta at T+15s (like the sniper)
  - Places a simulated LIMIT BUY on the predicted winning side
  - Bid price = best_bid + 0.01 (front of queue)
  - Filled when the ask drops to or below our bid level
  - Earns the spread: payout ($1.00) minus buy price
  - Zero taker fees, plus estimated 20% maker rebate

Unlike the taker bots, this bot does NOT cross the spread.
It waits passively for someone to sell into our bid.

Usage:
    python paper_trade_maker_one.py                # Run until Ctrl+C
    python paper_trade_maker_one.py --candles 20   # Run for 20 candles
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
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
    orderbook_ws_stream, fetch_orderbook_rest, get_cached_orderbook,
    update_ui, ui_renderer, adaptive_sleep,
    calc_taker_fee,
)


# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

STARTING_BALANCE = 10.00
MAX_BUY_PRICE = 0.55          # Don't bid higher than this (near 50/50)
MIN_BUY_PRICE = 0.10          # Don't bid lower than this
SAFE_BUFFER_USD = 30.0        # BTC must move this far from open to predict
MAX_TRADE_USD = 2.0           # Max simulated order size
BET_FRACTION = 0.15           # Bet 15% of balance max
MIN_ASK_SIZE_USD = 5.0        # Minimum depth required
MAX_SPREAD = 0.05             # Max spread to consider market healthy
TICK_SIZE = 0.01              # Minimum price increment

# Circuit breaker
CIRCUIT_BREAKER_LOSSES = 3    # Consecutive losses to trigger cooldown
CIRCUIT_BREAKER_COOLDOWN = 1800  # 30 minute cooldown (seconds)

# Maker-specific
BID_OFFSET = 0.01             # Bid at best_bid + this (front of queue)
MAKER_REBATE_PCT = 0.20       # 20% of taker fees returned to makers

# Decision timing
DECISION_SECOND = 15          # Seconds into candle to make prediction
CANDLE_SECONDS = 300

# BTC price staleness
PRICE_STALE_SECONDS = 10
PRICE_STALE_RECONNECT = 30

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
COINCAP_URL = "https://api.coincap.io/v2/assets/bitcoin"

CSV_FILE = "csv_logs/paper_trades_maker_one_v2.csv"


# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

btc_price: float = 0.0
btc_price_updated: float = 0.0
price_feed_status: str = "connecting"
bot_start_time: float = time.time()

# Circuit breaker state
circuit_breaker_active: bool = False
circuit_breaker_until: float = 0.0


# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Record of one simulated maker trade."""
    candle_num: int
    slug: str
    title: str
    timestamp: str
    open_price: float           # BTC open
    close_price: float          # BTC close
    delta: float
    actual_direction: str       # "UP" or "DOWN"
    predicted_direction: str    # "UP", "DOWN", or ""
    decision: str               # "QUOTE_UP", "QUOTE_DOWN", "SKIP_*"
    bid_price: float            # Price we bid at
    bid_size_usd: float         # USD value of our bid
    filled: bool                # Did our bid get hit?
    won: bool                   # Did the candle go our way?
    pnl: float
    pnl_pct: float
    maker_rebate: float         # Estimated rebate earned
    balance_after: float
    # Context
    best_bid: float = 0.0       # Market best bid when we quoted
    best_ask: float = 0.0       # Market best ask when we quoted
    spread: float = 0.0         # Market spread
    fill_time_elapsed: float = 0.0  # Seconds after decision until fill
    decision_btc_price: float = 0.0
    decision_delta: float = 0.0


@dataclass
class Stats:
    """Running statistics."""
    balance: float = STARTING_BALANCE
    trades: list[Trade] = field(default_factory=list)
    total_candles: int = 0
    total_quotes: int = 0       # Times we placed a bid
    total_fills: int = 0        # Times our bid was hit
    total_skips: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_rebates: float = 0.0
    total_wagered: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    peak_balance: float = STARTING_BALANCE
    valley_balance: float = STARTING_BALANCE
    current_streak: int = 0
    fill_rate: float = 0.0      # fills / quotes

    @property
    def win_rate(self) -> float:
        return (self.wins / self.total_fills * 100) if self.total_fills > 0 else 0

    @property
    def roi(self) -> float:
        return (self.total_pnl / self.total_wagered * 100) if self.total_wagered > 0 else 0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_fills if self.total_fills > 0 else 0

    @property
    def balance_change_pct(self) -> float:
        return ((self.balance - STARTING_BALANCE) / STARTING_BALANCE * 100)

    def record(self, trade: Trade):
        self.trades.append(trade)
        self.total_candles += 1

        if trade.decision.startswith("QUOTE"):
            self.total_quotes += 1

            if trade.filled:
                self.total_fills += 1
                self.total_wagered += trade.bid_size_usd
                self.total_pnl += trade.pnl
                self.total_rebates += trade.maker_rebate
                self.balance += trade.pnl + trade.maker_rebate

                self.peak_balance = max(self.peak_balance, self.balance)
                self.valley_balance = min(self.valley_balance, self.balance)

                if trade.won:
                    self.wins += 1
                    self.current_streak = max(0, self.current_streak) + 1
                else:
                    self.losses += 1
                    self.current_streak = min(0, self.current_streak) - 1

                self.best_trade = max(self.best_trade, trade.pnl)
                self.worst_trade = min(self.worst_trade, trade.pnl)
            # Track fill rate
            self.fill_rate = (self.total_fills / self.total_quotes * 100) if self.total_quotes > 0 else 0
        else:
            self.total_skips += 1


# ─────────────────────────────────────────────────────────────
# BTC PRICE STREAM
# ─────────────────────────────────────────────────────────────

async def btc_price_stream(session: aiohttp.ClientSession):
    """Stream BTC/USD via Binance WebSocket with fallback."""
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
    """Find current or next BTC Up/Down 5-min market."""
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

async def fetch_orderbook(session: aiohttp.ClientSession, token_id: str) -> dict:
    """Get orderbook: WS cache first, REST fallback."""
    ob = get_cached_orderbook(token_id)
    if ob["ask_price"] > 0:
        return ob
    return await fetch_orderbook_rest(session, token_id)


# ─────────────────────────────────────────────────────────────
# CSV LOGGING
# ─────────────────────────────────────────────────────────────

def init_csv(path: str):
    if not Path(path).exists():
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "candle_num", "timestamp", "slug", "title",
                "btc_open", "btc_close", "btc_delta",
                "actual_direction", "predicted_direction", "decision",
                "bid_price", "bid_size_usd", "filled", "won",
                "pnl", "pnl_pct", "maker_rebate",
                "cumulative_pnl", "balance", "fill_rate", "win_rate",
                "best_bid", "best_ask", "spread",
                "decision_btc_price", "decision_delta",
            ])


def log_trade_csv(path: str, trade: Trade, stats: Stats):
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade.candle_num,
            datetime.now(timezone.utc).isoformat(),
            trade.slug,
            trade.title,
            f"{trade.open_price:.2f}",
            f"{trade.close_price:.2f}",
            f"{trade.delta:.2f}",
            trade.actual_direction,
            trade.predicted_direction,
            trade.decision,
            f"{trade.bid_price:.4f}",
            f"{trade.bid_size_usd:.2f}",
            trade.filled,
            trade.won,
            f"{trade.pnl:.4f}",
            f"{trade.pnl_pct:.2f}",
            f"{trade.maker_rebate:.4f}",
            f"{stats.total_pnl:.4f}",
            f"{stats.balance:.4f}",
            f"{stats.fill_rate:.1f}",
            f"{stats.win_rate:.1f}",
            f"{trade.best_bid:.4f}",
            f"{trade.best_ask:.4f}",
            f"{trade.spread:.4f}",
            f"{trade.decision_btc_price:.2f}",
            f"{trade.decision_delta:.2f}",
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


def format_uptime(start: float) -> str:
    elapsed = int(time.time() - start)
    h = elapsed // 3600
    m = (elapsed % 3600) // 60
    s = elapsed % 60
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def build_screen(state: dict) -> str:
    """Build the dashboard for the UI renderer."""
    buf = io.StringIO()
    w = buf.write

    market = state.get("market")
    remaining = state.get("remaining", 0)
    candle_open = state.get("candle_open", 0)
    stats = state.get("stats")
    phase = state.get("phase", "WAITING")
    quote_info = state.get("quote_info", {})

    if not market or not stats:
        return ""

    now = datetime.now(timezone.utc)
    event_start = market["event_start"]
    to_start = (event_start - now).total_seconds()
    elapsed = CANDLE_SECONDS - remaining if remaining > 0 else CANDLE_SECONDS

    delta = btc_price - candle_open if candle_open > 0 else 0
    delta_sign = "+" if delta >= 0 else ""
    dir_c = GREEN if delta >= 0 else RED
    direction = "▲ UP" if delta >= 0 else "▼ DN"

    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    bet_size = min(MAX_TRADE_USD, stats.balance)
    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_s = "+" if stats.total_pnl >= 0 else ""

    # ── Header ──
    w(f"  {B}┌{'─' * 62}┐{R}\n")
    w(f"  {B}│{R}  🏦 {B}MAKER — ONE-SIDED{R}"
      f"                  {D}Paper Trading{R}   {B}│{R}\n")
    w(f"  {B}│{R}  {D}{market['title']:<60}{R}{B}│{R}\n")
    w(f"  {B}├{'─' * 62}┤{R}\n")
    uptime_str = format_uptime(bot_start_time)
    w(f"  {B}│{R}  💰 {bal_c}{B}${stats.balance:.4f}{R}"
      f"     Bid: ${bet_size:.2f}"
      f"     🕐 {uptime_str}"
      f"{' ' * max(1, 22 - len(uptime_str))}{B}│{R}\n")
    w(f"  {B}└{'─' * 62}┘{R}\n")

    # ── BTC Price ──
    stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else 0
    if price_feed_status == "ws_live":
        feed = f"{GREEN}●{R}"
    elif price_feed_status == "http_fallback":
        feed = f"{YELLOW}●{R}"
    else:
        feed = f"{RED}●{R}"
    stale_tag = f"  {RED}⚠ {int(stale_age)}s stale{R}" if stale_age > PRICE_STALE_SECONDS else ""
    w(f"\n  {feed} BTC  {B}${btc_price:>12,.2f}{R}{stale_tag}\n")
    if candle_open > 0:
        w(f"    Open ${candle_open:>12,.2f}    "
          f"Δ {dir_c}{B}{delta_sign}${abs(delta):,.2f}{R}  {dir_c}{direction}{R}\n")

    # ── Candle Timer ──
    if to_start > 0:
        w(f"\n  ⏳ Starts in {B}{format_countdown(to_start)}{R}\n")
    else:
        bar_len = 40
        filled = max(0, int((elapsed / CANDLE_SECONDS) * bar_len))
        bar = f"{CYAN}{'━' * filled}{R}{D}{'╌' * (bar_len - filled)}{R}"
        w(f"\n  [{bar}] {B}{format_countdown(remaining)}{R} {D}T+{int(elapsed)}s{R}\n")

    # ── Phase / Quote Status ──
    if phase == "OBSERVING":
        secs_left = max(0, DECISION_SECOND - elapsed)
        w(f"  {MAGENTA}🔬 Observing ({int(secs_left)}s to decision){R}\n")
    elif phase == "QUOTING":
        qi = quote_info
        bid_p = qi.get("bid_price", 0)
        side = qi.get("side", "?")
        cur_ask = qi.get("current_ask", 0)
        if qi.get("filled"):
            w(f"  {GREEN}✅ FILLED{R}  Bid {B}{side}{R}@${bid_p:.2f}  — waiting for close\n")
        else:
            w(f"  {YELLOW}📋 BID {side}{R}@${bid_p:.2f}"
              f"  {D}ask: ${cur_ask:.2f}  need ≤${bid_p:.2f}{R}\n")
    elif phase == "SKIPPED":
        w(f"  {YELLOW}🚫 Skipped — monitoring{R}\n")
    else:
        w(f"  {D}{phase}{R}\n")

    # ── Stats Grid ──
    if stats.total_candles > 0:
        w(f"\n  {D}{'─' * 62}{R}\n")
        w(f"  Quotes {B}{stats.total_quotes}{R}"
          f"  │  Fills {B}{stats.total_fills}{R}"
          f"  │  Rate {B}{stats.fill_rate:.0f}%{R}"
          f"  │  P&L {pnl_c}{B}{pnl_s}${stats.total_pnl:.4f}{R}\n")
        if stats.total_rebates > 0:
            w(f"  {D}Rebates: {GREEN}+${stats.total_rebates:.4f}{R}"
              f"  {D}│  Net: {pnl_c}{pnl_s}${stats.total_pnl + stats.total_rebates:.4f}{R}\n")
        w(f"  {D}{'─' * 62}{R}\n")

    # ── Last Trade ──
    if stats.trades:
        lt = stats.trades[-1]

        if lt.decision.startswith("QUOTE"):
            if lt.filled:
                result_c = GREEN if lt.won else RED
                result_tag = f"{result_c}{B}{'WIN' if lt.won else 'LOSS'}{R}"
                pnl_sign = "+" if lt.pnl >= 0 else ""
                w(f"\n  {D}Last #{lt.candle_num}{R}  {result_tag}  "
                  f"Bid {B}{lt.predicted_direction}{R}@${lt.bid_price:.2f}"
                  f"  →  {lt.actual_direction}"
                  f"  {result_c}{pnl_sign}${lt.pnl:.4f}{R}"
                  f"  {D}Rebate +${lt.maker_rebate:.4f}{R}\n")
            else:
                w(f"\n  {D}Last #{lt.candle_num}  {YELLOW}NO FILL{R}"
                  f" {D}Bid {lt.predicted_direction}@${lt.bid_price:.2f}"
                  f"  spread ${lt.spread:.2f}{R}\n")
        else:
            reason = lt.decision.replace("SKIP_", "")
            w(f"\n  {D}Last #{lt.candle_num}  {YELLOW}SKIP{R} {D}({reason}){R}\n")

        w(f"  {D}BTC ${lt.open_price:,.0f}→${lt.close_price:,.0f}"
          f"  ({'+' if lt.delta >= 0 else ''}${lt.delta:,.0f}){R}\n")

    w(f"\n  {D}Ctrl+C for report{R}\n")

    return buf.getvalue()


# ─────────────────────────────────────────────────────────────
# CANDLE SIMULATION
# ─────────────────────────────────────────────────────────────

async def simulate_candle(
    session: aiohttp.ClientSession,
    market: dict,
    candle_num: int,
    stats: Stats,
) -> Trade:
    """
    Simulate one candle with one-sided maker quoting:

    Phase 1 (0 – T+15s): OBSERVE — watch BTC, don't quote yet
    Phase 2 (T+15s): DECISION — predict direction, place bid
    Phase 3 (T+15s – close): QUOTING — monitor for fill
    Phase 4 (close): SETTLE — calculate P&L
    """
    global circuit_breaker_active, circuit_breaker_until
    event_start = market["event_start"]
    event_end = market["event_end"]
    token_up = market["token_up_id"]
    token_down = market["token_down_id"]

    candle_open = 0.0
    decision = ""
    predicted_direction = ""
    bid_price = 0.0
    bid_size_usd = 0.0
    filled = False
    fill_time_elapsed = 0.0
    best_bid_at_decision = 0.0
    best_ask_at_decision = 0.0
    spread_at_decision = 0.0
    decision_btc_price = 0.0
    decision_delta = 0.0

    # Launch WS orderbook streams
    ws_stop = asyncio.Event()
    ws_up_task = asyncio.create_task(orderbook_ws_stream(token_up, ws_stop))
    ws_dn_task = asyncio.create_task(orderbook_ws_stream(token_down, ws_stop))

    # Wait for candle to start
    while True:
        now = datetime.now(timezone.utc)
        to_start = (event_start - now).total_seconds()
        if to_start <= 0:
            break
        update_ui(market=market, remaining=CANDLE_SECONDS, candle_open=0,
                  stats=stats, phase="WAITING")
        await asyncio.sleep(1)

    candle_open = btc_price

    # ── Main candle loop ──
    decision_made = False
    decision_time = 0.0

    while True:
        now = datetime.now(timezone.utc)
        remaining = (event_end - now).total_seconds()
        elapsed = CANDLE_SECONDS - remaining

        if remaining <= 0:
            break

        # ── Phase 1: OBSERVE (first 15s) ──
        if elapsed < DECISION_SECOND:
            update_ui(market=market, remaining=remaining, candle_open=candle_open,
                      stats=stats, phase="OBSERVING")

        # ── Phase 2: DECISION (at T+15s) ──
        elif not decision_made:
            decision_btc_price = btc_price
            btc_delta = btc_price - candle_open
            decision_delta = btc_delta
            abs_delta = abs(btc_delta)

            # Check staleness
            stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else float('inf')
            if stale_age > PRICE_STALE_RECONNECT:
                decision = "SKIP_STALE_PRICE"
                decision_made = True
            elif circuit_breaker_active and time.time() < circuit_breaker_until:
                decision = "SKIP_COOLDOWN"
                decision_made = True
            elif abs_delta < SAFE_BUFFER_USD:
                decision = "SKIP_RISKY"
                decision_made = True
            elif stats.balance <= 0.001:
                decision = "SKIP_BROKE"
                decision_made = True
            else:
                # Predict direction
                if btc_delta > 0:
                    predicted_direction = "UP"
                    target_token = token_up
                else:
                    predicted_direction = "DOWN"
                    target_token = token_down

                # Get orderbook for the predicted winning side
                ob = await fetch_orderbook(session, target_token)
                best_bid_at_decision = ob["bid_price"]
                best_ask_at_decision = ob["ask_price"]
                spread_at_decision = ob["spread"]

                if best_bid_at_decision <= 0 or best_ask_at_decision <= 0:
                    decision = "SKIP_NO_BOOK"
                    decision_made = True
                elif spread_at_decision > MAX_SPREAD:
                    decision = "SKIP_WIDE_SPREAD"
                    decision_made = True
                else:
                    # Set our bid: best_bid + tick (front of queue)
                    bid_price = min(best_bid_at_decision + BID_OFFSET, MAX_BUY_PRICE)
                    bid_price = max(bid_price, MIN_BUY_PRICE)

                    # Don't bid above or at the ask (that's taking, not making)
                    if bid_price >= best_ask_at_decision:
                        bid_price = best_ask_at_decision - TICK_SIZE

                    if bid_price < MIN_BUY_PRICE:
                        decision = "SKIP_BID_TOO_LOW"
                        decision_made = True
                    else:
                        # Reset circuit breaker if cooldown expired
                        if circuit_breaker_active:
                            circuit_breaker_active = False
                            circuit_breaker_until = 0.0

                        effective_bal = min(stats.balance, STARTING_BALANCE)
                        bid_size_usd = min(MAX_TRADE_USD, effective_bal * BET_FRACTION)
                        decision = f"QUOTE_{predicted_direction}"
                        decision_made = True
                        decision_time = time.time()

        # ── Phase 3: QUOTING — check for simulated fill ──
        if decision.startswith("QUOTE") and not filled:
            target_token = token_up if predicted_direction == "UP" else token_down
            ob = get_cached_orderbook(target_token)

            # Fill condition: the ask drops to or below our bid
            # This means someone is willing to sell at our price
            if ob["ask_price"] > 0 and ob["ask_price"] <= bid_price:
                filled = True
                fill_time_elapsed = time.time() - decision_time

            update_ui(market=market, remaining=remaining, candle_open=candle_open,
                      stats=stats, phase="QUOTING",
                      quote_info={
                          "side": predicted_direction,
                          "bid_price": bid_price,
                          "spread": spread_at_decision,
                          "filled": filled,
                          "current_ask": ob["ask_price"],
                      })
        elif not decision.startswith("QUOTE"):
            update_ui(market=market, remaining=remaining, candle_open=candle_open,
                      stats=stats, phase="OBSERVING" if not decision_made else "SKIPPED")

        await adaptive_sleep(remaining, DECISION_SECOND)

    # ── Phase 4: SETTLE ──
    ws_stop.set()
    ws_up_task.cancel()
    ws_dn_task.cancel()

    close_price = btc_price
    total_delta = close_price - candle_open
    actual_direction = "UP" if total_delta >= 0 else "DOWN"

    if not decision_made:
        decision = "SKIP_RISKY"

    # Calculate P&L
    won = False
    pnl = 0.0
    pnl_pct = 0.0
    maker_rebate = 0.0

    if decision.startswith("QUOTE") and filled:
        shares = bid_size_usd / bid_price if bid_price > 0 else 0

        if predicted_direction == actual_direction:
            # Won: shares pay out $1.00 each
            payout = shares * 1.0
            pnl = payout - bid_size_usd  # NO taker fee for makers!
            pnl_pct = (pnl / bid_size_usd) * 100
            won = True
        else:
            # Lost: shares are worthless
            pnl = -bid_size_usd
            pnl_pct = -100.0
            won = False

        # Estimate maker rebate: 20% of taker fees collected on counterparty
        # The person who sold to us was a taker, so they paid fees
        taker_fee_on_counterparty = bid_size_usd * calc_taker_fee(bid_price)
        maker_rebate = taker_fee_on_counterparty * MAKER_REBATE_PCT

    balance_after = stats.balance + pnl + maker_rebate

    trade = Trade(
        candle_num=candle_num,
        slug=market["slug"],
        title=market["title"],
        timestamp=datetime.now(timezone.utc).isoformat(),
        open_price=candle_open,
        close_price=close_price,
        delta=total_delta,
        actual_direction=actual_direction,
        predicted_direction=predicted_direction,
        decision=decision,
        bid_price=bid_price,
        bid_size_usd=bid_size_usd,
        filled=filled,
        won=won,
        pnl=pnl,
        pnl_pct=pnl_pct,
        maker_rebate=maker_rebate,
        balance_after=balance_after,
        best_bid=best_bid_at_decision,
        best_ask=best_ask_at_decision,
        spread=spread_at_decision,
        fill_time_elapsed=fill_time_elapsed,
        decision_btc_price=decision_btc_price,
        decision_delta=decision_delta,
    )

    return trade


# ─────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────

def print_final_report(stats: Stats):
    print(f"\n\n{B}{'═' * 64}{R}")
    print(f"{B}  🏦 MAKER BOT — ONE-SIDED — FINAL REPORT{R}")
    print(f"{B}{'═' * 64}{R}")

    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_s = "+" if stats.total_pnl >= 0 else ""
    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED

    print(f"\n  {B}Session Summary{R}")
    print(f"  {'─' * 40}")
    print(f"  Candles observed:  {stats.total_candles}")
    print(f"  Quotes placed:    {stats.total_quotes}")
    print(f"  Fills received:   {stats.total_fills}")
    print(f"  Fill rate:        {stats.fill_rate:.1f}%")
    print(f"  Skips:            {stats.total_skips}")

    if stats.total_fills > 0:
        print(f"\n  {B}Trading Results{R}")
        print(f"  {'─' * 40}")
        print(f"  Wins:             {GREEN}{stats.wins}{R}")
        print(f"  Losses:           {RED}{stats.losses}{R}")
        print(f"  Win Rate:         {B}{stats.win_rate:.1f}%{R}")
        print(f"  Total Wagered:    ${stats.total_wagered:.2f}")
        print(f"  Total P&L:        {pnl_c}{pnl_s}${stats.total_pnl:.4f}{R}")
        print(f"  Maker Rebates:    {GREEN}+${stats.total_rebates:.4f}{R}")
        print(f"  Net P&L + Rebate: {pnl_c}{pnl_s}${stats.total_pnl + stats.total_rebates:.4f}{R}")
        print(f"  ROI:              {pnl_c}{pnl_s}{stats.roi:.1f}%{R}")
        print(f"  Avg P&L/fill:     ${stats.avg_pnl:.4f}")
        print(f"  Best Trade:       {GREEN}+${stats.best_trade:.4f}{R}")
        print(f"  Worst Trade:      {RED}${stats.worst_trade:.4f}{R}")

    print(f"\n  {B}Balance{R}")
    print(f"  {'─' * 40}")
    print(f"  Starting:         ${STARTING_BALANCE:.2f}")
    print(f"  Final:            {bal_c}${stats.balance:.4f}{R}")
    print(f"  Peak:             {GREEN}${stats.peak_balance:.4f}{R}")
    print(f"  Valley:           {RED}${stats.valley_balance:.4f}{R}")
    print(f"  Change:           {bal_c}{'+' if stats.balance_change_pct >= 0 else ''}"
          f"{stats.balance_change_pct:.1f}%{R}")
    print(f"\n{B}{'═' * 64}{R}\n")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Maker Bot — One-Sided Paper Trading")
    parser.add_argument("--candles", type=int, default=0,
                        help="Number of candles to run (0 = unlimited)")
    args = parser.parse_args()

    stats = Stats()
    init_csv(CSV_FILE)

    session = create_session()
    shutdown_event = asyncio.Event()

    # Start background tasks
    price_task = asyncio.create_task(btc_price_stream(session))
    ui_task = asyncio.create_task(ui_renderer(build_screen, shutdown_event))

    candle_num = 0

    try:
        while True:
            # Wait for BTC price
            while btc_price == 0:
                await asyncio.sleep(0.5)

            market = await discover_market(session)
            if not market:
                await asyncio.sleep(5)
                continue

            candle_num += 1
            trade = await simulate_candle(session, market, candle_num, stats)
            stats.record(trade)
            log_trade_csv(CSV_FILE, trade, stats)

            # Circuit breaker: trigger on consecutive losses
            if (trade.filled and not trade.won
                    and stats.current_streak <= -CIRCUIT_BREAKER_LOSSES
                    and not circuit_breaker_active):
                circuit_breaker_active = True
                circuit_breaker_until = time.time() + CIRCUIT_BREAKER_COOLDOWN

            if args.candles > 0 and candle_num >= args.candles:
                break

            await asyncio.sleep(2)

    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        price_task.cancel()
        ui_task.cancel()
        await session.close()
        print_final_report(stats)


if __name__ == "__main__":
    asyncio.run(main())
