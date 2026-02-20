#!/usr/bin/env python3
"""
Quick test: Binance WebSocket BTC/USDT price stream.
Tries multiple endpoints. Prints live price for 30 seconds then exits.
"""

import asyncio
import json
import aiohttp

# Try multiple endpoints — some may be blocked depending on region/DNS
WS_ENDPOINTS = [
    "wss://stream.binance.com:9443/ws/btcusdt@trade",
    "wss://stream.binance.com:443/ws/btcusdt@trade",
    "wss://fstream.binance.com/ws/btcusdt@trade",
    "wss://stream.binance.us:9443/ws/btcusd@trade",
]

async def test_endpoint(url: str) -> bool:
    """Try connecting to one endpoint. Returns True if successful."""
    print(f"  🔌 Trying {url} ...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, timeout=aiohttp.ClientTimeout(total=10)) as ws:
                print(f"  ✅ Connected!\n")
                count = 0
                start = asyncio.get_event_loop().time()

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        price = float(data["p"])
                        count += 1
                        elapsed = asyncio.get_event_loop().time() - start
                        print(f"  ₿ ${price:>12,.2f}  ({count} updates, {elapsed:.0f}s)", end="\r")

                        if elapsed >= 30:
                            break
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        print(f"  ❌ WebSocket error: {msg}")
                        return False

                print(f"\n\n  📊 Received {count} price updates in 30s ({count/30:.0f}/sec)")
                print(f"  👍 Working endpoint: {url}")
                return True
    except Exception as e:
        print(f"  ❌ Failed: {type(e).__name__}: {e}\n")
        return False

async def main():
    print("🔍 Testing Binance WebSocket endpoints...\n")
    for url in WS_ENDPOINTS:
        if await test_endpoint(url):
            return
    print("\n⚠️  All endpoints failed. Binance may be blocked from this network.")
    print("   Try running this on your VM instead.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
