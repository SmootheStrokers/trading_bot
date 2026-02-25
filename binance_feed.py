"""
binance_feed.py — Price feed for BTC, ETH, SOL, XRP.
Uses Kraken/Coinbase (US-accessible) as primary; CoinGecko as last resort.
"""

import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Dict, Optional, TYPE_CHECKING

import aiohttp
import websockets

if TYPE_CHECKING:
    from config import BotConfig

logger = logging.getLogger("binance_feed")

PRICE_FEED_PRIORITY = [
    "kraken",      # US-accessible, no rate limit issues
    "coinbase",    # Backup
    "coingecko",   # Last resort — heavily rate limited
]

KRAKEN_WS_URL = "wss://ws.kraken.com"
KRAKEN_PAIRS = ["XBT/USD", "ETH/USD", "SOL/USD", "XRP/USD"]
KRAKEN_SYMBOL_MAP = {"XBT/USD": "BTC", "ETH/USD": "ETH", "SOL/USD": "SOL", "XRP/USD": "XRP"}

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_PRODUCTS = ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"]
COINBASE_SYMBOL_MAP = {
    "BTC-USD": "BTC", "ETH-USD": "ETH",
    "SOL-USD": "SOL", "XRP-USD": "XRP",
}

COINGECKO_IDS = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "ripple"}
BINANCE_FUTURES_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
COINGECKO_PRICE_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd"
)
FUNDING_CACHE_SECONDS = 300  # 5 minutes
WINDOW_MINUTES = 15
BUFFER_SIZE = 100
REST_POLL_INTERVAL = 30  # seconds (CoinGecko free tier ~10-20 req/min)
COINGECKO_RATE_LIMIT_BACKOFF = 90  # seconds when 429 received


