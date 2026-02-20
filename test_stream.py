#!/usr/bin/env python3
"""
PolySniper Test — Live Data Stream for BTC Up/Down 5-Minute Markets.

Streams live data from:
  1. Binance WS    → real-time BTC/USDT price
  2. Gamma API     → discovers next BTC 5-min Up/Down market by slug
  3. CLOB REST API → orderbook (best bid/ask) for Up and Down tokens

No wallet needed. No geo restrictions on read-only APIs. Run from anywhere.

Usage:
    pip install websockets aiohttp
    python test_stream.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import websockets

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
COINCAP_URL = "https://api.coincap.io/v2/assets/bitcoin"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd"

REFRESH_INTERVAL = 1.0

# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

btc_price: float = 0.0


# ─────────────────────────────────────────────────────────────
# BTC PRICE POLLER — CoinGecko REST API
# ─────────────────────────────────────────────────────────────

async def btc_price_stream():
    """Poll BTC/USD price. CoinCap primary, CoinGecko fallback."""
    global btc_price

    async with aiohttp.ClientSession() as session:
        while True:
            fetched = False
            try:
                async with session.get(COINCAP_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        btc_price = float(data["data"]["priceUsd"])
                        fetched = True
            except Exception:
                pass

            if not fetched:
                try:
                    async with session.get(COINGECKO_URL, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            btc_price = float(data["bitcoin"]["usd"])
                except Exception as e:
                    print(f"❌ Price fetch error: {e}")

            await asyncio.sleep(2)


# ─────────────────────────────────────────────────────────────
# MARKET DISCOVERY — Slug-Based
# ─────────────────────────────────────────────────────────────

def compute_next_candle_slug() -> tuple[str, int, int]:
    """
    Compute the slug for the next (or current) BTC 5-min candle.

    Slug pattern: btc-updown-5m-{candle_end_unix}
    Candles are 300s (5 min) aligned to the Unix epoch.

    Returns (slug, candle_start_ts, candle_end_ts)
    """
    now_ts = time.time()
    candle_end = math.ceil(now_ts / 300) * 300
    candle_start = candle_end - 300

    # If we're past the end, move to next candle
    if now_ts >= candle_end:
        candle_start = candle_end
        candle_end += 300

    slug = f"btc-updown-5m-{candle_end}"
    return slug, candle_start, candle_end


async def discover_market(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Find the current or next BTC Up/Down 5-minute market.
    Uses direct slug lookup — fast and precise.
    """
    now_ts = time.time()

    # Try current candle, then next candle
    for offset in [0, 300]:
        candle_end = math.ceil(now_ts / 300) * 300 + offset
        candle_start = candle_end - 300
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

        # Parse times
        start_str = market.get("eventStartTime")
        end_str = market.get("endDate")
        if not start_str or not end_str:
            continue

        event_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        event_end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))

        # Skip if already expired
        if event_end <= datetime.now(timezone.utc):
            continue

        # Parse outcomes and tokens
        outcomes_str = market.get("outcomes", "[]")
        tokens_str = market.get("clobTokenIds", "[]")
        prices_str = market.get("outcomePrices", "[]")

        outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
        clob_tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str
        prices = json.loads(prices_str) if isinstance(prices_str, str) else prices_str

        if len(outcomes) < 2 or len(clob_tokens) < 2:
            continue

        # Map Up/Down to correct indices
        up_idx, down_idx = 0, 1
        for i, outcome in enumerate(outcomes):
            if outcome.lower() == "up":
                up_idx = i
            elif outcome.lower() == "down":
                down_idx = i

        return {
            "slug": slug,
            "title": event.get("title", ""),
            "condition_id": market.get("conditionId", ""),
            "event_start": event_start,
            "event_end": event_end,
            "token_up_id": clob_tokens[up_idx],
            "token_down_id": clob_tokens[down_idx],
            "up_price": float(prices[up_idx]) if prices else 0,
            "down_price": float(prices[down_idx]) if prices else 0,
            "best_bid": float(market.get("bestBid", 0)),
            "best_ask": float(market.get("bestAsk", 0)),
            "liquidity": float(market.get("liquidityNum", 0)),
            "volume": float(market.get("volumeNum", 0)),
        }

    return None


# ─────────────────────────────────────────────────────────────
# CLOB REST — Orderbook
# ─────────────────────────────────────────────────────────────

