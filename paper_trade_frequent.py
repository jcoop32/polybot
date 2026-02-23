#!/usr/bin/env python3
"""
Frequent Momentum Paper Trader — Trade at T+15s based on micro-momentum.

Unlike the sniper bots that wait until the last 15 seconds of a candle,
this bot trades at the START of each candle. At exactly T+15s after the
candle opens, it reads the BTC micro-delta and forcefully buys the
corresponding UP or DOWN token.

No safe buffers. No confidence sizing. Maximally aggressive.

Results are displayed live in the terminal and logged to
paper_trades_frequent.csv for later analysis.

Usage:
    python paper_trade_frequent.py                  # Run until Ctrl+C
    python paper_trade_frequent.py --candles 20     # Run for 20 candles
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from perf import (
    json_loads, create_session,
    orderbook_ws_stream, fetch_orderbook_rest, get_cached_orderbook,
    update_ui, ui_renderer,
    calc_taker_fee,
)


# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

STARTING_BALANCE = 10.00       # Starting paper bankroll
MAX_BUY_PRICE = 0.60           # Willing to pay more (early candle prices ~0.50-0.55)
MIN_BUY_PRICE = 0.10           # Don't buy below this (phantom liquidity)
MAX_TRADE_USD = 5.0            # Simulated trade size per candle
MIN_ASK_SIZE_USD = 2.0         # Lowered to ensure frequent fills
DECISION_SECOND = 15           # Make the decision at T+15s into the 300s candle

# Circuit breaker
CIRCUIT_BREAKER_LOSSES = 3     # Consecutive losses to trigger cooldown
CIRCUIT_BREAKER_COOLDOWN = 1800  # 30 minute cooldown (seconds)

# BTC price staleness
PRICE_STALE_SECONDS = 10
PRICE_STALE_RECONNECT = 30

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"
COINCAP_URL = "https://api.coincap.io/v2/assets/bitcoin"

CSV_FILE = "csv_logs/paper_trades_frequent_v3.csv"


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
    """Record of a single simulated trade."""
    candle_num: int
    slug: str
    title: str
    open_price: float           # BTC price at candle open
    close_price: float          # BTC price at candle close
    delta: float                # close - open
    actual_direction: str       # "UP" or "DOWN"
    decision: str               # "BUY_UP", "BUY_DOWN", "SKIP_*"
    side_bought: str            # "UP", "DOWN", or ""
    buy_price: float            # Ask price we would have paid
    bet_amount: float           # USDC wagered
    won: bool
    pnl: float                  # Profit/loss for this trade
    pnl_pct: float              # P&L as percentage
    taker_fee: float = 0.0             # Dynamic taker fee deducted
    balance_after: float = 0.0
    decision_btc_price: float = 0.0    # BTC price at moment of decision
    decision_delta: float = 0.0        # Delta at moment of decision (T+15s)


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
        global circuit_breaker_active, circuit_breaker_until
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
                # Win clears circuit breaker
                if circuit_breaker_active:
                    circuit_breaker_active = False
                    circuit_breaker_until = 0.0
            else:
                self.losses += 1
                self.current_streak = min(0, self.current_streak) - 1
                self.longest_loss_streak = max(self.longest_loss_streak, abs(self.current_streak))
                # Check circuit breaker trigger
                if abs(self.current_streak) >= CIRCUIT_BREAKER_LOSSES:
                    circuit_breaker_active = True
                    circuit_breaker_until = time.time() + CIRCUIT_BREAKER_COOLDOWN

            self.best_trade = max(self.best_trade, trade.pnl)
            self.worst_trade = min(self.worst_trade, trade.pnl)
        else:
            self.total_skips += 1


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

        # HTTP fallback
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
                "btc_open", "btc_close", "btc_delta",
                "actual_direction", "decision", "side_bought",
                "buy_price", "bet_amount", "won", "taker_fee",
                "pnl", "pnl_pct",
                "cumulative_pnl", "balance", "win_rate",
                "decision_btc_price", "decision_delta",
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
            f"{trade.open_price:.2f}",
            f"{trade.close_price:.2f}",
            f"{trade.delta:.2f}",
            trade.actual_direction,
            trade.decision,
            trade.side_bought,
            f"{trade.buy_price:.4f}",
            f"{trade.bet_amount:.2f}",
            trade.won,
            f"{trade.taker_fee:.4f}",
            f"{trade.pnl:.4f}",
            f"{trade.pnl_pct:.2f}",
            f"{stats.total_pnl:.4f}",
            f"{stats.balance:.4f}",
            f"{stats.win_rate:.1f}",
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
    """Format elapsed time since bot start as Xh Ym Zs."""
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

    market = state.get("market")
    remaining = state.get("remaining", 0)
    elapsed = state.get("elapsed", 0)
    candle_open = state.get("candle_open", 0)
    stats = state.get("stats")
    phase = state.get("phase", "")

    if not market or not stats:
        return ""

    now = datetime.now(timezone.utc)
    event_start = market["event_start"]
    to_start = (event_start - now).total_seconds()

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
    w(f"  {B}│{R}  ⚡ {B}FREQUENT MOMENTUM TRADER{R}"
      f"           {D}Paper Trading{R}   {B}│{R}\n")
    w(f"  {B}│{R}  {D}{market['title']:<60}{R}{B}│{R}\n")
    w(f"  {B}├{'─' * 62}┤{R}\n")
    uptime_str = format_uptime(bot_start_time)
    w(f"  {B}│{R}  💰 {bal_c}{B}${stats.balance:.4f}{R}"
      f"     Bet: ${bet_size:.2f}"
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
        filled = max(0, int((elapsed / 300) * bar_len))
        bar = f"{CYAN}{'━' * filled}{R}{D}{'╌' * (bar_len - filled)}{R}"
        w(f"\n  [{bar}] {B}{format_countdown(remaining)}{R} {D}T+{int(elapsed)}s{R}\n")

    # ── Phase ──
    if phase == "waiting":
        phase_str = f"{CYAN}⏳ Waiting{R}"
    elif phase == "measuring":
        secs_left = max(0, DECISION_SECOND - elapsed)
        phase_str = f"{MAGENTA}🔬 Measuring ({int(secs_left)}s to decision){R}"
    elif phase == "monitoring":
        decision_label = state.get("decision", "")
        if decision_label.startswith("BUY"):
            side = decision_label.replace("BUY_", "")
            phase_str = f"{GREEN}📡 Bought {side} — monitoring{R}"
        else:
            phase_str = f"{YELLOW}🚫 Skipped — monitoring{R}"
    else:
        phase_str = f"{D}—{R}"
    w(f"  {phase_str}")
    if circuit_breaker_active:
        cb_remaining = max(0, circuit_breaker_until - time.time())
        w(f"  {RED}🔌 Cooldown {int(cb_remaining)}s{R}")
    w("\n")

    # ── Stats Grid ──
    if stats.total_candles > 0:
        wr = stats.win_rate
        wr_c = GREEN if wr >= 50 else YELLOW
        w(f"\n  {D}{'─' * 62}{R}\n")
        w(f"  Trades {B}{stats.total_trades}{R}"
          f"  │  W{GREEN} {stats.wins}{R} L{RED} {stats.losses}{R}"
          f"  │  WR {wr_c}{B}{wr:.0f}%{R}"
          f"  │  P&L {pnl_c}{B}{pnl_s}${stats.total_pnl:.4f}{R}\n")
        w(f"  {D}{'─' * 62}{R}\n")

    # ── Last Trade ──
    if stats.trades:
        lt = stats.trades[-1]

        if lt.decision.startswith("BUY"):
            result_c = GREEN if lt.won else RED
            result_tag = f"{result_c}{B}{'WIN' if lt.won else 'LOSS'}{R}"
            pnl_sign = "+" if lt.pnl >= 0 else ""
            w(f"\n  {D}Last #{lt.candle_num}{R}  {result_tag}  "
              f"Bought {B}{lt.side_bought}{R}@${lt.buy_price:.2f}"
              f"  →  {lt.actual_direction}"
              f"  {result_c}{pnl_sign}${lt.pnl:.4f}{R} "
              f"{D}({pnl_sign}{lt.pnl_pct:.1f}%){R}\n")
            w(f"  {D}${lt.bet_amount:.2f} bet  "
              f"Fee ${lt.taker_fee:.4f}  "
              f"BTC ${lt.open_price:,.0f}→${lt.close_price:,.0f} "
              f"({'+' if lt.delta >= 0 else ''}${lt.delta:,.0f})  "
              f"@T+{DECISION_SECOND}s Δ{'+' if lt.decision_delta >= 0 else ''}"
              f"${lt.decision_delta:,.0f}{R}\n")
        else:
            reason = lt.decision.replace("SKIP_", "")
            w(f"\n  {D}Last #{lt.candle_num}  {YELLOW}SKIP{R} {D}({reason})"
              f"  BTC ${lt.open_price:,.0f}→${lt.close_price:,.0f}{R}\n")

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
    Run through one full 5-minute candle with early momentum logic.

    - Waits for candle to open
    - Records candle open price
    - At T+15s, reads micro-delta and buys UP or DOWN
    - Waits until candle closes to determine actual winner
    - Calculates P&L

    Returns a Trade with full details.
    """
    global circuit_breaker_active, circuit_breaker_until
    event_start = market["event_start"]
    event_end = market["event_end"]
    token_up = market["token_up_id"]
    token_down = market["token_down_id"]

    candle_open = 0.0
    decision = ""
    side_bought = ""
    buy_price = 0.0
    bet_amount = 0.0
    order_simulated = False
    decision_btc_price = 0.0
    decision_delta = 0.0

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
        update_ui(market=market, remaining=300, elapsed=0,
                  candle_open=0, stats=stats, phase="waiting")
        await asyncio.sleep(min(1.0, max(0.1, to_start)))

    # Record open price
    candle_open = btc_price

    # ── Main candle loop ──
    while True:
        now = datetime.now(timezone.utc)
        remaining = (event_end - now).total_seconds()
        elapsed = (now - event_start).total_seconds()

        if remaining <= 0:
            break

        # Determine phase for UI
        if not order_simulated and elapsed < DECISION_SECOND:
            phase = "measuring"
        elif order_simulated:
            phase = "monitoring"
        else:
            phase = "measuring"

        # Update UI state
        update_ui(
            market=market, remaining=remaining, elapsed=elapsed,
            candle_open=candle_open, stats=stats, phase=phase,
            decision=decision,
        )

        # ── Decision at T+DECISION_SECOND ──
        if elapsed >= DECISION_SECOND and not order_simulated:
            decision_btc_price = btc_price
            decision_delta = btc_price - candle_open

            # Check price staleness
            stale_age = (time.time() - btc_price_updated
                         if btc_price_updated > 0 else float('inf'))
            if stale_age > PRICE_STALE_RECONNECT:
                decision = "SKIP_STALE_PRICE"
                order_simulated = True
            elif stats.balance < 0.001:
                decision = "SKIP_NO_BALANCE"
                order_simulated = True
            elif circuit_breaker_active and time.time() < circuit_breaker_until:
                decision = "SKIP_COOLDOWN"
                order_simulated = True
            else:
                # Reset circuit breaker if cooldown expired
                if circuit_breaker_active:
                    circuit_breaker_active = False
                    circuit_breaker_until = 0.0

                if decision_delta == 0:
                    decision = "SKIP_FLAT"
                    order_simulated = True
                else:
                    # Determine direction
                    if decision_delta > 0:
                        target_side = "UP"
                        target_token = token_up
                    else:
                        target_side = "DOWN"
                        target_token = token_down

                    # Fetch orderbook for target
                    ob = await fetch_orderbook(session, target_token)
                    ask_price = ob["ask_price"]
                    ask_size = ob["ask_size"]

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
                    else:
                        decision = f"BUY_{target_side}"
                        side_bought = target_side
                        buy_price = ask_price
                        order_simulated = True

            # Update phase after decision
            update_ui(
                market=market, remaining=remaining, elapsed=elapsed,
                candle_open=candle_open, stats=stats, phase="monitoring",
                decision=decision,
            )

        # Adaptive sleep: fast during decision window, slow otherwise
        if not order_simulated and abs(elapsed - DECISION_SECOND) < 3:
            await asyncio.sleep(0.05)   # 20 Hz near decision point
        elif not order_simulated and elapsed < DECISION_SECOND:
            await asyncio.sleep(0.5)    # 2 Hz while measuring
        else:
            await asyncio.sleep(2.0)    # 0.5 Hz while monitoring

    # ── Candle closed — cleanup WS streams ──
    ws_stop.set()
    ws_up_task.cancel()
    ws_dn_task.cancel()

    # ── Determine actual result ──
    close_price = btc_price
    delta = close_price - candle_open
    actual_direction = "UP" if delta >= 0 else "DOWN"

    # If we never entered the decision window (joined very late)
    if not order_simulated:
        decision = "SKIP_LATE_JOIN"

    # ── Calculate P&L ──
    won = False
    pnl = 0.0
    pnl_pct = 0.0
    taker_fee = 0.0

    if decision.startswith("BUY"):
        bet_amount = min(MAX_TRADE_USD, stats.balance)
        shares = bet_amount / buy_price if buy_price > 0 else 0

        # Dynamic taker fee based on buy price (= probability)
        fee_rate = calc_taker_fee(buy_price)
        taker_fee = bet_amount * fee_rate

        if side_bought == actual_direction:
            payout = shares * 1.0
            pnl = payout - bet_amount - taker_fee
            pnl_pct = (pnl / bet_amount) * 100
            won = True
        else:
            pnl = -bet_amount - taker_fee
            pnl_pct = ((pnl) / bet_amount) * 100
            won = False

    balance_after = stats.balance + pnl

    trade = Trade(
        candle_num=candle_num,
        slug=market["slug"],
        title=market["title"],
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
        taker_fee=taker_fee,
        balance_after=balance_after,
        decision_btc_price=decision_btc_price,
        decision_delta=decision_delta,
    )

    return trade


