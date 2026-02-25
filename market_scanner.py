"""
market_scanner.py — Discovers and enriches active 15-min Up/Down markets.
Uses Gamma API for discovery (no auth); CLOB for order book & price history.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from config import BotConfig
from clob_client import ClobClient
from gamma_client import fetch_crypto_15min_markets, fetch_15min_markets_by_slugs
from models import Market, OrderBook, OrderBookLevel, PriceTick

logger = logging.getLogger("scanner")


class MarketScanner:
    def __init__(self, config: BotConfig, client: ClobClient):
        self.config = config
        self.client = client
        self._api_semaphore = asyncio.Semaphore(5)  # Limit concurrent API calls

    async def fetch_active_15min_markets(self) -> List[Market]:
        """
        Fetch all active 15-minute Up/Down markets, enriched with
        order book data and price history.
        """
        candidates = await self._discover_15min_markets()
        logger.debug(f"Found {len(candidates)} raw 15-min candidates")

        enriched = []
        tasks = [self._enrich_market(m) for m in candidates]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"Market enrichment failed: {r}")
            elif r is not None:
                enriched.append(r)

        return enriched

    async def _discover_15min_markets(self) -> List[Market]:
        """
        Fetch 15-min crypto Up/Down markets from Gamma API.
        Primary: slug-based discovery (btc-updown-15m-{ts}, etc.) for strict 15-min markets.
        Fallback: tag-based discovery for longer-dated crypto up/down markets.
        """
        min_secs = self.config.MARKET_MIN_TIME_REMAINING
        max_secs = self.config.MARKET_MAX_TIME_REMAINING

        # Try slug-based discovery first (true 15-min markets; max ~900 sec remaining)
        raw_markets = await fetch_15min_markets_by_slugs(
            min_secs_remaining=min_secs,
            max_secs_remaining=min(max_secs, 900),  # 15-min markets only
        )
        if not raw_markets:
            raw_markets = await fetch_crypto_15min_markets(
                min_secs_remaining=min_secs,
                max_secs_remaining=max_secs,
            )
        logger.debug(f"Gamma returned {len(raw_markets)} raw crypto up/down candidates")

        markets = []
        for m in raw_markets:
            parsed = self._parse_gamma_market(m)
            if parsed and self._is_15min_updown(m, parsed):
                markets.append(parsed)

        logger.info(f"Market scan complete: {len(markets)} 15-min markets found")
        return markets

    def _parse_gamma_market(self, raw: dict) -> Optional[Market]:
        """Parse Gamma API market dict into Market object."""
        try:
            condition_id = raw.get("conditionId") or raw.get("condition_id")
            question = raw.get("question", "")
            end_str = raw.get("endDate") or raw.get("endDateIso", "")
            if not condition_id or not end_str:
                return None

            clob_ids = raw.get("clobTokenIds")
            if isinstance(clob_ids, str):
                try:
                    token_ids = json.loads(clob_ids)
                except json.JSONDecodeError:
                    return None
            elif isinstance(clob_ids, list):
                token_ids = clob_ids
            else:
                return None
            if len(token_ids) < 2:
                return None

            end_str = end_str.replace("Z", "+00:00")
            if "T" not in end_str:
                end_str += "T23:59:59+00:00"  # Date-only from Gamma = end of day UTC
            end_dt = datetime.fromisoformat(end_str)
            end_ts = end_dt.timestamp()

            return Market(
                condition_id=condition_id,
                question=question,
                yes_token_id=str(token_ids[0]),
                no_token_id=str(token_ids[1]),
                end_date_iso=end_str,
                end_timestamp=end_ts,
            )
        except Exception as e:
            logger.debug(f"Failed to parse Gamma market: {e}")
            return None

    def _is_15min_updown(self, raw: dict, market: Market) -> bool:
        """Filter: active, accepting orders, Up/Down style, within time window."""
        if not raw.get("active", False):
            return False
        if raw.get("closed", True):
            return False
        if not raw.get("acceptingOrders", True):
            return False

        q = market.question.lower()
        updown_keywords = ["above", "below", "higher", "lower", "up", "down", "exceed", "reach", "hit"]
        if not any(kw in q for kw in updown_keywords):
            return False

        secs_remaining = market.seconds_remaining
        if secs_remaining < self.config.MARKET_MIN_TIME_REMAINING:
            return False
        if secs_remaining > self.config.MARKET_MAX_TIME_REMAINING:
            return False

        return True

    async def _enrich_market(self, market: Market) -> Optional[Market]:
        """Fetch order book and price history from CLOB."""
        async with self._api_semaphore:
            try:
                ob_raw = await self.client.get_order_book(market.yes_token_id)
                market.order_book = self._parse_order_book(ob_raw)
                try:
                    no_ob_raw = await self.client.get_order_book(market.no_token_id)
                    market.no_order_book = self._parse_order_book(no_ob_raw)
                except Exception as no_ob_err:
                    logger.debug(f"NO order book unavailable: {no_ob_err}")
                    market.no_order_book = None

                if market.order_book:
                    total_depth = (
                        market.order_book.total_bid_depth +
                        market.order_book.total_ask_depth
                    )
                    if total_depth < self.config.MIN_LIQUIDITY_USDC:
                        logger.debug(f"Skipping thin market: {market.question[:40]} (${total_depth:.0f})")
                        return None
                    max_spread = getattr(self.config, "MAX_SPREAD_CENTS", 0.10)
                    best_bid = market.order_book.best_yes_bid or 0
                    best_ask = market.order_book.best_yes_ask or 1
                    spread = best_ask - best_bid if best_ask > best_bid else 0
                    if spread > max_spread:
                        logger.debug(
                            f"Skipping wide spread: {market.question[:40]} ({spread:.2%} > {max_spread:.2%})"
                        )
                        return None

                try:
                    # CLOB prices-history expects market (condition_id), not token_id
                    hist_raw = await self.client.get_price_history(market.condition_id)
                    market.price_history = self._parse_price_history(hist_raw)
                except Exception as hist_err:
                    logger.debug(f"Price history unavailable for {market.condition_id[:16]}: {hist_err}")
                    market.price_history = []  # Continue without momentum/volume signals

                return market

            except Exception as e:
                logger.warning(f"Enrichment failed for {market.condition_id}: {e}")
                return None

    def _parse_order_book(self, raw: dict) -> OrderBook:
        ob = OrderBook(timestamp=datetime.now(timezone.utc))
        for level in raw.get("bids", []):
            ob.yes_bids.append(OrderBookLevel(
                price=float(level["price"]),
                size=float(level["size"]),
            ))
        for level in raw.get("asks", []):
            ob.yes_asks.append(OrderBookLevel(
                price=float(level["price"]),
                size=float(level["size"]),
            ))
        ob.yes_bids.sort(key=lambda x: x.price, reverse=True)
        ob.yes_asks.sort(key=lambda x: x.price)
        return ob

    def _parse_price_history(self, raw: dict) -> List[PriceTick]:
        ticks = []
        for item in raw.get("history", []):
            try:
                ticks.append(PriceTick(
                    price=float(item["p"]),
                    volume=float(item.get("v", 0)),
                    timestamp=datetime.fromtimestamp(item["t"], tz=timezone.utc),
                ))
            except Exception:
                continue
        return sorted(ticks, key=lambda x: x.timestamp)