async def fetch_orderbook(session: aiohttp.ClientSession, token_id: str) -> dict:
    """Fetch orderbook for a token from the CLOB REST API."""
    try:
        url = f"{CLOB_HOST}/book"
        params = {"token_id": token_id}
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return {"bids": [], "asks": []}
            return await resp.json()
    except Exception:
        return {"bids": [], "asks": []}


def best_from_book(book: dict, side: str) -> tuple[float, float]:
    """Get best price and total size from orderbook side."""
    entries = book.get(side, [])
    if not entries:
        return 0.0, 0.0
    if side == "bids":
        best = max(entries, key=lambda e: float(e.get("price", 0)))
    else:
        best = min(entries, key=lambda e: float(e.get("price", 999)))
    return float(best.get("price", 0)), float(best.get("size", 0))


def total_depth(book: dict, side: str) -> float:
    """Sum total size across all levels for a side."""
    return sum(float(e.get("size", 0)) for e in book.get(side, []))


# ─────────────────────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────────────────────

def format_countdown(seconds: float) -> str:
    if seconds <= 0:
        return "EXPIRED"
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    if mins > 0:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


async def display_loop(session: aiohttp.ClientSession, market: dict):
    """Main display loop — live dashboard refreshed every second."""
    token_up = market["token_up_id"]
    token_down = market["token_down_id"]
    event_start = market["event_start"]
    event_end = market["event_end"]
    title = market["title"]
    candle_open_price: float | None = None

    while True:
        now = datetime.now(timezone.utc)
        remaining = (event_end - now).total_seconds()
        to_start = (event_start - now).total_seconds()

        # Market expired — exit to find next candle
        if remaining <= -5:
            return

        # Fetch both orderbooks in parallel
        book_up, book_down = await asyncio.gather(
            fetch_orderbook(session, token_up),
            fetch_orderbook(session, token_down),
        )

        up_bid, up_bid_sz = best_from_book(book_up, "bids")
        up_ask, up_ask_sz = best_from_book(book_up, "asks")
        down_bid, down_bid_sz = best_from_book(book_down, "bids")
        down_ask, down_ask_sz = best_from_book(book_down, "asks")

        up_depth = total_depth(book_up, "asks")
        down_depth = total_depth(book_down, "asks")

        # Track candle open price
        if candle_open_price is None and to_start <= 0 and btc_price > 0:
            candle_open_price = btc_price

        # Phase
        if to_start > 0:
            phase = "⏳ PRE-CANDLE"
            phase_c = "\033[33m"
        elif remaining > 15:
            phase = "📊 MONITORING"
            phase_c = "\033[36m"
        elif remaining > 0:
            phase = "🔥 EXECUTION WINDOW"
            phase_c = "\033[1;31m"
        else:
            phase = "⏰ SETTLING"
            phase_c = "\033[90m"

        # Direction
        if candle_open_price and btc_price > 0:
            delta = btc_price - candle_open_price
            if delta >= 0:
                direction = "UP 📈"
                dir_c = "\033[32m"
            else:
                direction = "DOWN 📉"
                dir_c = "\033[31m"
        else:
            delta = 0.0
            direction = "WAITING"
            dir_c = "\033[33m"

        # ── Render ──
        R = "\033[0m"
        B = "\033[1m"
        D = "\033[2m"

        clear_screen()
        print(f"{B}{'═' * 68}{R}")
        print(f"{B}  🎯 POLYSNIPER — LIVE DATA STREAM{R}")
        print(f"{B}{'═' * 68}{R}")
        print(f"  {D}{title}{R}")
        print(f"  ⏱️  {event_start.strftime('%H:%M:%S')} → {event_end.strftime('%H:%M:%S')} UTC")
        print()

        # Countdown bar
        if to_start > 0:
            print(f"  ⏳ Starts in: {B}{format_countdown(to_start)}{R}")
        else:
            bar_len = 30
            filled = max(0, int((remaining / 300) * bar_len))
            bar = "█" * filled + "░" * (bar_len - filled)
            countdown_str = format_countdown(remaining)
            if remaining <= 15:
                print(f"  ⏱️  Remaining: \033[1;31m{countdown_str}{R}  [{bar}]")
            else:
                print(f"  ⏱️  Remaining: {B}{countdown_str}{R}  [{bar}]")

        print(f"  🏷️  Phase:     {phase_c}{phase}{R}")
        print()

        # BTC
        print(f"  {B}₿ BITCOIN{R}")
        print(f"  ├─ Live Price:   {B}${btc_price:>12,.2f}{R}")
        if candle_open_price:
            sign = "+" if delta >= 0 else ""
            print(f"  ├─ Candle Open:  ${candle_open_price:>12,.2f}")
            print(f"  ├─ Delta:        {dir_c}{sign}${delta:>11,.2f}{R}")
            print(f"  └─ Direction:    {dir_c}{B}{direction}{R}")
        else:
            print(f"  └─ Candle Open:  {D}(waiting for candle start){R}")
        print()

        # UP orderbook
        up_mid = (up_bid + up_ask) / 2 if up_bid and up_ask else 0
        print(f"  {B}📈 UP{R}   Implied: {B}{up_mid * 100:.1f}%{R}   Depth: {up_depth:,.0f} shares")
        print(f"  ├─ Bid: ${up_bid:.4f} ({up_bid_sz:>10,.1f})")
        print(f"  ├─ Ask: ${up_ask:.4f} ({up_ask_sz:>10,.1f})")
        print(f"  └─ Spread: ${(up_ask - up_bid):.4f}")
        print()

        # DOWN orderbook
        down_mid = (down_bid + down_ask) / 2 if down_bid and down_ask else 0
        print(f"  {B}📉 DOWN{R} Implied: {B}{down_mid * 100:.1f}%{R}   Depth: {down_depth:,.0f} shares")
        print(f"  ├─ Bid: ${down_bid:.4f} ({down_bid_sz:>10,.1f})")
        print(f"  ├─ Ask: ${down_ask:.4f} ({down_ask_sz:>10,.1f})")
        print(f"  └─ Spread: ${(down_ask - down_bid):.4f}")
        print()

        # Sniper Analysis
        print(f"  {B}🎯 SNIPER ANALYSIS{R}")
        if remaining <= 15 and remaining > 0 and candle_open_price:
            if delta >= 0:
                target_ask = up_ask
                label = "UP"
            else:
                target_ask = down_ask
                label = "DOWN"

            if target_ask > 0 and target_ask <= 0.98:
                profit = ((1.0 / target_ask) - 1) * 100
                print(f"  └─ 🟢 WOULD FIRE: BUY {label} @ ${target_ask:.4f} "
                      f"(profit if wins: {profit:.1f}%)")
            elif target_ask > 0.98:
                print(f"  └─ 🟡 TOO EXPENSIVE: {label} ask ${target_ask:.4f} > $0.98 cap")
            else:
                print(f"  └─ 🔴 NO ASKS for {label} token")
        elif remaining <= 0:
            if candle_open_price:
                winner = "UP" if delta >= 0 else "DOWN"
                print(f"  └─ ⏰ Settling... Direction was: {dir_c}{winner}{R}")
            else:
                print(f"  └─ ⏰ Settling...")
        elif to_start > 0:
            print(f"  └─ ⏳ Candle hasn't started yet")
        else:
            secs_to_window = max(0, int(remaining - 15))
            print(f"  └─ ⏸️  Execution window in {secs_to_window}s")
        print()
        print(f"  {D}Ctrl+C to stop · Refreshing every {REFRESH_INTERVAL}s{R}")
        print(f"{B}{'═' * 68}{R}")

        await asyncio.sleep(REFRESH_INTERVAL)


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

async def main():
    price_task = asyncio.create_task(btc_price_stream())

    print("⏳ Fetching live BTC price...")
    while btc_price == 0.0:
        await asyncio.sleep(0.3)
    print(f"✅ BTC Price: ${btc_price:,.2f}\n")

    async with aiohttp.ClientSession() as session:
        while True:
            print("🔍 Finding next BTC 5-min candle...")
            market = await discover_market(session)

            if market is None:
                print("⚠️  No market found. Retrying in 10s...")
                await asyncio.sleep(10)
                continue

            print(f"✅ {market['title']}")
            await asyncio.sleep(1)

            try:
                await display_loop(session, market)
            except KeyboardInterrupt:
                break

            # Auto-advance to next candle
            print("\n🔄 Candle finished. Finding next...\n")
            await asyncio.sleep(2)

    price_task.cancel()
    print("\n👋 Stream stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
