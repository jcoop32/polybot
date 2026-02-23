"""
perf.py — Shared Performance Utilities for Paper Trading Bots

Provides:
  - WebSocket orderbook streaming (from sniper.py) with REST fallback
  - Decoupled UI renderer (2 FPS max, single sys.stdout.write)
  - Optimized aiohttp session factory with connection pooling
  - Fast JSON parser (orjson with stdlib fallback)
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
from typing import Optional

import aiohttp

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────
# FAST JSON
# ─────────────────────────────────────────────────────────────

try:
    import orjson
    json_loads = orjson.loads
    json_dumps = lambda obj: orjson.dumps(obj).decode()
except ImportError:
    import json
    json_loads = json.loads
    json_dumps = json.dumps


# ─────────────────────────────────────────────────────────────
# SESSION FACTORY
# ─────────────────────────────────────────────────────────────

def create_session() -> aiohttp.ClientSession:
    """Create an optimized aiohttp session with warm connection pooling."""
    connector = aiohttp.TCPConnector(
        keepalive_timeout=60,
        limit=20,
        enable_cleanup_closed=True,
    )
    return aiohttp.ClientSession(connector=connector)


# ─────────────────────────────────────────────────────────────
# WEBSOCKET ORDERBOOK STREAMING
# ─────────────────────────────────────────────────────────────

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_HOST = "https://clob.polymarket.com"

# Per-token orderbook cache, updated by WS background tasks
# Key: token_id → dict with ask_price, ask_size, bid_price, spread, depth_usd, updated
orderbook_cache: dict[str, dict] = {}

_EMPTY_OB = {
    "ask_price": 0.0, "ask_size": 0.0, "bid_price": 0.0,
    "spread": 0.0, "depth_usd": 0.0, "updated": 0.0,
}


def _process_orderbook_message(data: dict, token_id: str):
    """Parse CLOB WS message and update orderbook_cache."""
    entry = orderbook_cache.setdefault(token_id, dict(_EMPTY_OB))
    asks = None
    bids = None

    # Direct asks/bids arrays
    if "asks" in data:
        asks = data["asks"]
    elif "market" in data and isinstance(data["market"], dict):
        asks = data["market"].get("asks")

    if "bids" in data:
        bids = data["bids"]
    elif "market" in data and isinstance(data["market"], dict):
        bids = data["market"].get("bids")

    if asks:
        best = min(asks, key=lambda a: float(a.get("price", a.get("p", 999))))
        entry["ask_price"] = float(best.get("price", best.get("p", 0)))
        entry["ask_size"] = float(best.get("size", best.get("s", 0)))
        entry["depth_usd"] = entry["ask_price"] * entry["ask_size"]

    if bids:
        best_bid = max(bids, key=lambda b: float(b.get("price", b.get("p", 0))))
        entry["bid_price"] = float(best_bid.get("price", best_bid.get("p", 0)))

    if entry["ask_price"] > 0 and entry["bid_price"] > 0:
        entry["spread"] = entry["ask_price"] - entry["bid_price"]

    # Price change events
    if data.get("type") == "price_change":
        changes = data.get("changes", [data])
        for change in changes:
            if change.get("asset_id") == token_id:
                price = change.get("price")
                if price is not None:
                    entry["ask_price"] = float(price)

    entry["updated"] = time.time()


async def orderbook_ws_stream(token_id: str, stop_event: asyncio.Event):
    """
    Subscribe to CLOB WebSocket for real-time orderbook updates.
    Updates orderbook_cache[token_id] on each message.
    Reconnects on failure until stop_event is set.
    """
    if websockets is None:
        return  # websockets not installed, rely on REST fallback

    orderbook_cache[token_id] = dict(_EMPTY_OB)

    while not stop_event.is_set():
        try:
            async with websockets.connect(CLOB_WS_URL, ping_interval=10) as ws:
                sub_msg = json_dumps({"type": "market", "assets_ids": [token_id]})
                await ws.send(sub_msg)

                async for msg in ws:
                    if stop_event.is_set():
                        break
                    try:
                        data = json_loads(msg)
                        _process_orderbook_message(data, token_id)
                    except Exception:
                        continue

        except Exception:
            pass

        if not stop_event.is_set():
            await asyncio.sleep(1)


def get_cached_orderbook(token_id: str, max_age: float = 5.0) -> dict:
    """
    Read cached orderbook — microsecond read vs 100ms+ REST.
    Returns cached data if fresh (< max_age seconds old), otherwise empty dict.
    """
    entry = orderbook_cache.get(token_id)
    if entry and entry["updated"] > 0:
        age = time.time() - entry["updated"]
        if age <= max_age:
            return entry
    return dict(_EMPTY_OB)


async def fetch_orderbook_rest(
    session: aiohttp.ClientSession, token_id: str
) -> dict:
    """REST fallback for orderbook when WS hasn't populated yet."""
    result = dict(_EMPTY_OB)
    try:
        url = f"{CLOB_HOST}/book"
        params = {"token_id": token_id}
        async with session.get(url, params=params,
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return result
            data = await resp.json()
            asks = data.get("asks", [])
            bids = data.get("bids", [])
            if asks:
                best_ask = min(asks, key=lambda a: float(a.get("price", 999)))
                result["ask_price"] = float(best_ask["price"])
                result["ask_size"] = float(best_ask.get("size", 0))
                result["depth_usd"] = result["ask_price"] * result["ask_size"]
            if bids:
                best_bid = max(bids, key=lambda b: float(b.get("price", 0)))
                result["bid_price"] = float(best_bid["price"])
            if result["ask_price"] > 0 and result["bid_price"] > 0:
                result["spread"] = result["ask_price"] - result["bid_price"]
            result["updated"] = time.time()
            # Also populate the cache
            orderbook_cache[token_id] = dict(result)
            return result
    except Exception:
        return result


# ─────────────────────────────────────────────────────────────
# DYNAMIC TAKER FEE (Jan 2026 — crypto markets)
# ─────────────────────────────────────────────────────────────
#
# Formula: fee = C × 0.25 × (p × (1-p))²
# Max fee: ~1.5625% at p=0.50, drops to near 0% at extremes.
# Makers pay ZERO fees. This only applies to taker orders.
#

FEE_CONSTANT_C = 1.0  # Polymarket's multiplier constant


def calc_taker_fee(probability: float) -> float:
    """
    Calculate the dynamic taker fee as a fraction (0.0–0.015625).

    Args:
        probability: Market probability (0.0–1.0), i.e. the ask price.

    Returns:
        Fee as a decimal fraction (e.g. 0.015625 = 1.5625%).
    """
    p = max(0.0, min(1.0, probability))
    return FEE_CONSTANT_C * 0.25 * (p * (1 - p)) ** 2


def calc_taker_fee_bps(probability: float) -> int:
    """Return the taker fee in basis points (for feeRateBps field)."""
    return round(calc_taker_fee(probability) * 10_000)


async def fetch_fee_rate(
    session: aiohttp.ClientSession, token_id: str
) -> int:
    """
    Query the live fee rate from the CLOB for a specific token.
    Returns feeRateBps (int). Falls back to calc_taker_fee_bps if API fails.
    """
    try:
        url = f"{CLOB_HOST}/fee-rate"
        params = {"tokenID": token_id}
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=3)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return int(data.get("feeRateBps", data.get("fee_rate_bps", 0)))
    except Exception:
        pass
    # Fallback: estimate from midpoint probability
    return calc_taker_fee_bps(0.5)