# ─────────────────────────────────────────────────────────────
# FINAL REPORT
# ─────────────────────────────────────────────────────────────

def print_final_report(stats: Stats):
    """Print comprehensive final report after all candles."""
    print("\033[2J\033[H", end="", flush=True)
    pnl_c = GREEN if stats.total_pnl >= 0 else RED
    pnl_s = "+" if stats.total_pnl >= 0 else ""
    bal_c = GREEN if stats.balance >= STARTING_BALANCE else RED
    bal_s = "+" if stats.balance_change_pct >= 0 else ""

    print()
    print(f"{B}{'═' * 64}{R}")
    print(f"{B}  ⚡ FREQUENT MOMENTUM TRADER — FINAL REPORT{R}")
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
    freq = (stats.total_trades / stats.total_candles * 100) if stats.total_candles > 0 else 0
    print(f"  └─ Trade frequency:    {freq:.0f}%")
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

async def main(max_candles: int | None = None):
    """Run frequent momentum paper trader across multiple candles."""
    global bot_start_time
    bot_start_time = time.time()
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
    print(f"🎯 Decision at T+{DECISION_SECOND}s into each candle\n")

    init_csv(CSV_FILE)
    stats = Stats()
    candle_num = 0

    try:
        while True:
            if max_candles and candle_num >= max_candles:
                break

            market = await discover_market(session)
            if market is None:
                print("⚠️  No market found. Retrying in 15s...")
                await asyncio.sleep(15)
                continue

            candle_num += 1

            trade = await simulate_candle(session, market, candle_num, stats)

            stats.record(trade)
            log_trade_csv(CSV_FILE, trade, stats)

            # Brief pause between candles
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
    parser = argparse.ArgumentParser(
        description="Frequent Momentum Paper Trader — T+15s micro-delta strategy"
    )
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
