"""
Polymarket 15-Minute Up/Down Trading Bot
Core philosophy: profit edge or no trade.
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Optional

from config import BotConfig
from clob_client import ClobClient
from market_scanner import MarketScanner
from edge_filter import EdgeFilter
from executor import OrderExecutor
from position_manager import PositionManager
from orphan_handler import OrphanHandler
from state_writer import write_state
from logger import setup_logger
from binance_feed import BinanceFeed
from strategy_router import StrategyRouter

logger = setup_logger("main")


class PolymarketBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.clob = ClobClient(config)
        self.scanner = MarketScanner(config, self.clob)
        self.edge_filter = EdgeFilter(config)
        self.strategy_router = StrategyRouter(config, self.edge_filter)
        self.executor = OrderExecutor(config, self.clob)
        self.position_manager = PositionManager(
            config, self.clob, self.executor,
            stop_predicate=lambda: self.running,
        )
        self.orphan_handler = OrphanHandler(config, self.clob, self.position_manager)
        self.binance_feed = BinanceFeed(config)
        self.running = False
        self.start_time: Optional[datetime] = None
        self.signal_feed: list = []
        self._last_orphan_reconcile: Optional[float] = 0.0
        self.btc_signal_state = {
            "fired": False,
            "side": None,
            "pct_move": 0.0,
            "timestamp": None,
            "window_open_price": 0.0,
        }
        self.maker_positions: dict = {}  # condition_id -> {"yes_id", "no_id", "placed_at"}

    async def run(self):
        logger.info("=" * 60)
        logger.info("  Polymarket 15-Min Up/Down Bot — STARTING")
        if self.config.PAPER_TRADING:
            logger.info("  *** PAPER TRADING MODE — No real orders will be placed ***")
        if getattr(self.config, "DRY_RUN", False):
            logger.info("  *** DRY RUN — No real orders will be placed ***")
        logger.info(f"  Min edge signals required: {self.config.MIN_EDGE_SIGNALS}")
        logger.info(f"  Max concurrent positions:  {self.config.MAX_POSITIONS}")
        logger.info(f"  Bankroll:                  ${self.config.BANKROLL:.2f}")
        logger.info("=" * 60)

        self.running = True
        self.start_time = datetime.now(timezone.utc)
        await self.clob.start()
        await self.binance_feed.start()

        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, self.shutdown)

        try:
            await asyncio.gather(
                self.scan_loop(),
                self.maker_loop(),
                self.position_manager.monitor_loop(),
                self.catalyst_watcher(),
            )
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Close all positions, then network sessions."""
        await self.position_manager.close_all_async()
        await self.binance_feed.stop()
        await self.clob.close()

    async def catalyst_watcher(self):
        """Read catalyst_flag.json every 30s and update config."""
        path = Path("catalyst_flag.json")
        while self.running:
            try:
                await asyncio.sleep(30)
                if not path.exists():
                    continue
                text = path.read_text()
                data = json.loads(text)
                if data.get("asset") == "XRP":
                    self.config.XRP_CATALYST_ACTIVE = True
                    self.config.XRP_CATALYST_DIRECTION = data.get("direction", "UP").upper()
                    self.config.XRP_CATALYST_SET_TIME = datetime.now(timezone.utc).isoformat()
                    logger.info(f"XRP catalyst set: {data.get('direction', 'UP')} — {data.get('reason', '')}")
            except Exception as e:
                if path.exists():
                    logger.debug(f"catalyst_flag parse error: {e}")

    def _is_maker_hours(self) -> bool:
        try:
            from zoneinfo import ZoneInfo
            et = ZoneInfo("America/New_York")
        except ImportError:
            et = timezone.utc
        hour = datetime.now(et).hour
        start, end = self.config.MAKER_MODE_HOURS_START, self.config.MAKER_MODE_HOURS_END
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    def _is_low_volatility(self, market) -> bool:
        """True if price_history stddev < 0.005 over last 10 ticks."""
        hist = getattr(market, "price_history", []) or []
        if len(hist) < 10:
            return False
        prices = [t.price for t in hist[-10:]]
        import statistics
        return statistics.stdev(prices) < 0.005

    async def maker_loop(self):
        """Strategy 4: Maker market making during low-volatility hours."""
        while self.running:
            try:
                await asyncio.sleep(60)
                if not self.config.MAKER_MODE_ENABLED or not self._is_maker_hours():
                    continue
                markets = await self.scanner.fetch_active_15min_markets()
                for market in markets:
                    q = (market.question or "").lower()
                    if "bitcoin" not in q and "btc" not in q and "ethereum" not in q and "eth" not in q:
                        continue
                    if self.position_manager.has_position(market.condition_id):
                        continue
                    if market.condition_id in self.maker_positions:
                        entry = self.maker_positions[market.condition_id]
                        placed_at = entry.get("placed_at") or 0
                        if (datetime.now(timezone.utc).timestamp() - placed_at) > 300:
                            del self.maker_positions[market.condition_id]
                        continue
                    if not self._is_low_volatility(market):
                        continue
                    yes_id, no_id = await self.executor.place_maker_pair(market)
                    if yes_id and no_id:
                        self.maker_positions[market.condition_id] = {
                            "yes_id": yes_id,
                            "no_id": no_id,
                            "placed_at": datetime.now(timezone.utc).timestamp(),
                        }
            except Exception as e:
                logger.error(f"Maker loop error: {e}", exc_info=True)

    async def scan_loop(self):
        num_markets = 0
        while self.running:
            try:
                # Clear BTC signal state if expired
                ts = self.btc_signal_state.get("timestamp")
                if ts:
                    elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
                    if elapsed > self.config.ETH_LAG_EXPIRY_SECONDS:
                        self.btc_signal_state["fired"] = False

                markets = await self.scanner.fetch_active_15min_markets()
                num_markets = len(markets)
                logger.info(f"Scanned {num_markets} active 15-min markets")

                # Orphan reconciliation (periodic)
                now_ts = time.time()
                if (now_ts - (self._last_orphan_reconcile or 0)) >= self.config.ORPHAN_RECONCILE_INTERVAL_SECONDS:
                    try:
                        orphans = await self.orphan_handler.reconcile(markets, add_orphans=True)
                        if orphans:
                            logger.info(f"Orphan reconcile: {len(orphans)} orphan(s) found and recovered")
                        self._last_orphan_reconcile = now_ts
                    except Exception as e:
                        logger.warning(f"Orphan reconcile failed: {e}")

                # Process BTC first to update btc_signal_state for ETH lag
                btc_markets = [m for m in markets if self._asset(m.question) == "BTC"]
                other_markets = [m for m in markets if m not in btc_markets]
                for market in btc_markets + other_markets:
                    asset = self._asset(market.question)
                    if self.position_manager.has_position(market.condition_id):
                        continue
                    if self.position_manager.at_capacity():
                        logger.debug("At max capacity — skipping new entries")
                        break

                    edge_result = await self.strategy_router.route(
                        market,
                        self.binance_feed,
                        self.btc_signal_state,
                    )

                    if asset == "BTC" and edge_result.has_edge:
                        pct = self.binance_feed.get_pct_move_from_window_open("BTC")
                        if pct is not None and abs(pct) >= self.config.ETH_LAG_MIN_BTC_MOVE:
                            self.btc_signal_state["fired"] = True
                            self.btc_signal_state["side"] = edge_result.side
                            self.btc_signal_state["pct_move"] = pct
                            self.btc_signal_state["timestamp"] = datetime.now(timezone.utc)
                            self.btc_signal_state["window_open_price"] = (
                                self.binance_feed.get_window_open_price("BTC") or 0
                            )

                    actually_entered = False
                    if edge_result.has_edge:
                        if self.position_manager.would_exceed_portfolio_risk(edge_result.kelly_size):
                            logger.debug(
                                f"Portfolio risk cap — would exceed {self.config.MAX_PORTFOLIO_RISK:.0%} "
                                f"(current: ${self.position_manager.total_open_exposure_usdc():.0f})"
                            )
                        else:
                            logger.info(
                                f"EDGE FOUND [{edge_result.strategy_name or 'BASE'}] — {market.question[:60]} | "
                                f"Side: {edge_result.side} | "
                                f"Signals: {edge_result.signal_count} | "
                                f"Kelly size: ${edge_result.kelly_size:.2f}"
                            )
                            order_id = await self.executor.place_order(market, edge_result)
                            if order_id:
                                self.position_manager.add_position(market, edge_result)
                                actually_entered = True
                            else:
                                logger.warning("Order failed — position not added")
                    self._append_signal_feed(market, edge_result, entered=actually_entered)
                    if not edge_result.has_edge:
                        logger.debug(
                            f"No edge — {market.question[:50]} | "
                            f"Signals: {edge_result.signal_count}"
                        )

            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)

            write_state(
                self.position_manager,
                self.signal_feed,
                self.config.BANKROLL,
                running=self.running,
                start_time=self.start_time,
                paper_trading=self.config.PAPER_TRADING,
                btc_signal_state=self.btc_signal_state,
                markets_last_scan=num_markets,
                maker_active=self.config.MAKER_MODE_ENABLED and self._is_maker_hours(),
            )
            await asyncio.sleep(self.config.SCAN_INTERVAL_SECONDS)

    def _asset(self, question: str) -> str:
        q = (question or "").lower()
        if "bitcoin" in q or "btc" in q:
            return "BTC"
        if "ethereum" in q or "eth" in q:
            return "ETH"
        if "solana" in q or "sol" in q:
            return "SOL"
        if "xrp" in q or "ripple" in q:
            return "XRP"
        return "UNKNOWN"

    def _append_signal_feed(self, market, edge_result, entered: bool):
        """Append an EdgeResult evaluation to signal_feed for dashboard display."""
        self.signal_feed.append({
            "market": market.question[:80],
            "question": market.question[:80],  # Alias for UI compatibility
            "ob_imbalance_signal": edge_result.ob_imbalance_signal,
            "momentum_signal": edge_result.momentum_signal,
            "volume_signal": edge_result.volume_signal,
            "kelly_signal": edge_result.kelly_signal,
            "eth_lag_signal": getattr(edge_result, "eth_lag_signal", False),
            "sol_squeeze_signal": getattr(edge_result, "sol_squeeze_signal", False),
            "xrp_catalyst_signal": getattr(edge_result, "xrp_catalyst_signal", False),
            "signal_count": edge_result.signal_count,
            "side": edge_result.side.value if edge_result.side else None,
            "kelly_size": round(edge_result.kelly_size, 2),
            "kelly_edge": round(edge_result.kelly_edge, 4),
            "entered": entered,
            "strategy_name": edge_result.strategy_name or "",
            "asset": getattr(edge_result, "asset", "") or self._asset(market.question),
            "pct_move_from_open": round(getattr(edge_result, "pct_move_from_open", 0) * 100, 2),
            "spot_price": getattr(edge_result, "spot_price", 0),
            "rsi_value": getattr(edge_result, "rsi_value", 0),
        })

    def shutdown(self):
        logger.info("Shutdown signal received — closing positions and exiting...")
        self.running = False
        write_state(
            self.position_manager,
            self.signal_feed,
            self.config.BANKROLL,
            running=False,
            start_time=self.start_time,
            paper_trading=self.config.PAPER_TRADING,
        )
        # Positions closed in _cleanup() when run() exits


if __name__ == "__main__":
    config = BotConfig()
    bot = PolymarketBot(config)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.shutdown()