# ─────────────────────────────────────────────────────────────
# DECOUPLED UI RENDERER
# ─────────────────────────────────────────────────────────────

# UI state — written by trading logic (instant), read by renderer
ui_state: dict = {}


def update_ui(**kwargs):
    """Update UI state dict. Called from trading logic — non-blocking."""
    ui_state.update(kwargs)


async def ui_renderer(
    build_fn,
    shutdown_event: asyncio.Event,
    fps: float = 2.0,
):
    """
    Background task: renders dashboard at max `fps` frames per second.

    Args:
        build_fn: Callable(state: dict) -> str that builds the screen content.
        shutdown_event: Set to stop the renderer.
        fps: Maximum frames per second (default 2.0).
    """
    interval = 1.0 / fps
    while not shutdown_event.is_set():
        if ui_state:
            try:
                screen = build_fn(ui_state)
                sys.stdout.write("\033[2J\033[H" + screen)
                sys.stdout.flush()
            except Exception:
                pass
        await asyncio.sleep(interval)


# ─────────────────────────────────────────────────────────────
# ADAPTIVE SLEEP
# ─────────────────────────────────────────────────────────────

async def adaptive_sleep(
    remaining: float,
    trade_window: float,
    *,
    exec_hz: float = 20.0,
    approach_hz: float = 2.0,
    coast_hz: float = 0.5,
):
    """
    Sleep for a duration based on how close we are to the execution window.

    Args:
        remaining: Seconds remaining in the candle.
        trade_window: The execution window size in seconds.
        exec_hz: Tick rate during execution window.
        approach_hz: Tick rate in the 30s before execution.
        coast_hz: Tick rate while coasting.
    """
    if remaining <= trade_window:
        await asyncio.sleep(1.0 / exec_hz)
    elif remaining <= trade_window + 30:
        await asyncio.sleep(1.0 / approach_hz)
    else:
        await asyncio.sleep(1.0 / coast_hz)


async def adaptive_sleep_early(
    elapsed: float,
    compute_delay: float,
    exec_end: float,
    *,
    exec_hz: float = 20.0,
    approach_hz: float = 2.0,
    coast_hz: float = 0.5,
):
    """
    Adaptive sleep for start-of-candle execution (indicator bot).

    Args:
        elapsed: Seconds elapsed since candle start.
        compute_delay: When indicators are computed.
        exec_end: End of execution window.
    """
    if elapsed < compute_delay:
        await asyncio.sleep(1.0 / approach_hz)
    elif elapsed < exec_end:
        await asyncio.sleep(1.0 / exec_hz)
    else:
        await asyncio.sleep(1.0 / coast_hz)
