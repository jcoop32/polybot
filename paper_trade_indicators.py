#!/usr/bin/env python3
"""
PolySniper Paper Trader — Indicator-Based Edition

Uses technical indicators (RSI, EMA crossover, MACD, Bollinger Bands)
computed from recent BTC 1-minute candles to predict direction at the
START of each 5-minute Polymarket candle.

Buys in the first 30 seconds of each candle based on indicator consensus.
$10 bankroll, no-reinvest mode (effective balance always capped at $10).

Usage:
    python paper_trade_indicators.py                  # Run until Ctrl+C
    python paper_trade_indicators.py --candles 20     # Run for 20 candles
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
import numpy as np

from perf import (
    json_loads, json_dumps, create_session,
    orderbook_ws_stream, get_cached_orderbook, fetch_orderbook_rest,
    update_ui, ui_renderer, adaptive_sleep_early,
)


# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

STARTING_BALANCE = 10.00
MAX_BUY_PRICE = 0.97
MIN_BUY_PRICE = 0.10
MAX_TRADE_USD = 5.0
MIN_ASK_SIZE_USD = 5.0
MAX_SPREAD = 0.05

# Confidence-based position sizing
CONFIDENCE_SIZING = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}

# Risk management
DRAWDOWN_THRESHOLD = 0.50
DRAWDOWN_COOLDOWN = 3
LOSS_STREAK_LIMIT = 3

# Indicator parameters
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
EMA_FAST = 9
EMA_SLOW = 21
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_PERIOD = 20
BB_STD = 2
MIN_CONSENSUS = 2          # Need at least 2 indicators agreeing to trade

# Trade timing
INDICATOR_COMPUTE_DELAY = 5    # Seconds after candle open to compute indicators
EXECUTION_WINDOW_START = 15    # Seconds after candle open to execute trade
EXECUTION_WINDOW_END = 30      # Latest execution point

# BTC price staleness
PRICE_STALE_SECONDS = 10
PRICE_STALE_RECONNECT = 30

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
COINCAP_URL = "https://api.coincap.io/v2/assets/bitcoin"

CSV_FILE = "paper_trades_indicator.csv"


# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

btc_price: float = 0.0
btc_price_updated: float = 0.0
price_feed_status: str = "connecting"


# ─────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────

@dataclass
class IndicatorSnapshot:
    """Snapshot of all indicator values at decision time."""
    rsi: float = 0.0
    rsi_signal: int = 0         # +1 UP, -1 DOWN, 0 NEUTRAL
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_signal: int = 0
    macd_line: float = 0.0
    macd_signal_line: float = 0.0
    macd_histogram: float = 0.0
    macd_signal: int = 0
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_middle: float = 0.0
    bb_position: float = 0.0   # 0.0 = at lower band, 1.0 = at upper band
    bb_signal: int = 0
    consensus_score: int = 0    # Sum of all signals
    predicted_direction: str = ""  # "UP", "DOWN", or ""


@dataclass
class Trade:
    """Record of a single simulated trade."""
    candle_num: int
    slug: str
    title: str
    candle_start: str
    candle_end: str
    open_price: float           # BTC open
    close_price: float          # BTC close
    delta: float
    actual_direction: str
    decision: str               # "BUY_UP", "BUY_DOWN", "SKIP_*"
    side_bought: str
    buy_price: float            # Ask price
    bet_amount: float
    won: bool
    pnl: float
    pnl_pct: float
    balance_after: float
    # Orderbook context
    ask_size: float = 0.0
    book_depth_usd: float = 0.0
    opposing_ask_price: float = 0.0
    spread: float = 0.0
    # BTC tracking
    btc_high: float = 0.0
    btc_low: float = 0.0
    btc_volatility: float = 0.0
    decision_btc_price: float = 0.0
    decision_remaining: float = 0.0
    # Indicator fields
    rsi_value: float = 0.0
    ema_signal: int = 0
    macd_signal_val: int = 0
    bb_signal_val: int = 0
    consensus_score: int = 0
    confidence: str = ""


@dataclass
class Stats:
    """Running statistics across all trades."""
    balance: float = STARTING_BALANCE
    trades: list[Trade] = field(default_factory=list)
    total_candles: int = 0
    total_trades: int = 0
    total_skips: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_wagered: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    current_streak: int = 0
    longest_win_streak: int = 0
    longest_loss_streak: int = 0
    peak_balance: float = STARTING_BALANCE
    valley_balance: float = STARTING_BALANCE
    cooldown_remaining: int = 0
    shadow_wins: int = 0
    shadow_losses: int = 0

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
            # Shadow tracking
            if trade.actual_direction and trade.consensus_score != 0:
                predicted = "UP" if trade.consensus_score > 0 else "DOWN"
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
# TECHNICAL INDICATORS
# ─────────────────────────────────────────────────────────────

async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    limit: int = 100,
) -> list[dict]:
    """Fetch recent klines from Binance REST API."""
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        async with session.get(BINANCE_KLINES_URL, params=params,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return []
            raw = await resp.json()
            klines = []
            for k in raw:
                klines.append({
                    "open_time": k[0],
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]),
                    "close_time": k[6],
                })
            return klines
    except Exception:
        return []


def compute_ema(values: np.ndarray, period: int) -> np.ndarray:
    """Compute Exponential Moving Average using numpy."""
    if len(values) < period:
        return np.zeros(len(values))
    ema = np.zeros(len(values))
    ema[period - 1] = np.mean(values[:period])
    multiplier = 2.0 / (period + 1)
    for i in range(period, len(values)):
        ema[i] = (values[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def compute_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> np.ndarray:
    """Compute Relative Strength Index using numpy."""
    if len(closes) < period + 1:
        return np.full(len(closes), 50.0)

    rsi = np.full(len(closes), 50.0)
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i + 1] = 100 - (100 / (1 + rs))

    return rsi


def compute_macd(
    closes: np.ndarray,
    fast: int = MACD_FAST,
    slow: int = MACD_SLOW,
    signal_period: int = MACD_SIGNAL,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute MACD line, signal line, and histogram using numpy."""
    ema_fast = compute_ema(closes, fast)
    ema_slow = compute_ema(closes, slow)

    macd_line = np.where((ema_fast != 0) & (ema_slow != 0),
                         ema_fast - ema_slow, 0.0)

    valid_start = slow - 1
    valid_macd = macd_line[valid_start:]
    signal_line_partial = compute_ema(valid_macd, signal_period)
    signal_line = np.concatenate([np.zeros(valid_start), signal_line_partial])

    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_bollinger(
    closes: np.ndarray,
    period: int = BB_PERIOD,
    num_std: float = BB_STD,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Bollinger Bands using numpy."""
    upper = np.zeros(len(closes))
    middle = np.zeros(len(closes))
    lower = np.zeros(len(closes))

    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        sma = np.mean(window)
        std = np.std(window)
        middle[i] = sma
        upper[i] = sma + num_std * std
        lower[i] = sma - num_std * std

    return upper, middle, lower


def indicator_consensus(klines: list[dict]) -> IndicatorSnapshot:
    """
    Run all indicators on klines data and return consensus snapshot.

    Each indicator votes: +1 (UP), -1 (DOWN), 0 (NEUTRAL).
    Consensus = sum of votes.
    """
    if len(klines) < max(RSI_PERIOD, EMA_SLOW, MACD_SLOW, BB_PERIOD) + 5:
        return IndicatorSnapshot()

    closes = np.array([k["close"] for k in klines], dtype=np.float64)
    current_price = closes[-1]
    snap = IndicatorSnapshot()

    # ── RSI ──
    rsi_values = compute_rsi(closes, RSI_PERIOD)
    snap.rsi = rsi_values[-1]
    if snap.rsi < RSI_OVERSOLD:
        snap.rsi_signal = 1     # Oversold → expect bounce UP
    elif snap.rsi > RSI_OVERBOUGHT:
        snap.rsi_signal = -1    # Overbought → expect pullback DOWN
    else:
        snap.rsi_signal = 0

    # ── EMA Crossover ──
    ema_fast_vals = compute_ema(closes, EMA_FAST)
    ema_slow_vals = compute_ema(closes, EMA_SLOW)
    snap.ema_fast = ema_fast_vals[-1]
    snap.ema_slow = ema_slow_vals[-1]
    if snap.ema_fast > snap.ema_slow:
        snap.ema_signal = 1     # Fast above slow → bullish
    elif snap.ema_fast < snap.ema_slow:
        snap.ema_signal = -1    # Fast below slow → bearish
    else:
        snap.ema_signal = 0

    # ── MACD ──
    macd_line, signal_line, histogram = compute_macd(closes)
    snap.macd_line = macd_line[-1]
    snap.macd_signal_line = signal_line[-1]
    snap.macd_histogram = histogram[-1]
    if snap.macd_line > snap.macd_signal_line:
        snap.macd_signal = 1    # MACD above signal → bullish
    elif snap.macd_line < snap.macd_signal_line:
        snap.macd_signal = -1   # MACD below signal → bearish
    else:
        snap.macd_signal = 0

    # ── Bollinger Bands ──
    bb_upper, bb_middle, bb_lower = compute_bollinger(closes)
    snap.bb_upper = bb_upper[-1]
    snap.bb_middle = bb_middle[-1]
    snap.bb_lower = bb_lower[-1]
    if snap.bb_upper > snap.bb_lower:
        snap.bb_position = (current_price - snap.bb_lower) / (snap.bb_upper - snap.bb_lower)
    else:
        snap.bb_position = 0.5

    if current_price <= snap.bb_lower:
        snap.bb_signal = 1      # At/below lower band → expect bounce UP
    elif current_price >= snap.bb_upper:
        snap.bb_signal = -1     # At/above upper band → expect pullback DOWN
    else:
        snap.bb_signal = 0

    # ── Consensus ──
    snap.consensus_score = snap.rsi_signal + snap.ema_signal + snap.macd_signal + snap.bb_signal
    if snap.consensus_score > 0:
        snap.predicted_direction = "UP"
    elif snap.consensus_score < 0:
        snap.predicted_direction = "DOWN"
    else:
        snap.predicted_direction = ""

    return snap


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
                # Indicator columns
                "rsi", "ema_signal", "macd_signal", "bb_signal",
                "consensus_score", "confidence",
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
            # Indicator columns
            f"{trade.rsi_value:.1f}",
            trade.ema_signal,
            trade.macd_signal_val,
            trade.bb_signal_val,
            trade.consensus_score,
            trade.confidence,
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


def signal_icon(val: int) -> str:
    if val > 0:
        return f"{GREEN}▲ UP{R}"
    elif val < 0:
        return f"{RED}▼ DN{R}"
    return f"{D}── NT{R}"


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
        print(f"     Indicators: RSI={trade.rsi_value:.1f}  EMA={signal_icon(trade.ema_signal)}  "
              f"MACD={signal_icon(trade.macd_signal_val)}  BB={signal_icon(trade.bb_signal_val)}  "
              f"Consensus={trade.consensus_score:+d} ({trade.confidence})")
    else:
        reason = trade.decision.replace("SKIP_", "")
        print(f"  ⏭️  {YELLOW}Candle #{trade.candle_num} — SKIPPED ({reason}){R}")
        print(f"     BTC: ${trade.open_price:,.2f} → ${trade.close_price:,.2f}")
        if trade.consensus_score != 0:
            print(f"     Indicators: consensus={trade.consensus_score:+d} "
                  f"(would have predicted {trade.consensus_score > 0 and 'UP' or 'DOWN'})")


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
    print(f"  │ Wins:       {GREEN}{stats.wins}{R}{'':{5 - len(str(stats.wins))}}  "
          f"│ Losses:    {RED}{stats.losses}{R}{'':{5 - len(str(stats.losses))}}  "
          f"│ Win Rate: {B}{stats.win_rate:.1f}%{R}")
    print(f"  │ Total P&L:  {pnl_c}{pnl_sign}${stats.total_pnl:.4f}{R}{'':1}  "
          f"│ ROI:       {pnl_c}{pnl_sign}{stats.roi:.1f}%{R}{'':1}  "
          f"│ Avg P&L: ${stats.avg_pnl:.4f}")
    print(f"  │ Wagered:    ${stats.total_wagered:.2f}{'':1}  "
          f"│ Best:      {GREEN}+${stats.best_trade:.4f}{R}{'':1}  "
          f"│ Worst: {RED}${stats.worst_trade:.4f}{R}")
    print(f"  │ Streak:     {streak_c}{stats.current_streak:+d}{R}{'':{4}}  "
          f"│ Peak:      {GREEN}${stats.peak_balance:.4f}{R}{'':1}  "
          f"│ Valley: {RED}${stats.valley_balance:.4f}{R}")
    shadow_total = stats.shadow_wins + stats.shadow_losses
    if shadow_total > 0:
        shadow_wr = stats.shadow_wins / shadow_total * 100
        print(f"  │ {D}Shadow:     {stats.shadow_wins}W/{stats.shadow_losses}L "
              f"({shadow_wr:.0f}% of skips would have won){R}")
    print(f"  {B}{'─' * 58}{R}")


def print_waiting(market: dict, remaining: float, candle_open: float, stats: Stats,
                  indicators: IndicatorSnapshot | None = None):
    """Print the live waiting screen while a candle is running."""
    now = datetime.now(timezone.utc)
    event_start = market["event_start"]
    event_end = market["event_end"]
    to_start = (event_start - now).total_seconds()
    elapsed = 300 - remaining if remaining > 0 else 300

    delta = btc_price - candle_open if candle_open > 0 else 0
    delta_sign = "+" if delta >= 0 else ""
    dir_c = GREEN if delta >= 0 else RED
    direction = "UP 📈" if delta >= 0 else "DOWN 📉"

    # Status based on elapsed time
    if to_start > 0:
        verdict = f"{CYAN}Candle starts in {format_countdown(to_start)}{R}"
    elif elapsed < INDICATOR_COMPUTE_DELAY:
        verdict = f"{CYAN}🔬 Computing indicators...{R}"
    elif elapsed < EXECUTION_WINDOW_START:
        verdict = f"{YELLOW}📊 Indicators ready — executing in {int(EXECUTION_WINDOW_START - elapsed)}s{R}"
    elif elapsed < EXECUTION_WINDOW_END:
        verdict = f"{RED}{B}🔥 EXECUTION WINDOW{R}"
    else:
        verdict = f"{D}Monitoring — waiting for candle close{R}"

    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    effective = min(stats.balance, STARTING_BALANCE)
    bet_size = min(MAX_TRADE_USD, effective)

    clear()
    print(f"{B}{'═' * 64}{R}")
    print(f"{B}  📈 POLYSNIPER — INDICATOR PAPER TRADING ($10 No-Reinvest){R}")
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
        cd_c = f"{RED}{B}" if elapsed < EXECUTION_WINDOW_END else B
        print(f"  ⏱️  Remaining: {cd_c}{cd}{R}  [{bar}]")

    print()
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

    # ── Show Indicators ──
    if indicators and indicators.rsi > 0:
        print()
        print(f"  {B}{'─' * 64}{R}")
        print(f"  {B}📊 INDICATORS{R}")
        print(f"  {B}{'─' * 64}{R}")
        print(f"  │ RSI(14):    {indicators.rsi:>6.1f}  {signal_icon(indicators.rsi_signal)}")
        print(f"  │ EMA(9/21):  {indicators.ema_fast:>10,.2f} / {indicators.ema_slow:>10,.2f}  "
              f"{signal_icon(indicators.ema_signal)}")
        print(f"  │ MACD:       {indicators.macd_line:>8.2f} vs {indicators.macd_signal_line:>8.2f}  "
              f"{signal_icon(indicators.macd_signal)}")
        print(f"  │ BB Pos:     {indicators.bb_position:>6.2f}  "
              f"(${indicators.bb_lower:,.0f} — ${indicators.bb_upper:,.0f})  "
              f"{signal_icon(indicators.bb_signal)}")
        print(f"  │")
        cons = indicators.consensus_score
        cons_c = GREEN if cons > 0 else (RED if cons < 0 else D)
        cons_dir = "UP" if cons > 0 else ("DOWN" if cons < 0 else "NEUTRAL")
        abs_cons = abs(cons)
        conf = "HIGH" if abs_cons >= 4 else ("MEDIUM" if abs_cons >= 3 else ("LOW" if abs_cons >= 2 else "SKIP"))
        print(f"  │ Consensus:  {cons_c}{B}{cons:+d} → {cons_dir} ({conf}){R}")
        print(f"  {B}{'─' * 64}{R}")

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
            print(f"  │ Indicators: RSI={lt.rsi_value:.1f}  consensus={lt.consensus_score:+d}")
        else:
            reason = lt.decision.replace("SKIP_", "")
            print(f"  │ Decision:   {YELLOW}SKIPPED — {reason}{R}")
            print(f"  │ BTC Move:   ${lt.open_price:,.2f} → ${lt.close_price:,.2f} "
                  f"({'+' if lt.delta >= 0 else ''}${lt.delta:,.2f})")

        print(f"  {B}{'─' * 64}{R}")

    print()
    print(f"  {D}Ctrl+C to stop and see final report{R}")
    print(f"{B}{'═' * 64}{R}")


# ─────────────────────────────────────────────────────────────
# BUILD SCREEN (for decoupled UI renderer)
# ─────────────────────────────────────────────────────────────

def build_screen(state: dict) -> str:
    """Build the entire indicator dashboard into a single string for the UI renderer."""
    import io as _io
    buf = _io.StringIO()
    w = buf.write

    market = state.get("market")
    remaining = state.get("remaining", 0)
    candle_open = state.get("candle_open", 0)
    stats = state.get("stats")
    indicators = state.get("indicators")

    if not market or not stats:
        return ""

    now = datetime.now(timezone.utc)
    event_start = market["event_start"]
    to_start = (event_start - now).total_seconds()
    elapsed = 300 - remaining if remaining > 0 else 300

    delta = btc_price - candle_open if candle_open > 0 else 0
    delta_sign = "+" if delta >= 0 else ""
    dir_c = GREEN if delta >= 0 else RED
    direction = "UP 📈" if delta >= 0 else "DOWN 📉"

    if to_start > 0:
        verdict = f"{CYAN}Candle starts in {format_countdown(to_start)}{R}"
    elif elapsed < INDICATOR_COMPUTE_DELAY:
        verdict = f"{CYAN}🔬 Computing indicators...{R}"
    elif elapsed < EXECUTION_WINDOW_START:
        verdict = f"{YELLOW}📊 Indicators ready — executing in {int(EXECUTION_WINDOW_START - elapsed)}s{R}"
    elif elapsed < EXECUTION_WINDOW_END:
        verdict = f"{RED}{B}🔥 EXECUTION WINDOW{R}"
    else:
        verdict = f"{D}Monitoring — waiting for candle close{R}"

    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    effective = min(stats.balance, STARTING_BALANCE)
    bet_size = min(MAX_TRADE_USD, effective)

    w(f"{B}{'═' * 64}{R}\n")
    w(f"{B}  📈 POLYSNIPER — INDICATOR PAPER TRADING ($10 No-Reinvest){R}\n")
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
        cd_c = f"{RED}{B}" if elapsed < EXECUTION_WINDOW_END else B
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

    # Indicators panel
    if indicators and indicators.rsi > 0:
        w("\n")
        w(f"  {B}{'─' * 64}{R}\n")
        w(f"  {B}📊 INDICATORS{R}\n")
        w(f"  {B}{'─' * 64}{R}\n")
        w(f"  │ RSI(14): {indicators.rsi:>6.1f}  {signal_icon(indicators.rsi_signal)}\n")
        w(f"  │ EMA(9/21): {signal_icon(indicators.ema_signal)}  "
          f"MACD: {signal_icon(indicators.macd_signal)}  "
          f"BB: {signal_icon(indicators.bb_signal)}\n")
        cons = indicators.consensus_score
        cons_c = GREEN if cons > 0 else (RED if cons < 0 else D)
        cons_dir = "UP" if cons > 0 else ("DOWN" if cons < 0 else "NEUTRAL")
        abs_cons = abs(cons)
        conf = "HIGH" if abs_cons >= 4 else ("MEDIUM" if abs_cons >= 3 else ("LOW" if abs_cons >= 2 else "SKIP"))
        w(f"  │ Consensus: {cons_c}{B}{cons:+d} → {cons_dir} ({conf}){R}\n")
        w(f"  {B}{'─' * 64}{R}\n")

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
            pnl_sign = "+" if lt.pnl >= 0 else ""
            w(f"  │ Result: {result_c}{B}{'✅ WON' if lt.won else '❌ LOST'}{R}\n")
            w(f"  │ Bet: ${lt.bet_amount:.2f}  │  "
              f"P&L: {result_c}{pnl_sign}${lt.pnl:.4f}{R}\n")
        else:
            reason = lt.decision.replace("SKIP_", "")
            w(f"  │ Decision: {YELLOW}SKIPPED — {reason}{R}\n")
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
) -> Trade:
    """
    Simulate one candle using indicator-based prediction.

    1. Wait for candle to start
    2. At T+5s: fetch klines, compute indicators
    3. At T+15s: fetch orderbook, execute trade
    4. Wait for candle close, determine result
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

    # Context tracking
    ask_size = 0.0
    book_depth_usd = 0.0
    opposing_ask_price = 0.0
    spread = 0.0
    btc_high = 0.0
    btc_low = float('inf')
    decision_btc_price = 0.0
    decision_remaining = 0.0
    confidence = ""
    indicators = IndicatorSnapshot()

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
                  stats=stats, indicators=None)
        await asyncio.sleep(1)

    # Record open price
    candle_open = btc_price
    btc_high = btc_price
    btc_low = btc_price

    # ── Phase 1: Compute indicators at T+5s ──
    indicator_computed = False

    # ── Main candle loop ──
    while True:
        now = datetime.now(timezone.utc)
        remaining = (event_end - now).total_seconds()
        elapsed = 300 - remaining

        if remaining <= 0:
            break

        # Track high/low
        if btc_price > 0:
            btc_high = max(btc_high, btc_price)
            btc_low = min(btc_low, btc_price)

        # Update UI state (rendered by background task)
        update_ui(market=market, remaining=remaining, candle_open=candle_open,
                  stats=stats,
                  indicators=indicators if indicator_computed else None)

        # ── Compute indicators once, early in the candle ──
        if elapsed >= INDICATOR_COMPUTE_DELAY and not indicator_computed:
            # Check price staleness first
            stale_age = time.time() - btc_price_updated if btc_price_updated > 0 else float('inf')
            if stale_age > PRICE_STALE_RECONNECT:
                decision = "SKIP_STALE_PRICE"
                order_simulated = True
            else:
                klines = await fetch_klines(session)
                if len(klines) < 30:
                    decision = "SKIP_NO_DATA"
                    order_simulated = True
                else:
                    indicators = indicator_consensus(klines)
            indicator_computed = True

        # ── Execute trade in the execution window ──
        if (elapsed >= EXECUTION_WINDOW_START and not order_simulated
                and indicator_computed and indicators.predicted_direction):

            decision_btc_price = btc_price
            decision_remaining = remaining
            abs_consensus = abs(indicators.consensus_score)

            if abs_consensus < MIN_CONSENSUS:
                decision = "SKIP_NO_CONSENSUS"
                order_simulated = True
            elif stats.balance <= 0.001:
                decision = "SKIP_BROKE"
                order_simulated = True
            else:
                predicted = indicators.predicted_direction
                if predicted == "UP":
                    target_token = token_up
                    opposing_token = token_down
                else:
                    target_token = token_down
                    opposing_token = token_up

                # Fetch orderbooks
                ob, opp_ob = await asyncio.gather(
                    fetch_orderbook(session, target_token),
                    fetch_orderbook(session, opposing_token),
                )
                ask_price = ob["ask_price"]
                ask_size = ob["ask_size"]
                book_depth_usd = ob["depth_usd"]
                spread = ob["spread"]
                opposing_ask_price = opp_ob["ask_price"]

                # Determine confidence from consensus strength
                if abs_consensus >= 4:
                    confidence = "HIGH"
                elif abs_consensus >= 3:
                    confidence = "MEDIUM"
                else:
                    confidence = "LOW"

                # Apply filters
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
                    decision = f"BUY_{predicted}"
                    side_bought = predicted
                    buy_price = ask_price
                    order_simulated = True

        # If we passed the execution window without deciding (no prediction)
        if elapsed > EXECUTION_WINDOW_END and not order_simulated:
            if not indicators.predicted_direction:
                decision = "SKIP_NO_CONSENSUS"
            else:
                decision = "SKIP_RISKY"
            order_simulated = True

        await adaptive_sleep_early(elapsed, INDICATOR_COMPUTE_DELAY, EXECUTION_WINDOW_END)

    # ── Candle closed — cleanup WS streams ──
    ws_stop.set()
    ws_up_task.cancel()
    ws_dn_task.cancel()

    # ── Determine actual result ──
    close_price = btc_price
    delta = close_price - candle_open
    actual_direction = "UP" if delta >= 0 else "DOWN"

    if not order_simulated:
        decision = "SKIP_RISKY"

    # ── Calculate P&L ──
    won = False
    pnl = 0.0
    pnl_pct = 0.0
    bet_amount = 0.0

    if decision.startswith("BUY"):
        effective_bal = min(stats.balance, STARTING_BALANCE)
        sizing_mult = CONFIDENCE_SIZING.get(confidence, 1.0)
        bet_amount = min(MAX_TRADE_USD * sizing_mult, effective_bal)
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
        ask_size=ask_size,
        book_depth_usd=book_depth_usd,
        opposing_ask_price=opposing_ask_price,
        spread=spread,
        btc_high=btc_high,
        btc_low=btc_low if btc_low < float('inf') else 0.0,
        btc_volatility=btc_volatility,
        decision_btc_price=decision_btc_price,
        decision_remaining=decision_remaining,
        # Indicator fields
        rsi_value=indicators.rsi,
        ema_signal=indicators.ema_signal,
        macd_signal_val=indicators.macd_signal,
        bb_signal_val=indicators.bb_signal,
        consensus_score=indicators.consensus_score,
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
    print(f"{B}  📋 INDICATOR PAPER TRADING — FINAL REPORT{R}")
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
        print(f"  {'#':>3}  {'Direction':>9}  {'Decision':>18}  {'Buy @':>8}  "
              f"{'P&L':>10}  {'Balance':>10}  {'Cons':>5}  {'Result':>6}")
        print(f"  {'─' * 80}")
        for t in stats.trades:
            if t.decision.startswith("BUY"):
                result_icon = f"{GREEN}WIN{R}" if t.won else f"{RED}LOSS{R}"
                sign = "+" if t.pnl >= 0 else ""
                c = GREEN if t.won else RED
                bc = GREEN if t.balance_after >= STARTING_BALANCE else RED
                print(f"  {t.candle_num:>3}  {t.actual_direction:>9}  "
                      f"{t.decision:>18}  ${t.buy_price:>6.4f}  "
                      f"{c}{sign}${t.pnl:>8.4f}{R}  "
                      f"{bc}${t.balance_after:>8.4f}{R}  "
                      f"{t.consensus_score:>+5d}  {result_icon}")
            else:
                print(f"  {t.candle_num:>3}  {t.actual_direction:>9}  "
                      f"{YELLOW}{t.decision:>18}{R}  {'—':>8}  "
                      f"{'—':>10}  {'—':>10}  "
                      f"{t.consensus_score:>+5d}  {D}SKIP{R}")

    print()
    print(f"  {D}Full log saved to: {CSV_FILE}{R}")
    print(f"{B}{'═' * 64}{R}")
    print()


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main(max_candles: int | None = None):
    """Run indicator-based paper trader."""
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
    print(f"💰 Starting balance: ${STARTING_BALANCE:.2f} (no-reinvest)")
    print(f"📊 Strategy: RSI + EMA + MACD + Bollinger Bands consensus")
    print(f"⏱️  Entry: T+{EXECUTION_WINDOW_START}s to T+{EXECUTION_WINDOW_END}s")
    print()

    init_csv(CSV_FILE)
    stats = Stats()
    candle_num = 0

    try:
        while True:
            if max_candles and candle_num >= max_candles:
                break

            if stats.balance <= 0.001:
                print(f"\n  {RED}{B}💀 BANKRUPT — balance is $0.00. Ending session.{R}\n")
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

            trade = await simulate_candle(session, market, candle_num, stats)

            stats.record(trade)
            log_trade_csv(CSV_FILE, trade, stats)

            # ── Check drawdown ──
            if (stats.peak_balance > 0 and
                    stats.balance < stats.peak_balance * DRAWDOWN_THRESHOLD):
                stats.cooldown_remaining = DRAWDOWN_COOLDOWN
                print(f"\n  {RED}🛑 DRAWDOWN BREAKER: balance ${stats.balance:.4f} < "
                      f"50% of peak ${stats.peak_balance:.4f} — pausing {DRAWDOWN_COOLDOWN} candles{R}")

            # Show result
            clear()
            print(f"\n{B}{'═' * 64}{R}")
            print(f"{B}  📈 CANDLE #{candle_num} RESULT{R}")
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
    parser = argparse.ArgumentParser(description="PolySniper Indicator Paper Trader")
    parser.add_argument(
        "--candles", "-n",
        type=int,
        default=None,
        help="Number of candles to simulate (default: run until Ctrl+C)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(max_candles=args.candles))
    except KeyboardInterrupt:
        print(f"\n👋 Goodbye!")
