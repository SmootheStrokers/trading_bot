"""
strategy_router.py — Maps each market to its correct strategy and configures
the EdgeFilter accordingly. The single place where strategy logic is selected.

Strategy assignment:
  BTC markets  → Strategy 1 (Momentum Carry) + Strategy 4 (Maker)
  ETH markets  → Strategy 2 (ETH Lag) with BTC signal input + Strategy 4 (Maker)
  SOL markets  → Strategy 3 (Squeeze Detection)
  XRP markets  → Strategy 5 (Catalyst Only) — no trade without active catalyst
"""

import logging
from typing import Any, Dict, Optional

from config import BotConfig
from edge_filter import EdgeFilter
from models import Market, EdgeResult

logger = logging.getLogger("strategy_router")


def detect_asset(question) -> str:
    """Parse market question for asset: BTC, ETH, SOL, XRP, or UNKNOWN."""
    if hasattr(question, "question"):
        question = question.question
    q = (str(question) if question is not None else "").lower()
    if "bitcoin" in q or "btc" in q:
        return "BTC"
    if "ethereum" in q or "eth" in q:
        return "ETH"
    if "solana" in q or "sol" in q:
        return "SOL"
    if "xrp" in q or "ripple" in q:
        return "XRP"
    return "UNKNOWN"


class BinanceFeedInterface:
    """Minimal interface for BinanceFeed — used for type hints and testing."""

    def get_price(self, symbol: str) -> Optional[float]:
        ...

    def get_pct_move_from_window_open(self, symbol: str) -> Optional[float]:
        ...

    def get_window_open_price(self, symbol: str) -> Optional[float]:
        ...

    async def get_funding_rate(self, symbol: str) -> float:
        ...


class StrategyRouter:
    def __init__(self, config: BotConfig, edge_filter: EdgeFilter):
        self.config = config
        self.edge_filter = edge_filter

    async def route(
        self,
        market: Market,
        binance_feed: BinanceFeedInterface,
        btc_signal_state: Dict[str, Any],
        bankroll: Optional[float] = None,
    ) -> EdgeResult:
        """
        Route a market to the correct strategy evaluation.
        Returns EdgeResult — if has_edge is False, bot does not trade.
        """
        asset = detect_asset(market.question)
        market.asset = asset

        # Get live price feed (Kraken/Coinbase/CoinGecko)
        spot_price = binance_feed.get_price(asset)
        pct_move = binance_feed.get_pct_move_from_window_open(asset)
        window_open_price = binance_feed.get_window_open_price(asset)
        # Log price data for every market every scan
        if spot_price is None:
            logger.info(f"[{asset}] PRICE: spot=None (feed not ready?) — momentum/strategy may fail")
        else:
            pct_str = f"{pct_move:.2%}" if pct_move is not None else "None"
            win_str = f"${window_open_price:.2f}" if window_open_price else "None"
            logger.info(f"[{asset}] PRICE: spot=${spot_price:.2f} pct_move={pct_str} window_open={win_str}")

        # BTC neutral or up for SOL squeeze
        btc_pct = binance_feed.get_pct_move_from_window_open("BTC")
        btc_is_neutral_or_up = btc_pct is None or btc_pct >= -0.002

        funding_rate = 0.0
        if asset in ("BTC", "ETH", "SOL"):
            funding_rate = await binance_feed.get_funding_rate(asset)

        btc_price_history = None
        if asset == "BTC":
            btc_price_history = binance_feed.get_price_history(asset)

        result = await self.edge_filter.evaluate(
            market,
            btc_signal_state=btc_signal_state,
            spot_price=spot_price,
            window_open_price=window_open_price,
            pct_move=pct_move,
            funding_rate=funding_rate if asset == "SOL" else None,
            btc_is_neutral_or_up=btc_is_neutral_or_up,
            btc_price_history=btc_price_history,
            bankroll=bankroll,
        )
        result.asset = asset
        return result
