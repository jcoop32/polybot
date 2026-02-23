#!/usr/bin/env python3
"""
Paper Trade Maker — Two-Sided Quoting Strategy

Simulates a true MARKET MAKER on BTC 5-min candle markets:
  - Places limit BUY orders on BOTH UP and DOWN tokens
  - If both fill → locked profit: $1.00 - (up_price + down_price)
  - If only one fills → exposed to directional risk
  - Uses BTC-derived fair value to set quote prices
  - Inventory skewing: adjusts quotes when one side fills first
  - Zero taker fees, plus estimated 20% maker rebate

This is the most capital-efficient strategy: you profit from
the spread regardless of which direction BTC moves.

Usage:
    python paper_trade_maker_two.py                # Run until Ctrl+C
    python paper_trade_maker_two.py --candles 20   # Run for 20 candles
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
MAX_BUY_PRICE = 0.60          # Don't bid higher than this per side
MIN_BUY_PRICE = 0.10          # Don't bid lower than this
MAX_COMBINED_COST = 0.96      # Max total cost for UP+DOWN (must be < 1.00)
MAX_TRADE_USD = 5.0           # Max simulated order size PER SIDE
MIN_ASK_SIZE_USD = 5.0
MAX_SPREAD = 0.08             # Max spread per side
TICK_SIZE = 0.01

# Two-sided quoting parameters
HALF_SPREAD = 0.02            # Quote this far from fair value on each side
SKEW_STEP = 0.01              # Skew quotes by this much when one side fills
REQUOTE_THRESHOLD = 0.02      # Requote if fair value moves by this much

# Fair value model
SENSITIVITY = 3.0             # Sigmoid sensitivity (higher = snappier)
VOL_WINDOW_USD = 50.0         # Default BTC volatility estimate for normalization

# Timing
QUOTE_START_SECOND = 15       # Start quoting at T+15s
CANDLE_SECONDS = 300
CANCEL_BEFORE_CLOSE = 10      # Cancel all orders 10s before candle close

# BTC price staleness
PRICE_STALE_SECONDS = 10
PRICE_STALE_RECONNECT = 30

# Maker rebate
MAKER_REBATE_PCT = 0.20       # 20% of taker fees

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
COINCAP_URL = "https://api.coincap.io/v2/assets/bitcoin"

CSV_FILE = "csv_logs/paper_trades_maker_two.csv"


# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

btc_price: float = 0.0
btc_price_updated: float = 0.0
price_feed_status: str = "connecting"
bot_start_time: float = time.time()


# ─────────────────────────────────────────────────────────────
# FAIR VALUE MODEL
# ─────────────────────────────────────────────────────────────

def fair_value_up(btc_now: float, candle_open: float, vol: float = VOL_WINDOW_USD) -> float:
    """
    Estimate P(UP) using a sigmoid of BTC's delta from candle open.
    Normalized by volatility so the model adapts to market conditions.

    Returns: probability between 0.05 and 0.95
    """
    if candle_open <= 0 or vol <= 0:
        return 0.50
    delta = btc_now - candle_open
    z = delta / vol
    p = 1.0 / (1.0 + math.exp(-z * SENSITIVITY))
    return max(0.05, min(0.95, p))


def calc_quotes(p_up: float, half_spread: float, skew: float = 0.0):
    """
    Calculate bid prices for both sides given fair value.

    Args:
        p_up: Fair value probability of UP (0.05-0.95)
        half_spread: Half the desired spread width
        skew: Skew adjustment (+ve = want more DOWN fills, raise DOWN bid)

    Returns:
        (up_bid, down_bid) — clamped to valid range
    """
    p_down = 1.0 - p_up

    up_bid = p_up - half_spread - skew
    down_bid = p_down - half_spread + skew

    # Clamp to valid range
    up_bid = max(MIN_BUY_PRICE, min(MAX_BUY_PRICE, round(up_bid / TICK_SIZE) * TICK_SIZE))
    down_bid = max(MIN_BUY_PRICE, min(MAX_BUY_PRICE, round(down_bid / TICK_SIZE) * TICK_SIZE))

    # Ensure combined cost stays profitable
    if up_bid + down_bid >= MAX_COMBINED_COST:
        excess = (up_bid + down_bid) - MAX_COMBINED_COST + TICK_SIZE
        up_bid -= excess / 2
        down_bid -= excess / 2
        up_bid = round(up_bid / TICK_SIZE) * TICK_SIZE
        down_bid = round(down_bid / TICK_SIZE) * TICK_SIZE

    return up_bid, down_bid


# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """Record of one simulated two-sided maker candle."""
    candle_num: int
    slug: str
    title: str
    timestamp: str
    open_price: float           # BTC open
    close_price: float          # BTC close
    delta: float
    actual_direction: str
    decision: str               # "QUOTE_BOTH", "SKIP_*"
    # UP side
    up_bid: float               # Our bid price for UP
    up_filled: bool
    up_fill_time: float         # Seconds after quoting until fill
    # DOWN side
    down_bid: float             # Our bid price for DOWN
    down_filled: bool
    down_fill_time: float
    # Results
    both_filled: bool           # Dream scenario: locked profit
    single_side: str            # "UP", "DOWN", "" — which side filled alone
    bid_size_usd: float         # Per-side size
    total_cost: float           # Combined cost if both filled
    locked_profit: float        # Profit if both sides filled
    pnl: float
    pnl_pct: float
    maker_rebate: float
    balance_after: float
    # Context
    fair_value: float = 0.50
    skew: float = 0.0
    up_best_ask: float = 0.0
    down_best_ask: float = 0.0
    up_spread: float = 0.0
    down_spread: float = 0.0


@dataclass
class Stats:
    """Running statistics for two-sided maker."""
    balance: float = STARTING_BALANCE
    trades: list[Trade] = field(default_factory=list)
    total_candles: int = 0
    total_quotes: int = 0       # Candles where we quoted
    both_fills: int = 0         # Both sides filled (best case)
    up_only_fills: int = 0      # Only UP filled
    down_only_fills: int = 0    # Only DOWN filled
    no_fills: int = 0           # Neither side filled
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

    @property
    def fill_rate(self) -> float:
        if self.total_quotes == 0:
            return 0.0
        fills = self.both_fills + self.up_only_fills + self.down_only_fills
        return fills / self.total_quotes * 100

    @property
    def both_fill_rate(self) -> float:
        return (self.both_fills / self.total_quotes * 100) if self.total_quotes > 0 else 0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0

    @property
    def roi(self) -> float:
        return (self.total_pnl / self.total_wagered * 100) if self.total_wagered > 0 else 0

    @property
    def avg_pnl(self) -> float:
        fills = self.both_fills + self.up_only_fills + self.down_only_fills
        return self.total_pnl / fills if fills > 0 else 0

    @property
    def balance_change_pct(self) -> float:
        return ((self.balance - STARTING_BALANCE) / STARTING_BALANCE * 100)

    def record(self, trade: Trade):
        self.trades.append(trade)
        self.total_candles += 1

        if trade.decision.startswith("QUOTE"):
            self.total_quotes += 1

            if trade.both_filled:
                self.both_fills += 1
                self.total_wagered += trade.total_cost
                self.total_pnl += trade.pnl
                self.total_rebates += trade.maker_rebate
                self.balance += trade.pnl + trade.maker_rebate
                self.wins += 1  # Both-fill is always a win
                self.best_trade = max(self.best_trade, trade.pnl)
            elif trade.single_side:
                if trade.single_side == "UP":
                    self.up_only_fills += 1
                else:
                    self.down_only_fills += 1
                self.total_wagered += trade.bid_size_usd
                self.total_pnl += trade.pnl
                self.total_rebates += trade.maker_rebate
                self.balance += trade.pnl + trade.maker_rebate
                if trade.pnl >= 0:
                    self.wins += 1
                else:
                    self.losses += 1
                self.best_trade = max(self.best_trade, trade.pnl)
                self.worst_trade = min(self.worst_trade, trade.pnl)
            else:
                self.no_fills += 1

            self.peak_balance = max(self.peak_balance, self.balance)
            self.valley_balance = min(self.valley_balance, self.balance)
        else:
            self.total_skips += 1


# ─────────────────────────────────────────────────────────────
# BTC PRICE STREAM
# ─────────────────────────────────────────────────────────────

async def btc_price_stream(session: aiohttp.ClientSession):
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
                "candle_num", "timestamp", "slug",
                "btc_open", "btc_close", "btc_delta", "actual_direction",
                "decision", "fair_value",
                "up_bid", "up_filled", "down_bid", "down_filled",
                "both_filled", "single_side",
                "bid_size_usd", "total_cost", "locked_profit",
                "pnl", "pnl_pct", "maker_rebate",
                "cumulative_pnl", "balance",
                "both_fill_rate", "fill_rate",
            ])


def log_trade_csv(path: str, trade: Trade, stats: Stats):
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            trade.candle_num,
            datetime.now(timezone.utc).isoformat(),
            trade.slug,
            f"{trade.open_price:.2f}",
            f"{trade.close_price:.2f}",
            f"{trade.delta:.2f}",
            trade.actual_direction,
            trade.decision,
            f"{trade.fair_value:.4f}",
            f"{trade.up_bid:.4f}",
            trade.up_filled,
            f"{trade.down_bid:.4f}",
            trade.down_filled,
            trade.both_filled,
            trade.single_side,
            f"{trade.bid_size_usd:.2f}",
            f"{trade.total_cost:.4f}",
            f"{trade.locked_profit:.4f}",
            f"{trade.pnl:.4f}",
            f"{trade.pnl_pct:.2f}",
            f"{trade.maker_rebate:.4f}",
            f"{stats.total_pnl:.4f}",
            f"{stats.balance:.4f}",
            f"{stats.both_fill_rate:.1f}",
            f"{stats.fill_rate:.1f}",
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
    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_s = "+" if stats.total_pnl >= 0 else ""

    # ── Header ──
    w(f"  {B}┌{'─' * 62}┐{R}\n")
    w(f"  {B}│{R}  🏦 {B}MAKER — TWO-SIDED{R}"
      f"                  {D}Paper Trading{R}   {B}│{R}\n")
    w(f"  {B}│{R}  {D}{market['title']:<60}{R}{B}│{R}\n")
    w(f"  {B}├{'─' * 62}┤{R}\n")
    uptime_str = format_uptime(bot_start_time)
    w(f"  {B}│{R}  💰 {bal_c}{B}${stats.balance:.4f}{R}"
      f"                    🕐 {uptime_str}"
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
    qi = quote_info
    if phase == "OBSERVING":
        secs_left = max(0, QUOTE_START_SECOND - elapsed)
        w(f"  {MAGENTA}🔬 Observing ({int(secs_left)}s to quote){R}\n")
    elif phase == "QUOTING":
        fv = qi.get("fair_value", 0.5)
        up_bid = qi.get("up_bid", 0)
        dn_bid = qi.get("down_bid", 0)
        up_filled = qi.get("up_filled", False)
        dn_filled = qi.get("down_filled", False)
        up_ask = qi.get("up_ask", 0)
        dn_ask = qi.get("down_ask", 0)

        w(f"  {D}FV: P(UP)={fv:.2f}  P(DN)={1-fv:.2f}{R}\n")

        # Compact two-line quote display
        up_stat = f"{GREEN}✓{R}" if up_filled else f"{YELLOW}○{R}"
        dn_stat = f"{GREEN}✓{R}" if dn_filled else f"{YELLOW}○{R}"
        w(f"  {up_stat} UP  @${up_bid:.2f}")
        if not up_filled and up_ask > 0:
            w(f"  {D}ask ${up_ask:.2f}{R}")
        w(f"     {dn_stat} DN  @${dn_bid:.2f}")
        if not dn_filled and dn_ask > 0:
            w(f"  {D}ask ${dn_ask:.2f}{R}")
        w("\n")

        # Spread profit line
        combined = up_bid + dn_bid
        spread_profit = 1.0 - combined
        w(f"  {D}Combined ${combined:.2f}  →  "
          f"Spread {GREEN}+${spread_profit:.4f}{R}\n")

        if up_filled and dn_filled:
            w(f"  {GREEN}{B}🎉 BOTH FILLED — Locked profit{R}\n")
        elif up_filled or dn_filled:
            side = "UP" if up_filled else "DN"
            w(f"  {YELLOW}⚠ Only {side} filled — directional{R}\n")
    elif phase == "CANCELLED":
        w(f"  {D}🛑 Cancelled — waiting for close{R}\n")
    elif phase == "SKIPPED":
        w(f"  {YELLOW}🚫 Skipped{R}\n")
    else:
        w(f"  {D}{phase}{R}\n")

    # ── Stats Grid ──
    if stats.total_candles > 0:
        one_fills = stats.up_only_fills + stats.down_only_fills
        w(f"\n  {D}{'─' * 62}{R}\n")
        w(f"  Quoted {B}{stats.total_quotes}{R}"
          f"  │  Both {GREEN}{stats.both_fills}{R}"
          f"  One {YELLOW}{one_fills}{R}"
          f"  None {D}{stats.no_fills}{R}"
          f"  │  P&L {pnl_c}{B}{pnl_s}${stats.total_pnl:.4f}{R}\n")
        if stats.total_rebates > 0:
            w(f"  {D}Rebates: {GREEN}+${stats.total_rebates:.4f}{R}"
              f"  {D}│  Net: {pnl_c}{pnl_s}${stats.total_pnl + stats.total_rebates:.4f}{R}\n")
        w(f"  {D}{'─' * 62}{R}\n")

    # ── Last Trade ──
    if stats.trades:
        lt = stats.trades[-1]

        if lt.decision.startswith("QUOTE"):
            if lt.both_filled:
                w(f"\n  {D}Last #{lt.candle_num}{R}  "
                  f"{GREEN}{B}BOTH{R}  "
                  f"UP@${lt.up_bid:.2f} + DN@${lt.down_bid:.2f}"
                  f"  = ${lt.total_cost:.2f}"
                  f"  →  {GREEN}+${lt.locked_profit:.4f}{R}\n")
            elif lt.single_side:
                result_c = GREEN if lt.pnl >= 0 else RED
                result_tag = f"{result_c}{B}{'WIN' if lt.pnl >= 0 else 'LOSS'}{R}"
                pnl_sign = "+" if lt.pnl >= 0 else ""
                filled_p = lt.up_bid if lt.single_side == "UP" else lt.down_bid
                w(f"\n  {D}Last #{lt.candle_num}{R}  {result_tag}  "
                  f"Only {B}{lt.single_side}{R}@${filled_p:.2f}"
                  f"  →  {lt.actual_direction}"
                  f"  {result_c}{pnl_sign}${lt.pnl:.4f}{R}"
                  f"  {D}Rebate +${lt.maker_rebate:.4f}{R}\n")
            else:
                w(f"\n  {D}Last #{lt.candle_num}  {YELLOW}NO FILLS{R}"
                  f" {D}UP ask ${lt.up_best_ask:.2f}  DN ask ${lt.down_best_ask:.2f}{R}\n")
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
    Simulate one candle with two-sided maker quoting:

    Phase 1 (0 – T+15s): OBSERVE — let BTC establish direction
    Phase 2 (T+15s): QUOTE — place bids on both UP and DOWN
    Phase 3 (T+15s – close-10s): MONITOR — check for fills, requote
    Phase 4 (close-10s): CANCEL remaining unfilled orders
    Phase 5 (close): SETTLE P&L
    """
    event_start = market["event_start"]
    event_end = market["event_end"]
    token_up = market["token_up_id"]
    token_down = market["token_down_id"]

    candle_open = 0.0
    decision = ""
    up_bid = 0.0
    down_bid = 0.0
    up_filled = False
    down_filled = False
    up_fill_time = 0.0
    down_fill_time = 0.0
    bid_size_usd = 0.0
    current_fair_value = 0.50
    current_skew = 0.0
    up_best_ask = 0.0
    down_best_ask = 0.0
    up_spread = 0.0
    down_spread = 0.0

    # Launch WS orderbook streams
    ws_stop = asyncio.Event()
    ws_up = asyncio.create_task(orderbook_ws_stream(token_up, ws_stop))
    ws_dn = asyncio.create_task(orderbook_ws_stream(token_down, ws_stop))

    # Wait for candle start
    while True:
        now = datetime.now(timezone.utc)
        to_start = (event_start - now).total_seconds()
        if to_start <= 0:
            break
        update_ui(market=market, remaining=CANDLE_SECONDS, candle_open=0,
                  stats=stats, phase="WAITING")
        await asyncio.sleep(1)

    candle_open = btc_price
    quoting_started = False
    quote_time = 0.0
    cancelled = False
    last_fv_btc = 0.0           # Track BTC price used for last fair value calc

    # ── Main candle loop ──
    while True:
        now = datetime.now(timezone.utc)
        remaining = (event_end - now).total_seconds()
        elapsed = CANDLE_SECONDS - remaining

        if remaining <= 0:
            break

        # ── Phase 1: OBSERVE ──
        if elapsed < QUOTE_START_SECOND:
            update_ui(market=market, remaining=remaining, candle_open=candle_open,
                      stats=stats, phase="OBSERVING")

        # ── Phase 2: INITIAL QUOTE ──
        elif not quoting_started:
            stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else float('inf')
            if stale_age > PRICE_STALE_RECONNECT:
                decision = "SKIP_STALE_PRICE"
                quoting_started = True
            elif stats.balance <= 0.001:
                decision = "SKIP_BROKE"
                quoting_started = True
            else:
                # Calculate fair value and initial quotes
                current_fair_value = fair_value_up(btc_price, candle_open)
                last_fv_btc = btc_price
                up_bid, down_bid = calc_quotes(current_fair_value, HALF_SPREAD)

                # Validate against orderbooks
                up_ob = await fetch_orderbook(session, token_up)
                dn_ob = await fetch_orderbook(session, token_down)
                up_best_ask = up_ob["ask_price"]
                down_best_ask = dn_ob["ask_price"]
                up_spread = up_ob["spread"]
                down_spread = dn_ob["spread"]

                # Don't bid at or above ask (would be a taker)
                if up_best_ask > 0 and up_bid >= up_best_ask:
                    up_bid = up_best_ask - TICK_SIZE
                if down_best_ask > 0 and down_bid >= down_best_ask:
                    down_bid = down_best_ask - TICK_SIZE

                if up_bid < MIN_BUY_PRICE and down_bid < MIN_BUY_PRICE:
                    decision = "SKIP_BIDS_TOO_LOW"
                    quoting_started = True
                elif up_bid + down_bid >= 1.0:
                    decision = "SKIP_NO_EDGE"
                    quoting_started = True
                else:
                    effective_bal = min(stats.balance, STARTING_BALANCE)
                    bid_size_usd = min(MAX_TRADE_USD, effective_bal / 2)
                    decision = "QUOTE_BOTH"
                    quoting_started = True
                    quote_time = time.time()

        # ── Phase 3: MONITOR for fills ──
        if decision == "QUOTE_BOTH" and not cancelled:
            # Snapshot orderbooks ONCE per tick per token
            up_ob = get_cached_orderbook(token_up)
            dn_ob = get_cached_orderbook(token_down)
            up_best_ask = up_ob["ask_price"]
            down_best_ask = dn_ob["ask_price"]

            # Check UP fill
            if not up_filled:
                if up_best_ask > 0 and up_best_ask <= up_bid:
                    up_filled = True
                    up_fill_time = time.time() - quote_time

                    # Inventory skew: we're long UP, skew to attract DOWN fills
                    if not down_filled:
                        current_skew = -SKEW_STEP  # Raise DOWN bid
                        _, down_bid_new = calc_quotes(current_fair_value, HALF_SPREAD, current_skew)
                        if down_best_ask > 0 and down_bid_new < down_best_ask:
                            down_bid = down_bid_new

            # Check DOWN fill
            if not down_filled:
                if down_best_ask > 0 and down_best_ask <= down_bid:
                    down_filled = True
                    down_fill_time = time.time() - quote_time

                    # Inventory skew: we're long DOWN, skew to attract UP fills
                    if not up_filled:
                        current_skew = SKEW_STEP  # Raise UP bid
                        up_bid_new, _ = calc_quotes(current_fair_value, HALF_SPREAD, current_skew)
                        if up_best_ask > 0 and up_bid_new < up_best_ask:
                            up_bid = up_bid_new

            # Requote only if BTC price actually changed since last fair value calc
            cur_btc = btc_price
            if cur_btc != last_fv_btc and (not up_filled or not down_filled):
                new_fv = fair_value_up(cur_btc, candle_open)
                if abs(new_fv - current_fair_value) > REQUOTE_THRESHOLD:
                    current_fair_value = new_fv
                    new_up, new_down = calc_quotes(current_fair_value, HALF_SPREAD, current_skew)
                    if not up_filled and up_best_ask > 0 and new_up < up_best_ask:
                        up_bid = new_up
                    if not down_filled and down_best_ask > 0 and new_down < down_best_ask:
                        down_bid = new_down
                last_fv_btc = cur_btc

            # Phase 4: CANCEL remaining orders near close
            if remaining <= CANCEL_BEFORE_CLOSE and not cancelled:
                cancelled = True
                # In real trading we'd cancel. In paper, just stop monitoring fills.

            update_ui(market=market, remaining=remaining, candle_open=candle_open,
                      stats=stats, phase="CANCELLED" if cancelled else "QUOTING",
                      quote_info={
                          "fair_value": current_fair_value,
                          "up_bid": up_bid, "up_filled": up_filled, "up_ask": up_best_ask,
                          "down_bid": down_bid, "down_filled": down_filled, "down_ask": down_best_ask,
                      })
        elif not decision.startswith("QUOTE"):
            update_ui(market=market, remaining=remaining, candle_open=candle_open,
                      stats=stats, phase="SKIPPED")

        await adaptive_sleep(remaining, QUOTE_START_SECOND)

    # ── Phase 5: SETTLE ──
    ws_stop.set()
    ws_up.cancel()
    ws_dn.cancel()

    close_price = btc_price
    total_delta = close_price - candle_open
    actual_direction = "UP" if total_delta >= 0 else "DOWN"

    if not quoting_started:
        decision = "SKIP_RISKY"

    # Calculate P&L
    both_filled = up_filled and down_filled
    single_side = ""
    pnl = 0.0
    pnl_pct = 0.0
    total_cost = 0.0
    locked_profit = 0.0
    maker_rebate = 0.0

    if decision.startswith("QUOTE"):
        shares_per_side = bid_size_usd  # Approximate: 1 share per $bid_price

        if both_filled:
            # Best case: both sides filled → locked profit
            up_shares = bid_size_usd / up_bid if up_bid > 0 else 0
            down_shares = bid_size_usd / down_bid if down_bid > 0 else 0
            # Use the minimum shares (balanced position)
            min_shares = min(up_shares, down_shares)
            total_cost = min_shares * (up_bid + down_bid)
            payout = min_shares * 1.0  # One side always pays $1.00
            locked_profit = payout - total_cost
            pnl = locked_profit
            pnl_pct = (pnl / total_cost) * 100 if total_cost > 0 else 0

            # Rebate on both fills
            taker_fee_up = bid_size_usd * calc_taker_fee(up_bid)
            taker_fee_dn = bid_size_usd * calc_taker_fee(down_bid)
            maker_rebate = (taker_fee_up + taker_fee_dn) * MAKER_REBATE_PCT

        elif up_filled and not down_filled:
            single_side = "UP"
            shares = bid_size_usd / up_bid if up_bid > 0 else 0
            if actual_direction == "UP":
                pnl = (shares * 1.0) - bid_size_usd
                pnl_pct = (pnl / bid_size_usd) * 100
            else:
                pnl = -bid_size_usd
                pnl_pct = -100.0

            taker_fee = bid_size_usd * calc_taker_fee(up_bid)
            maker_rebate = taker_fee * MAKER_REBATE_PCT

        elif down_filled and not up_filled:
            single_side = "DOWN"
            shares = bid_size_usd / down_bid if down_bid > 0 else 0
            if actual_direction == "DOWN":
                pnl = (shares * 1.0) - bid_size_usd
                pnl_pct = (pnl / bid_size_usd) * 100
            else:
                pnl = -bid_size_usd
                pnl_pct = -100.0

            taker_fee = bid_size_usd * calc_taker_fee(down_bid)
            maker_rebate = taker_fee * MAKER_REBATE_PCT

        # else: no fills, no P&L

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
        decision=decision,
        up_bid=up_bid,
        up_filled=up_filled,
        up_fill_time=up_fill_time,
        down_bid=down_bid,
        down_filled=down_filled,
        down_fill_time=down_fill_time,
        both_filled=both_filled,
        single_side=single_side,
        bid_size_usd=bid_size_usd,
        total_cost=total_cost,
        locked_profit=locked_profit,
        pnl=pnl,
        pnl_pct=pnl_pct,
        maker_rebate=maker_rebate,
        balance_after=balance_after,
        fair_value=current_fair_value,
        skew=current_skew,
        up_best_ask=up_best_ask,
        down_best_ask=down_best_ask,
        up_spread=up_spread,
        down_spread=down_spread,
    )

    return trade


