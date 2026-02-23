#!/usr/bin/env python3
"""
PolySniper — 15-Second Bitcoin Candle Scalper for Polymarket.

Targets the last 15 seconds of a BTC "Up or Down" 5-minute candle market
to scalp the winning outcome at a discount.

Strategy:
  - Track live BTC price via Binance WS
  - The candle "open" price is recorded at eventStartTime
  - If BTC is above open + buffer → "Up" is winning
  - If BTC is below open - buffer → "Down" is winning
  - In the last 15 seconds, buy the winning side if ask ≤ MAX_BUY_PRICE

Deploy on GCP europe-west4 (Netherlands) for <6ms latency to Polymarket
servers (eu-west-2) with an allowed IP.

Requirements:
    pip install py-clob-client websockets aiohttp python-dotenv
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import websockets
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from perf import calc_taker_fee, fetch_fee_rate

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

MAX_BUY_PRICE = 0.98          # Don't buy if best ask is above this (ensure ≥2% profit)
SAFE_BUFFER_USD = 50.0        # BTC must be this far from candle open to trade
TRADE_TIME_WINDOW = 15        # Start trading at this many seconds remaining
MAX_TRADE_USD = 5.0           # Max USDC to spend per trade (safety cap for testing)
HEARTBEAT_WINDOW = 60         # Start heartbeat logs at this many seconds remaining

# Polymarket API endpoints
CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Binance WebSocket for live BTC/USDT price
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws/btcusdt@trade"

CHAIN_ID = 137  # Polygon

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("polysniper")

# ─────────────────────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────────────────────

btc_price: float = 0.0            # Updated by Binance WS task
best_ask_price: float = 0.0       # Updated by CLOB WS or REST fallback
best_ask_size: float = 0.0        # Size available at best ask
order_fired: bool = False         # Prevent duplicate fires per candle
shutdown_event = asyncio.Event()  # Graceful shutdown


# ─────────────────────────────────────────────────────────────
# MARKET DISCOVERY — Slug-Based (fast, precise)
# ─────────────────────────────────────────────────────────────

async def discover_next_market(session: aiohttp.ClientSession) -> Optional[dict]:
    """
    Find the current or next BTC Up/Down 5-minute market.

    Slug pattern: btc-updown-5m-{candle_end_unix}
    Candles are 300s (5 min) aligned to the Unix epoch.

    Returns dict with:
        - slug, title, condition_id
        - event_start: datetime (candle open)
        - event_end: datetime (candle close)
        - token_up_id, token_down_id
    """
    now_ts = time.time()

    # Try current candle, then next candle
    for offset in [0, 300]:
        candle_end = math.ceil(now_ts / 300) * 300 + offset
        slug = f"btc-updown-5m-{candle_end}"

        try:
            url = f"{GAMMA_API}/events/slug/{slug}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    continue
                event = await resp.json()
        except Exception as e:
            log.error(f"Gamma API error for {slug}: {e}")
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

        try:
            event_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
            event_end = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        # Skip if already expired
        if event_end <= datetime.now(timezone.utc):
            continue

        # Parse outcomes and token IDs
        outcomes_str = market.get("outcomes", "[]")
        tokens_str = market.get("clobTokenIds", "[]")

        outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
        clob_tokens = json.loads(tokens_str) if isinstance(tokens_str, str) else tokens_str

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
        }

    return None


# ─────────────────────────────────────────────────────────────
# BINANCE WEBSOCKET — Live BTC Price
# ─────────────────────────────────────────────────────────────

async def binance_price_stream():
    """Persistent WebSocket for real-time BTC/USDT price."""
    global btc_price

    while not shutdown_event.is_set():
        try:
            async with websockets.connect(BINANCE_WS_URL, ping_interval=20) as ws:
                log.info("🔌 Connected to Binance BTC/USDT stream")
                async for msg in ws:
                    if shutdown_event.is_set():
                        break
                    data = json.loads(msg)
                    btc_price = float(data.get("p", 0))
        except websockets.ConnectionClosed:
            log.warning("Binance WS disconnected, reconnecting in 2s...")
        except Exception as e:
            log.error(f"Binance WS error: {e}")
        await asyncio.sleep(2)


# ─────────────────────────────────────────────────────────────
# POLYMARKET CLOB WEBSOCKET — Live Orderbook
# ─────────────────────────────────────────────────────────────

async def polymarket_orderbook_stream(token_id: str, stop_event: asyncio.Event):
    """
    Subscribe to the Polymarket CLOB WebSocket for real-time orderbook
    updates on a specific token. Updates global best_ask_price/size.
    """
    global best_ask_price, best_ask_size

    best_ask_price = 0.0
    best_ask_size = 0.0

    while not stop_event.is_set() and not shutdown_event.is_set():
        try:
            async with websockets.connect(CLOB_WS_URL, ping_interval=10) as ws:
                sub_msg = {"type": "market", "assets_ids": [token_id]}
                await ws.send(json.dumps(sub_msg))
                log.info(f"🔌 Subscribed to CLOB WS for token {token_id[:16]}...")

                async for msg in ws:
                    if stop_event.is_set() or shutdown_event.is_set():
                        break
                    try:
                        data = json.loads(msg)
                        _process_orderbook_message(data, token_id)
                    except json.JSONDecodeError:
                        continue

        except websockets.ConnectionClosed:
            if not stop_event.is_set():
                log.warning("CLOB WS disconnected, reconnecting in 1s...")
        except Exception as e:
            if not stop_event.is_set():
                log.error(f"CLOB WS error: {e}")
        await asyncio.sleep(1)


def _process_orderbook_message(data: dict, token_id: str):
    """Parse CLOB WS message and update best ask price/size."""
    global best_ask_price, best_ask_size

    # Handle various message formats from the CLOB WS
    asks = None

    # Direct asks array
    if "asks" in data:
        asks = data["asks"]
    # Nested under market key
    elif "market" in data and isinstance(data["market"], dict):
        asks = data["market"].get("asks")

    if asks:
        best = min(asks, key=lambda a: float(a.get("price", a.get("p", 999))))
        best_ask_price = float(best.get("price", best.get("p", 0)))
        best_ask_size = float(best.get("size", best.get("s", 0)))

    # Price change events
    if data.get("type") == "price_change":
        changes = data.get("changes", [data])
        for change in changes:
            if change.get("asset_id") == token_id:
                price = change.get("price")
                if price is not None:
                    best_ask_price = float(price)


# ─────────────────────────────────────────────────────────────
# ORDERBOOK FALLBACK (REST)
# ─────────────────────────────────────────────────────────────

async def fetch_orderbook_rest(
    session: aiohttp.ClientSession, token_id: str
) -> tuple[float, float]:
    """Fallback: fetch orderbook via CLOB REST API. Returns (best_ask, size)."""
    try:
        url = f"{CLOB_HOST}/book"
        params = {"token_id": token_id}
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return 0.0, 0.0
            data = await resp.json()
            asks = data.get("asks", [])
            if asks:
                best = min(asks, key=lambda a: float(a.get("price", 999)))
                return float(best["price"]), float(best.get("size", 0))
            return 0.0, 0.0
    except Exception as e:
        log.error(f"REST orderbook error: {e}")
        return 0.0, 0.0


# ─────────────────────────────────────────────────────────────
# ORDER EXECUTION
# ─────────────────────────────────────────────────────────────

def create_clob_client() -> ClobClient:
    """Initialize the authenticated CLOB client from environment variables."""
    private_key = os.environ["POLYMARKET_PRIVATE_KEY"]
    funder = os.environ.get("POLYMARKET_FUNDER")
    sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE", "0"))

    kwargs = {
        "host": CLOB_HOST,
        "key": private_key,
        "chain_id": CHAIN_ID,
    }
    if funder:
        kwargs["funder"] = funder
        kwargs["signature_type"] = sig_type

    client = ClobClient(**kwargs)
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def fire_fok_order(client: ClobClient, token_id: str, amount: float) -> dict:
    """Place a Fill-or-Kill market buy order for the given token."""
    trade_amount = min(amount, MAX_TRADE_USD)

    log.info(f"🎯 FIRING FOK BUY — Token: {token_id[:16]}... Amount: ${trade_amount:.2f}")

    market_order = MarketOrderArgs(
        token_id=token_id,
        amount=trade_amount,
        side=BUY,
    )

    signed_order = client.create_market_order(market_order)
    response = client.post_order(signed_order, OrderType.FOK)

    log.info(f"📦 Order response: {response}")
    return response


# ─────────────────────────────────────────────────────────────
# MAIN TRADING LOOP
# ─────────────────────────────────────────────────────────────

async def trading_loop():
    """
    Main bot loop:
    1. Discover the next BTC Up/Down 5-min candle market
    2. Record BTC price at candle open (the "open price")
    3. Phase 1: Monitor BTC vs open price
    4. Phase 2: Execute at T-minus TRADE_TIME_WINDOW seconds
    5. Repeat for the next candle
    """
    global order_fired, best_ask_price, best_ask_size

    log.info("=" * 60)
    log.info("🚀 PolySniper v2.0 — Up/Down Candle Scalper")
    log.info(f"   MAX_BUY_PRICE:     ${MAX_BUY_PRICE}")
    log.info(f"   SAFE_BUFFER_USD:   ${SAFE_BUFFER_USD}")
    log.info(f"   TRADE_TIME_WINDOW: {TRADE_TIME_WINDOW}s")
    log.info(f"   MAX_TRADE_USD:     ${MAX_TRADE_USD}")
    log.info("=" * 60)

    # Wait for Binance price to populate
    log.info("⏳ Waiting for Binance BTC price...")
    while btc_price == 0.0 and not shutdown_event.is_set():
        await asyncio.sleep(0.5)
    log.info(f"✅ BTC Price: ${btc_price:,.2f}")

    # Initialize CLOB client
    client = create_clob_client()
    log.info("✅ CLOB client authenticated")

    async with aiohttp.ClientSession() as session:
        while not shutdown_event.is_set():
            # ── Step 1: Discover next market ──
            log.info("🔍 Discovering next BTC 5-min candle...")
            market = await discover_next_market(session)

            if market is None:
                log.warning("No market found. Retrying in 15s...")
                await asyncio.sleep(15)
                continue

            event_start = market["event_start"]
            event_end = market["event_end"]
            token_up = market["token_up_id"]
            token_down = market["token_down_id"]

            log.info(f"📊 Target: {market['title']}")
            log.info(f"   Candle: {event_start.strftime('%H:%M:%S')} → {event_end.strftime('%H:%M:%S')} UTC")
            log.info(f"   UP  token: {token_up[:20]}...")
            log.info(f"   DOWN token: {token_down[:20]}...")

            # Reset state for this candle
            order_fired = False
            best_ask_price = 0.0
            best_ask_size = 0.0
            candle_open_price: float | None = None

            # Wait for candle to start if needed
            now = datetime.now(timezone.utc)
            time_to_start = (event_start - now).total_seconds()
            if time_to_start > 0:
                log.info(f"⏳ Candle starts in {int(time_to_start)}s. Waiting...")
                await asyncio.sleep(max(0, time_to_start - 1))

            # ── Record candle open price ──
            candle_open_price = btc_price
            log.info(f"📌 Candle OPEN price: ${candle_open_price:,.2f}")

            # ── Start CLOB WS subscription (we don't know which side yet) ──
            ws_stop = asyncio.Event()
            active_ws_task: asyncio.Task | None = None
            active_token_id: str | None = None

            # ── Phase 1 + Phase 2 Loop ──
            while not shutdown_event.is_set():
                now = datetime.now(timezone.utc)
                remaining = (event_end - now).total_seconds()

                if remaining <= 0:
                    log.info("⏰ Market expired. Moving to next candle.\n")
                    ws_stop.set()
                    break

                # Determine winning side based on open price
                delta = btc_price - candle_open_price
                abs_delta = abs(delta)

                if abs_delta < SAFE_BUFFER_USD:
                    side = "RISKY"
                    winning_token = None
                    emoji = "🔴"
                elif delta > 0:
                    side = "UP"
                    winning_token = token_up
                    emoji = "🟢"
                else:
                    side = "DOWN"
                    winning_token = token_down
                    emoji = "🟢"

                # Start/switch WS subscription if winning token changed
                if winning_token and winning_token != active_token_id:
                    if active_ws_task:
                        ws_stop.set()
                        await asyncio.sleep(0.2)
                        ws_stop = asyncio.Event()

                    active_token_id = winning_token
                    active_ws_task = asyncio.create_task(
                        polymarket_orderbook_stream(active_token_id, ws_stop)
                    )

                # ── Heartbeat (last HEARTBEAT_WINDOW seconds) ──
                if remaining <= HEARTBEAT_WINDOW:
                    secs_left = int(remaining)

                    if side == "RISKY":
                        log.info(
                            f"[{secs_left:>3}s left] {emoji} BTC RISKY "
                            f"(${btc_price:,.2f} vs open ${candle_open_price:,.2f} "
                            f"— delta ${abs_delta:,.0f} < ${SAFE_BUFFER_USD:.0f} buffer). "
                            f"ABORT."
                        )
                    else:
                        # Use WS price if available, else REST fallback
                        ask_p = best_ask_price
                        if ask_p == 0.0 and winning_token:
                            ask_p, _ = await fetch_orderbook_rest(session, winning_token)

                        # ── Phase 2: EXECUTION ──
                        if remaining <= TRADE_TIME_WINDOW and not order_fired:
                            if ask_p > 0 and ask_p <= MAX_BUY_PRICE:
                                # Calculate fee and check if trade is still profitable
                                fee_rate = calc_taker_fee(ask_p)
                                fee_pct = fee_rate * 100
                                profit = ((1.0 / ask_p) - 1) * 100
                                net_profit = profit - fee_pct
                                
                                if net_profit <= 0:
                                    log.info(
                                        f"[{secs_left:>3}s left] {emoji} BTC Safe "
                                        f"(${btc_price:,.2f} vs open ${candle_open_price:,.2f}). "
                                        f"Winning: {side}. Best Ask: ${ask_p:.4f}. "
                                        f"Profit: {profit:.1f}% - Fee: {fee_pct:.2f}% = "
                                        f"Net: {net_profit:.2f}% -> ⏸️ FEE EXCEEDS EDGE"
                                    )
                                else:
                                    log.info(
                                        f"[{secs_left:>3}s left] {emoji} BTC Safe "
                                        f"(${btc_price:,.2f} vs open ${candle_open_price:,.2f}). "
                                        f"Winning: {side}. Best Ask: ${ask_p:.4f}. "
                                        f"Profit: {profit:.1f}% - Fee: {fee_pct:.2f}% = "
                                        f"Net: {net_profit:.1f}%. -> 🔥 FIRING BUY!"
                                    )
                                    try:
                                        result = fire_fok_order(
                                            client, winning_token, MAX_TRADE_USD
                                        )
                                        order_fired = True
                                        log.info(f"✅ ORDER PLACED: {result}")
                                    except Exception as e:
                                        log.error(f"❌ ORDER FAILED: {e}")
                                        order_fired = True  # Don't retry on same candle
                            elif ask_p > MAX_BUY_PRICE:
                                log.info(
                                    f"[{secs_left:>3}s left] {emoji} BTC Safe "
                                    f"(${btc_price:,.2f} vs open ${candle_open_price:,.2f}). "
                                    f"Winning: {side}. Best Ask: ${ask_p:.4f}. "
                                    f"-> ⏸️ TOO EXPENSIVE (cap: ${MAX_BUY_PRICE})"
                                )
                            else:
                                log.info(
                                    f"[{secs_left:>3}s left] {emoji} BTC Safe "
                                    f"(${btc_price:,.2f} vs open ${candle_open_price:,.2f}). "
                                    f"Winning: {side}. No asks available. -> ⏸️ WAITING"
                                )
                        else:
                            # Phase 1 heartbeat (monitoring)
                            status = "READY" if not order_fired else "DONE ✅"
                            log.info(
                                f"[{secs_left:>3}s left] {emoji} BTC Safe "
                                f"(${btc_price:,.2f} vs open ${candle_open_price:,.2f}). "
                                f"Winning: {side}. Best Ask: ${ask_p:.4f}. {status}"
                            )

                await asyncio.sleep(1)

            # Cleanup WS task
            ws_stop.set()
            if active_ws_task:
                active_ws_task.cancel()

            # Brief pause before next candle
            await asyncio.sleep(2)


# ─────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────

async def main():
    """Launch all concurrent tasks."""
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: shutdown_event.set())

    log.info("🏁 PolySniper starting...")

    tasks = [
        asyncio.create_task(binance_price_stream()),
        asyncio.create_task(trading_loop()),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        shutdown_event.set()
        for t in tasks:
            t.cancel()
        log.info("👋 PolySniper shut down.")


if __name__ == "__main__":
    load_dotenv()

    if not os.environ.get("POLYMARKET_PRIVATE_KEY"):
        log.error("❌ POLYMARKET_PRIVATE_KEY not set in .env")
        sys.exit(1)

    asyncio.run(main())
