"""
clob_client.py — Async wrapper around the Polymarket CLOB API.
Handles auth, request signing, and raw API calls.
"""

import asyncio
import base64
import hashlib
import hmac
import time
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp
from config import BotConfig

logger = logging.getLogger("clob_client")


class ClobClient:
    def __init__(self, config: BotConfig):
        self.config = config
        self.base_url = config.CLOB_API_URL
        self.data_api_url = "https://data-api.polymarket.com"
        self._session: Optional[aiohttp.ClientSession] = None
        self._closed = False
        self._last_request_time = datetime.min
        self._request_delay = getattr(config, "CLOB_REQUEST_DELAY", 0.5)

    async def start(self):
        """Call once at bot startup — creates the shared session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            logger.info("ClobClient session opened")

    async def close(self):
        """Close the underlying aiohttp session. Safe to call multiple times."""
        if self._closed:
            return
        self._closed = True
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            logger.info("ClobClient session closed")

    def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("ClobClient is closed")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _sign_request(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate CLOB API auth headers using HMAC-SHA256 (Polymarket L2 spec)."""
        timestamp = str(int(time.time() * 1000))
        message = str(timestamp) + method.upper() + path
        if body:
            message += str(body).replace("'", '"')
        # Secret from Polymarket is base64-encoded; decode before HMAC
        try:
            secret_bytes = base64.urlsafe_b64decode(self.config.API_SECRET)
        except Exception as e:
            raise ValueError(f"Invalid POLY_API_SECRET (expected base64): {e}") from e
        h = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256)
        signature = base64.urlsafe_b64encode(h.digest()).decode("utf-8")
        headers = {
            "POLY-API-KEY": self.config.API_KEY,
            "POLY-PASSPHRASE": self.config.API_PASSPHRASE,
            "POLY-TIMESTAMP": timestamp,
            "POLY-SIGNATURE": signature,
            "Content-Type": "application/json",
        }
        # POLY_ADDRESS (Polygon signer/funder) required for authenticated endpoints
        addr = getattr(self.config, "PROXY_WALLET", None)
        if addr:
            headers["POLY-ADDRESS"] = addr if addr.startswith("0x") else f"0x{addr}"
        return headers

    def _is_rate_limited(self, e: Exception) -> bool:
        s = str(e).lower()
        return "429" in s or "too many requests" in s

    def _retry_delay(self, attempt: int, is_429: bool) -> float:
        if is_429:
            base = getattr(self.config, "RETRY_429_DELAY_SECONDS", 5.0)
            return base * (2 ** attempt)  # Exponential backoff for 429
        return self.config.RETRY_DELAY_SECONDS

    async def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        # Rate limit gate — enforce minimum delay between requests
        now = datetime.utcnow()
        elapsed = (now - self._last_request_time).total_seconds()
        if elapsed < self._request_delay:
            await asyncio.sleep(self._request_delay - elapsed)
        self._last_request_time = datetime.utcnow()

        # Path for HMAC must match request; include query string when params present
        signed_path = path
        if params and any(v for v in params.values()):
            filtered = {k: v for k, v in params.items() if v is not None and v != ""}
            if filtered:
                signed_path = f"{path}?{urlencode(filtered)}"

        session = self._get_session()
        headers = self._sign_request("GET", signed_path)
        url = f"{self.base_url}{path}"
        max_attempts = self.config.RETRY_ATTEMPTS + 2  # Extra retries for 429
        for attempt in range(max_attempts):
            try:
                async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        await resp.read()  # Drain to release connection
                        wait = int(resp.headers.get("Retry-After", 10))
                        logger.warning(f"Rate limited on {path} — waiting {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError as e:
                is_429 = e.status == 429
                logger.warning(f"GET {path} attempt {attempt+1} failed: {e}")
                if attempt < max_attempts - 1:
                    delay = self._retry_delay(attempt, is_429)
                    await asyncio.sleep(delay)
                else:
                    raise
            except Exception as e:
                is_429 = self._is_rate_limited(e)
                logger.warning(f"GET {path} attempt {attempt+1} failed: {e}")
                if attempt < max_attempts - 1:
                    delay = self._retry_delay(attempt, is_429)
                    await asyncio.sleep(delay)
                else:
                    raise

    async def _post(self, path: str, body: Dict) -> Any:
        import json
        # Rate limit gate
        now = datetime.utcnow()
        elapsed = (now - self._last_request_time).total_seconds()
        if elapsed < self._request_delay:
            await asyncio.sleep(self._request_delay - elapsed)
        self._last_request_time = datetime.utcnow()

        session = self._get_session()
        body_str = json.dumps(body)
        headers = self._sign_request("POST", path, body_str)
        url = f"{self.base_url}{path}"
        max_attempts = self.config.RETRY_ATTEMPTS + 2
        for attempt in range(max_attempts):
            try:
                async with session.post(url, headers=headers, data=body_str, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 429:
                        await resp.read()  # Drain to release connection
                        wait = int(resp.headers.get("Retry-After", 10))
                        logger.warning(f"Rate limited on {path} — waiting {wait}s")
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError as e:
                is_429 = e.status == 429
                logger.warning(f"POST {path} attempt {attempt+1} failed: {e}")
                if attempt < max_attempts - 1:
                    delay = self._retry_delay(attempt, is_429)
                    await asyncio.sleep(delay)
                else:
                    raise
            except Exception as e:
                is_429 = self._is_rate_limited(e)
                logger.warning(f"POST {path} attempt {attempt+1} failed: {e}")
                if attempt < max_attempts - 1:
                    delay = self._retry_delay(attempt, is_429)
                    await asyncio.sleep(delay)
                else:
                    raise

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_markets(self, next_cursor: str = "") -> Dict:
        """Fetch paginated list of markets."""
        params = {"next_cursor": next_cursor} if next_cursor else {}
        return await self._get("/markets", params=params)

    async def get_market(self, condition_id: str) -> Dict:
        """Fetch a single market by condition ID."""
        return await self._get(f"/markets/{condition_id}")

    async def get_order_book(self, token_id: str) -> Dict:
        """Fetch order book for a token."""
        return await self._get(f"/book", params={"token_id": token_id})

    async def get_last_trade_price(self, token_id: str) -> Dict:
        """Get last traded price for a token."""
        return await self._get(f"/last-trade-price", params={"token_id": token_id})

    async def get_price_history(self, market_or_token_id: str, interval: str = "1m", fidelity: int = 60) -> Dict:
        """Get historical price/volume data. Pass condition_id (recommended) or token_id as market param."""
        params = {
            "market": market_or_token_id,
            "interval": interval,
            "fidelity": fidelity,
        }
        return await self._get("/prices-history", params=params)

    # ── Order Management ──────────────────────────────────────────────────────

    async def create_order(self, order: Dict) -> Dict:
        """Place a new order on the CLOB."""
        return await self._post("/order", order)

    async def cancel_order(self, order_id: str) -> Dict:
        """Cancel an open order."""
        return await self._post("/cancel", {"orderID": order_id})

    async def cancel_all_orders(self) -> Dict:
        """Cancel all open orders."""
        return await self._post("/cancel-all", {})

    async def get_order(self, order_id: str) -> Dict:
        """Get status of a specific order."""
        return await self._get(f"/order/{order_id}")

    async def get_open_orders(self) -> List[Dict]:
        """Get all open orders."""
        result = await self._get("/orders", params={"state": "LIVE"})
        return result.get("data", [])

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> Dict:
        """Get USDC balance and allowance. Uses /balance-allowance (CLOB API)."""
        return await self._get("/balance-allowance")

    async def get_positions(self) -> List[Dict]:
        """Get current token positions from the Polymarket Data API."""
        wallet = getattr(self.config, "PROXY_WALLET", None)
        if not wallet:
            raise ValueError("PROXY_WALLET not set in config — required for positions lookup")

        session = self._get_session()
        url = f"{self.data_api_url}/positions"
        params = {"user": wallet}

        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data if isinstance(data, list) else data.get("data", [])
