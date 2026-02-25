"""
gamma_client.py — Fetches market data from Polymarket Gamma API (public, no auth).
Used for discovering 15-min crypto Up/Down markets; CLOB is for order book & trading.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import aiohttp

logger = logging.getLogger("gamma")

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CRYPTO_TAG_ID = 21  # Crypto tag for BTC/ETH/SOL markets

# 15-min event slug pattern: {coin}-updown-15m-{unix_ts}
# unix_ts = start of 15-min window (:00, :15, :30, :45 UTC)
COINS_15MIN = ["btc", "eth", "sol", "xrp"]


def _floor_to_15min(utc_dt: datetime) -> int:
    """Return Unix timestamp of 15-min boundary (floor)."""
    min_of_day = utc_dt.hour * 60 + utc_dt.minute
    slot = (min_of_day // 15) * 15
    base = utc_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int((base + timedelta(minutes=slot)).timestamp())


async def fetch_event_by_slug(session: aiohttp.ClientSession, slug: str) -> Optional[dict]:
    """Fetch a single event by slug. Returns None on 404 or error."""
    try:
        async with session.get(f"{GAMMA_API_URL}/events/slug/{slug}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            return await resp.json()
    except Exception as e:
        logger.debug(f"Gamma event slug {slug}: {e}")
        return None


async def fetch_15min_markets_by_slugs(
    min_secs_remaining: float = 60,
    max_secs_remaining: float = 900,
) -> List[dict]:
    """
    Fetch active 15-min Up/Down markets by computing slug patterns.
    Generates slugs for current and next 2 windows across BTC, ETH, SOL.
    Returns list of market dicts in Gamma format (one per event market).
    """
    now = datetime.now(timezone.utc)
    base_ts = _floor_to_15min(now)
    # Fetch current window + next 2 (markets may be listed slightly early)
    timestamps = [base_ts, base_ts + 900, base_ts + 1800]
    seen_condition_ids = set()
    all_markets = []

    async with aiohttp.ClientSession() as session:
        slugs = [f"{coin}-updown-15m-{ts}" for coin in COINS_15MIN for ts in timestamps]
        results = await asyncio.gather(
            *[fetch_event_by_slug(session, s) for s in slugs],
            return_exceptions=True,
        )
        for event in results:
            if isinstance(event, Exception):
                logger.debug(f"Gamma slug fetch failed: {event}")
                continue
            if not event or not isinstance(event, dict):
                continue
            markets = event.get("markets") or []
            for m in markets:
                if not m.get("active") or m.get("closed"):
                    continue
                if not m.get("acceptingOrders", True):
                    continue
                cid = m.get("conditionId") or m.get("condition_id")
                if not cid or cid in seen_condition_ids:
                    continue
                seen_condition_ids.add(cid)
                end_str = m.get("endDate") or m.get("endDateIso")
                if not end_str:
                    continue
                end_str = end_str.replace("Z", "+00:00")
                if "T" not in end_str:
                    end_str += "T23:59:59+00:00"
                try:
                    end_dt = datetime.fromisoformat(end_str)
                except Exception:
                    continue
                secs = (end_dt - now).total_seconds()
                if secs > 0 and min_secs_remaining <= secs <= max_secs_remaining:
                    all_markets.append(m)

    return all_markets


async def fetch_markets(
    session: aiohttp.ClientSession,
    *,
    active: bool = True,
    closed: bool = False,
    tag_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    order: Optional[str] = None,
    ascending: Optional[bool] = None,
) -> List[dict]:
    """Fetch markets from Gamma API."""
    params = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": limit,
        "offset": offset,
    }
    if order is not None:
        params["order"] = order
    if ascending is not None:
        params["ascending"] = str(ascending).lower()
    if tag_id is not None:
        params["tag_id"] = tag_id

    async with session.get(f"{GAMMA_API_URL}/markets", params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_crypto_15min_markets(
    min_secs_remaining: float = 60,
    max_secs_remaining: float = 900,
) -> List[dict]:
    """
    Fetch active crypto Up/Down markets within the time window.
    Fetches crypto tag markets and filters client-side by end date.
    """
    now = datetime.now(timezone.utc)
    all_markets = []

    async with aiohttp.ClientSession() as session:
        try:
            all_raw = []
            for offset in [0, 100, 200]:
                markets = await fetch_markets(
                    session,
                    active=True,
                    closed=False,
                    tag_id=CRYPTO_TAG_ID,
                    limit=100,
                    offset=offset,
                )
                if not markets:
                    break
                all_raw.extend(markets)
            markets = all_raw
            for m in markets:
                if not m.get("active") or m.get("closed"):
                    continue
                if not m.get("acceptingOrders", True):
                    continue
                q = (m.get("question") or "").lower()
                updown = any(k in q for k in ["above", "below", "up", "down", "higher", "lower", "exceed", "greater", "hit", ">$"])
                crypto = any(a in q for a in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "megaeth"])
                if not (updown and crypto):
                    continue
                end_str = m.get("endDate") or m.get("endDateIso")
                if not end_str:
                    continue
                end_str = end_str.replace("Z", "+00:00")
                if "T" not in end_str:
                    end_str += "T23:59:59+00:00"
                try:
                    end_dt = datetime.fromisoformat(end_str)
                except Exception:
                    continue
                secs = (end_dt - now).total_seconds()
                if secs > 0 and min_secs_remaining <= secs <= max_secs_remaining:
                    all_markets.append(m)
        except Exception as e:
            logger.warning(f"Gamma fetch failed: {e}")

    return all_markets