class BinanceFeed:
    def __init__(self, config: Optional["BotConfig"] = None):
        self.config = config
        self.latest_prices: Dict[str, float] = {}
        self.window_open_prices: Dict[str, float] = {}
        self.price_history_buffer: Dict[str, deque] = {
            sym: deque(maxlen=BUFFER_SIZE) for sym in ("BTC", "ETH", "SOL", "XRP")
        }
        self._ws: Optional[object] = None
        self._running = False
        self._last_window_minute: Optional[int] = None
        self._funding_cache: Dict[str, tuple] = {}  # symbol -> (rate, timestamp)
        self._session: Optional[aiohttp.ClientSession] = None
        self._feed_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the price feed — tries Kraken, Coinbase, then CoinGecko."""
        self._running = True
        self._session = aiohttp.ClientSession()
        self._feed_task = asyncio.create_task(self._run_feed_loop())
        await asyncio.sleep(2)
        logger.info("BinanceFeed started — waiting for price data")

    async def stop(self):
        """Stop the feed and cleanup resources."""
        self._running = False
        self._ws = None
        if self._feed_task and not self._feed_task.done():
            self._feed_task.cancel()
            try:
                await self._feed_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
            self._session = None
        logger.debug("BinanceFeed stopped")

    async def _run_feed_loop(self):
        """Try feeds in priority order until one works."""
        for feed in PRICE_FEED_PRIORITY:
            if not self._running:
                return
            try:
                if feed == "kraken":
                    await self._connect_kraken()
                    return
                elif feed == "coinbase":
                    await self._connect_coinbase()
                    return
                elif feed == "coingecko":
                    logger.warning("Falling back to CoinGecko REST — rate limits apply")
                    await self._poll_coingecko()
                    return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(f"{feed} feed failed: {e} — trying next")

    async def _connect_kraken(self):
        """Kraken WebSocket — US accessible, no API key needed for public data."""
        subscribe_msg = {
            "event": "subscribe",
            "pair": KRAKEN_PAIRS,
            "subscription": {"name": "ticker"},
        }
        async with websockets.connect(KRAKEN_WS_URL) as ws:
            await ws.send(json.dumps(subscribe_msg))
            logger.info("Price feed: Kraken WebSocket connected")
            while self._running:
                try:
                    msg = json.loads(await ws.recv())
                except Exception as e:
                    raise RuntimeError(f"Kraken recv error: {e}") from e
                if isinstance(msg, list) and len(msg) >= 4:
                    pair = msg[3]
                    data = msg[1]
                    if isinstance(data, dict) and "c" in data:
                        price = float(data["c"][0])
                        symbol = KRAKEN_SYMBOL_MAP.get(pair)
                        if symbol and price > 0:
                            self.latest_prices[symbol] = price
                            self.price_history_buffer[symbol].append((price, time.time()))
                            self._update_window_open_prices()

    async def _connect_coinbase(self):
        """Coinbase WebSocket — US accessible, no API key for public ticker."""
        subscribe_msg = {
            "type": "subscribe",
            "product_ids": COINBASE_PRODUCTS,
            "channels": ["ticker"],
        }
        async with websockets.connect(COINBASE_WS_URL) as ws:
            await ws.send(json.dumps(subscribe_msg))
            logger.info("Price feed: Coinbase WebSocket connected")
            while self._running:
                try:
                    msg = json.loads(await ws.recv())
                except Exception as e:
                    raise RuntimeError(f"Coinbase recv error: {e}") from e
                if isinstance(msg, dict) and msg.get("type") == "ticker":
                    product = msg.get("product_id")
                    price_str = msg.get("price")
                    symbol = COINBASE_SYMBOL_MAP.get(product) if product else None
                    if symbol and price_str:
                        price = float(price_str)
                        if price > 0:
                            self.latest_prices[symbol] = price
                            self.price_history_buffer[symbol].append((price, time.time()))
                            self._update_window_open_prices()

    async def _poll_coingecko(self):
        """CoinGecko REST polling — last resort, heavily rate limited."""
        retry_delay = REST_POLL_INTERVAL
        await asyncio.sleep(2)
        while self._running and self._session:
            try:
                await self._fetch_coingecko_prices()
                retry_delay = REST_POLL_INTERVAL
            except asyncio.CancelledError:
                return
            except Exception as e:
                err_str = str(e).lower()
                if "429" in err_str or "too many requests" in err_str:
                    retry_delay = COINGECKO_RATE_LIMIT_BACKOFF
                    logger.warning(f"CoinGecko rate limited — backing off {retry_delay}s")
                else:
                    logger.warning(f"CoinGecko fetch error: {e}")
            await asyncio.sleep(retry_delay)

    async def _fetch_coingecko_prices(self):
        """Fetch BTC, ETH, SOL, XRP from CoinGecko."""
        timeout = aiohttp.ClientTimeout(total=10)
        async with self._session.get(COINGECKO_PRICE_URL, timeout=timeout) as resp:
            if resp.status == 429:
                await resp.read()
                raise RuntimeError("CoinGecko rate limited (429)")
            resp.raise_for_status()
            data = await resp.json()
        now = time.time()
        for symbol, cg_id in COINGECKO_IDS.items():
            price = data.get(cg_id, {}).get("usd")
            if price and price > 0:
                self.latest_prices[symbol] = float(price)
                self.price_history_buffer[symbol].append((float(price), now))
        self._update_window_open_prices()

    def _update_window_open_prices(self):
        """Reset window_open_prices on each 15-min clock boundary."""
        now = datetime.now(timezone.utc)
        minute = now.minute
        current_window = minute // WINDOW_MINUTES
        if self._last_window_minute is not None:
            last_window = self._last_window_minute // WINDOW_MINUTES
            if current_window != last_window:
                for sym, price in self.latest_prices.items():
                    if price > 0:
                        self.window_open_prices[sym] = price
                logger.info(f"Window open prices updated: {self.window_open_prices}")
        self._last_window_minute = minute
        if not self.window_open_prices and self.latest_prices:
            for sym, price in self.latest_prices.items():
                if price > 0:
                    self.window_open_prices[sym] = price

    def get_price(self, symbol: str) -> Optional[float]:
        """Get latest spot price for symbol (e.g. 'BTC', 'ETH')."""
        return self.latest_prices.get(symbol)

    def get_pct_move_from_window_open(self, symbol: str) -> Optional[float]:
        """Get % move from window open price for symbol."""
        spot = self.latest_prices.get(symbol)
        open_price = self.window_open_prices.get(symbol)
        if spot and open_price and open_price > 0:
            return (spot - open_price) / open_price
        return None

    def get_price_history(self, symbol: str) -> list:
        """Get rolling price history (prices only) for symbol."""
        buf = self.price_history_buffer.get(symbol, deque())
        return [p for p, _ in buf]

    def get_window_open_price(self, symbol: str) -> Optional[float]:
        """Get the window open price for symbol."""
        return self.window_open_prices.get(symbol)

    async def get_funding_rate(self, symbol: str) -> float:
        """
        Fetch funding rate from Binance Futures API.
        Cache for 5 minutes.
        """
        now = time.time()
        cached = self._funding_cache.get(symbol)
        if cached:
            rate, ts = cached
            if now - ts < FUNDING_CACHE_SECONDS:
                return rate
        if not self._session:
            self._session = aiohttp.ClientSession()
        try:
            binance_sym = f"{symbol}USDT"
            url = f"{BINANCE_FUTURES_FUNDING_URL}?symbol={binance_sym}"
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json()
                rate = float(data.get("lastFundingRate", 0))
                self._funding_cache[symbol] = (rate, now)
                return rate
        except Exception as e:
            logger.warning(f"Funding rate fetch failed for {symbol}: {e}")
            if cached:
                return cached[0]
            return 0.0