# ─────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────

def print_final_report(stats: Stats):
    print(f"\n\n{B}{'═' * 64}{R}")
    print(f"{B}  🏦 MAKER BOT — TWO-SIDED — FINAL REPORT{R}")
    print(f"{B}{'═' * 64}{R}")

    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_s = "+" if stats.total_pnl >= 0 else ""
    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED

    print(f"\n  {B}Session Summary{R}")
    print(f"  {'─' * 40}")
    print(f"  Candles observed:   {stats.total_candles}")
    print(f"  Candles quoted:     {stats.total_quotes}")
    print(f"  Skips:              {stats.total_skips}")

    print(f"\n  {B}Fill Breakdown{R}")
    print(f"  {'─' * 40}")
    print(f"  Both filled:        {GREEN}{stats.both_fills}{R}  ({stats.both_fill_rate:.1f}%)")
    print(f"  UP only:            {YELLOW}{stats.up_only_fills}{R}")
    print(f"  DOWN only:          {YELLOW}{stats.down_only_fills}{R}")
    print(f"  No fills:           {D}{stats.no_fills}{R}")
    print(f"  Any fill rate:      {B}{stats.fill_rate:.1f}%{R}")

    fills = stats.both_fills + stats.up_only_fills + stats.down_only_fills
    if fills > 0:
        print(f"\n  {B}Trading Results{R}")
        print(f"  {'─' * 40}")
        print(f"  Wins:               {GREEN}{stats.wins}{R}")
        print(f"  Losses:             {RED}{stats.losses}{R}")
        print(f"  Win Rate:           {B}{stats.win_rate:.1f}%{R}")
        print(f"  Total Wagered:      ${stats.total_wagered:.2f}")
        print(f"  Total P&L:          {pnl_c}{pnl_s}${stats.total_pnl:.4f}{R}")
        print(f"  Maker Rebates:      {GREEN}+${stats.total_rebates:.4f}{R}")
        print(f"  Net P&L + Rebate:   {pnl_c}{pnl_s}${stats.total_pnl + stats.total_rebates:.4f}{R}")
        print(f"  Avg P&L/fill:       ${stats.avg_pnl:.4f}")
        print(f"  Best Trade:         {GREEN}+${stats.best_trade:.4f}{R}")
        print(f"  Worst Trade:        {RED}${stats.worst_trade:.4f}{R}")

    print(f"\n  {B}Balance{R}")
    print(f"  {'─' * 40}")
    print(f"  Starting:           ${STARTING_BALANCE:.2f}")
    print(f"  Final:              {bal_c}${stats.balance:.4f}{R}")
    print(f"  Peak:               {GREEN}${stats.peak_balance:.4f}{R}")
    print(f"  Valley:             {RED}${stats.valley_balance:.4f}{R}")
    print(f"  Change:             {bal_c}{'+' if stats.balance_change_pct >= 0 else ''}"
          f"{stats.balance_change_pct:.1f}%{R}")
    print(f"\n{B}{'═' * 64}{R}\n")


# ─────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Maker Bot — Two-Sided Paper Trading")
    parser.add_argument("--candles", type=int, default=0,
                        help="Number of candles to run (0 = unlimited)")
    args = parser.parse_args()

    stats = Stats()
    init_csv(CSV_FILE)

    session = create_session()
    shutdown_event = asyncio.Event()

    price_task = asyncio.create_task(btc_price_stream(session))
    ui_task = asyncio.create_task(ui_renderer(build_screen, shutdown_event))

    candle_num = 0

    try:
        while True:
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
