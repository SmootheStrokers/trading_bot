"""
Polymarket 15-Minute Up/Down Trading Bot
Core philosophy: profit edge or no trade.
"""

import asyncio
import json
import logging

import aiohttp
import os
import signal
import sys
from datetime import datetime, timezone
import time
from pathlib import Path
from typing import Optional

from config import BotConfig
from auth import ensure_clob_creds
from clob_client import ClobClient
from market_scanner import MarketScanner
from edge_filter import EdgeFilter
from executor import OrderExecutor
from position_manager import PositionManager
from orphan_handler import OrphanHandler
from risk_manager import RiskManager
from state_writer import write_state
from logger import setup_logger
from binance_feed import BinanceFeed
from strategy_router import StrategyRouter
from models import EdgeResult, Side

# Configure logging early — use config.LOG_FILE so dashboard reads same file as terminal
config = BotConfig()
logger = setup_logger("main", log_file=config.LOG_FILE)


async def _check_geoblock() -> None:
    """Check if current IP is geoblocked by Polymarket. Exit with clear message if blocked."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://polymarket.com/api/geoblock", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                if data.get("blocked"):
                    country = data.get("country", "your region")
                    logger.error(
                        f"Polymarket blocks trading from {country}. "
                        "See https://docs.polymarket.com/developers/CLOB/geoblock"
                    )
                    sys.exit(1)
    except Exception as e:
        logger.warning(f"Could not check geoblock status: {e}")


def _validate_private_key(cfg: BotConfig) -> None:
    """Validate POLY_PRIVATE_KEY before live trading. Exit with clear message if invalid."""
    key = (cfg.PRIVATE_KEY or "").strip()
    if not key:
        logger.error("POLY_PRIVATE_KEY is empty. Live trading requires your wallet's private key.")
        sys.exit(1)
    hex_part = key[2:] if key.lower().startswith("0x") else key
    if len(hex_part) != 64:
        hint = (
            "You may have entered your wallet ADDRESS (40 hex chars) instead of the private key. "
            "Export from MetaMask: Account details -> Export Private Key."
        ) if len(hex_part) == 40 else ""
        logger.error(
            f"POLY_PRIVATE_KEY must be 64 hex characters (32 bytes). Got {len(hex_part)}. {hint}"
        )
        sys.exit(1)
# Suppress websocket heartbeat spam in bot.log
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.client").setLevel(logging.WARNING)


class PolymarketBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.clob = ClobClient(config)
        self.scanner = MarketScanner(config, self.clob)
        self.edge_filter = EdgeFilter(config)
        self.strategy_router = StrategyRouter(config, self.edge_filter)
        self.executor = OrderExecutor(config, self.clob)
        self.risk_manager = RiskManager(config)
        self.position_manager = PositionManager(
            config, self.clob, self.executor,
            stop_predicate=lambda: self.running,
            on_trade_close=lambda pnl: self.risk_manager.record_trade_close(pnl),
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
        self._last_num_markets = 0
        self._last_markets_with_edge = 0
        self._live_bankroll: Optional[float] = None
        self._starting_bankroll: Optional[float] = None

    async def _fetch_live_balance(self) -> Optional[float]:
        """Fetch USDC balance from Polymarket CLOB. Returns None on failure."""
        try:
            resp = await self.clob.get_balance()
            val = None
            if isinstance(resp, (int, float)):
                val = float(resp)
            elif isinstance(resp, dict):
                val = resp.get("balance") or resp.get("usdc") or resp.get("available")
                if val is None and "balances" in resp:
                    bals = resp["balances"]
                    if isinstance(bals, list) and bals:
                        b = bals[0]
                        val = b.get("currentBalance") or b.get("buyingPower") or b.get("assetAvailable")
                if val is not None:
                    val = float(val)
            return round(val, 2) if val is not None else None
        except Exception as e:
            logger.warning(f"Could not fetch live balance: {e}")
            return None

    def get_effective_bankroll(self) -> float:
        """Bankroll to use for sizing/risk. Live mode: Polymarket balance. Paper: from trades or config."""
        if not self.config.PAPER_TRADING and not getattr(self.config, "DRY_RUN", False):
            if self._live_bankroll is not None:
                return self._live_bankroll
            return self.config.BANKROLL
        try:
            from state_writer import _compute_bankroll_from_trades, _read_starting_bankroll
            starting = _read_starting_bankroll(self.config.BANKROLL)
            return _compute_bankroll_from_trades(starting)
        except Exception:
            return self.config.BANKROLL

    async def run(self):
        logger.info("=" * 60)
        logger.info("  Polymarket 15-Min Up/Down Bot — STARTING")
        if self.config.PAPER_TRADING:
            logger.info("  *** PAPER TRADING MODE — No real orders will be placed ***")
        elif getattr(self.config, "DRY_RUN", False):
            logger.info("  *** DRY RUN — No real orders will be placed ***")
        else:
            logger.info("  *** LIVE TRADING — Real money orders will be placed ***")
        logger.info(f"  Min edge signals required: {self.config.MIN_EDGE_SIGNALS}")
        logger.info(f"  Max concurrent positions:  {self.config.MAX_POSITIONS}")
        logger.info(f"  Bankroll (config):          ${self.config.BANKROLL:.2f}")
        logger.info("=" * 60)

        self.running = True
        self.start_time = datetime.now(timezone.utc)
        try:
            await self.clob.start()
            await self.binance_feed.start()

            # Live mode: fetch Polymarket balance and use it (ignores BANKROLL from env)
            if not self.config.PAPER_TRADING and not getattr(self.config, "DRY_RUN", False):
                bal = await self._fetch_live_balance()
                if bal is not None:
                    self._live_bankroll = bal
                    self._starting_bankroll = bal
                    logger.info(f"  Live Polymarket balance:    ${bal:.2f} (using for all sizing)")
                else:
                    logger.warning("  Could not fetch Polymarket balance — using BANKROLL from config")

            # Persist session start for dashboard
            _session_start_path = Path("session_start.json")
            try:
                start_br = self._starting_bankroll or self.config.BANKROLL
                _session_start_path.write_text(json.dumps({
                    "starting_bankroll": start_br,
                    "started_at": self.start_time.isoformat(),
                }, indent=2))
            except Exception:
                pass

            # Validate credentials when live trading (fail fast with clear message)
            if not self.config.PAPER_TRADING and not getattr(self.config, "DRY_RUN", False):
                _validate_private_key(self.config)
                await _check_geoblock()

            # One-time force test trade (FORCE_TEST_TRADE=true) to verify full pipeline
            if os.getenv("FORCE_TEST_TRADE", "false").lower() in ("true", "1", "yes"):
                await self._force_test_trade_once()

            if sys.platform != "win32":
                loop = asyncio.get_event_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.shutdown)

            await asyncio.gather(
                self.scan_loop(),
                self.maker_loop(),
                self.position_manager.monitor_loop(),
                self.catalyst_watcher(),
                self.report_scheduler_loop(),
                self._state_writer_loop(),
            )
        finally:
            await self._cleanup()

    async def _cleanup(self):
        """Close positions on restart only if CLOSE_ON_RESTART=true; otherwise leave open for resume."""
        if self.config.CLOSE_ON_RESTART:
            await self.position_manager.close_all_async()
        else:
            logger.info("CLOSE_ON_RESTART=false — leaving positions open for resume")
        await self.binance_feed.stop()
        await self.clob.close()

    async def report_scheduler_loop(self):
        """Run daily report at 11:59 PM UTC (or REPORT_SEND_TIME_UTC). Weekly report on Sundays."""
        if not getattr(self.config, "DAILY_REPORT_ENABLED", True):
            return
        send_time = getattr(self.config, "REPORT_SEND_TIME_UTC", "23:59") or "23:59"
        try:
            parts = send_time.replace(":", " ").split()
            h = int(parts[0]) if parts else 23
            m = int(parts[1]) if len(parts) > 1 else 59
        except Exception:
            h, m = 23, 59
        weekly_day = getattr(self.config, "WEEKLY_REPORT_DAY", "sunday").lower()
        weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        weekly_weekday = weekdays.index(weekly_day) if weekly_day in weekdays else 6  # Sunday=6

        while self.running:
            try:
                now = datetime.now(timezone.utc)
                # Check if we're within the report minute (23:59)
                if now.hour == h and now.minute == m:
                    logger.info("Running scheduled daily report...")
                    try:
                        from daily_report import run_daily_report, run_weekly_report
                        run_daily_report(send_email_flag=True, send_discord_flag=True)
                        if now.weekday() == weekly_weekday:
                            run_weekly_report()
                            logger.info("Weekly report generated")
                    except Exception as e:
                        logger.error(f"Report generation failed: {e}", exc_info=True)
                    await asyncio.sleep(61)  # Avoid running twice in same minute
                else:
                    await asyncio.sleep(30)  # Check every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Report scheduler error: {e}", exc_info=True)
                await asyncio.sleep(60)

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

    async def _state_writer_loop(self):
        """Write bot_state.json every 8 seconds for dashboard real-time updates."""
        while self.running:
            try:
                await asyncio.sleep(8)
                if not self.running:
                    break
                bankroll_for_state = self.get_effective_bankroll()
                risk_state = self.risk_manager.get_state(bankroll_for_state)
                write_state(
                    self.position_manager,
                    self.signal_feed,
                    bankroll_for_state,
                    running=self.running,
                    start_time=self.start_time,
                    paper_trading=self.config.PAPER_TRADING,
                    btc_signal_state=self.btc_signal_state,
                    markets_last_scan=self._last_num_markets,
                    maker_active=self.config.MAKER_MODE_ENABLED and self._is_maker_hours(),
                    markets_with_edge=self._last_markets_with_edge,
                    risk_state=risk_state,
                    daily_pnl=risk_state.get("daily_pnl"),
                    market_prices=getattr(self.binance_feed, "latest_prices", None) or {},
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"State writer loop: {e}")

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
                # Live mode: refresh Polymarket balance each scan
                if not self.config.PAPER_TRADING and not getattr(self.config, "DRY_RUN", False):
                    bal = await self._fetch_live_balance()
                    if bal is not None:
                        self._live_bankroll = bal

                # Clear BTC signal state if expired
                ts = self.btc_signal_state.get("timestamp")
                if ts:
                    elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
                    if elapsed > self.config.ETH_LAG_EXPIRY_SECONDS:
                        self.btc_signal_state["fired"] = False

                markets = await self.scanner.fetch_active_15min_markets()
                num_markets = len(markets)
                self._last_num_markets = num_markets
                markets_with_edge = 0
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
                    can_trade = True  # Reset per market; risk checks may set False
                    asset = self._asset(market.question)
                    if self.position_manager.has_position(market.condition_id):
                        logger.info(f"[{asset}] GATE: has_position (condition_id={market.condition_id[:16]}...) — SKIP {market.question[:40]}")
                        continue
                    if self.position_manager.at_capacity():
                        logger.info(f"[{asset}] GATE: at_capacity (max {self.config.MAX_POSITIONS}) — SKIP")
                        break

                    bankroll = self.get_effective_bankroll()
                    edge_result = await self.strategy_router.route(
                        market,
                        self.binance_feed,
                        self.btc_signal_state,
                        bankroll=bankroll,
                    )

                    # Risk checks (only when edge found): daily loss limit, per-trade limit
                    can_trade = True
                    if edge_result.has_edge:
                        self.risk_manager.reset_daily_at_midnight()
                        capped_size = min(
                            edge_result.kelly_size,
                            self.risk_manager.get_max_position_size(bankroll),
                        )
                        can_trade, risk_reason = self.risk_manager.can_trade(bankroll, capped_size)
                        # After loss streak, require higher edge to avoid revenge trading
                        loss_streak = self.risk_manager.get_consecutive_losses()
                        min_edge_boost = getattr(self.config, "LOSS_STREAK_REQUIRE_HIGHER_EDGE", 2)
                        if can_trade and loss_streak >= min_edge_boost:
                            extra_edge = 0.02
                            if edge_result.kelly_edge < max(self.config.MIN_KELLY_EDGE, getattr(self.config, "MIN_EDGE_PCT", 0.03)) + extra_edge:
                                can_trade = False
                                logger.warning(f"Loss streak {loss_streak} — requiring +{extra_edge:.0%} extra edge, skipping")
                        if not can_trade and risk_reason:
                            logger.info(f"[{asset}] GATE: risk_blocked — {risk_reason}")
                        else:
                            edge_result.kelly_size = capped_size
                            if edge_result.kelly_size < self.config.MIN_BET_SIZE:
                                can_trade = False
                                logger.info(f"[{asset}] GATE: kelly_size ${edge_result.kelly_size:.2f} < MIN ${self.config.MIN_BET_SIZE} — SKIP")

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
                    if edge_result.has_edge and can_trade:
                        if self.position_manager.would_exceed_portfolio_risk(edge_result.kelly_size, bankroll):
                            logger.info(
                                f"[{asset}] GATE: portfolio_risk — would exceed {self.config.MAX_PORTFOLIO_RISK:.0%} "
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
                                self.risk_manager.record_trade_open()
                                actually_entered = True
                            else:
                                logger.warning("Order failed — position not added")
                    if edge_result.has_edge:
                        markets_with_edge += 1
                    self._append_signal_feed(market, edge_result, entered=actually_entered)
                self._last_markets_with_edge = markets_with_edge

            except Exception as e:
                logger.error(f"Scan loop error: {e}", exc_info=True)

            bankroll_for_state = self.get_effective_bankroll()
            risk_state = self.risk_manager.get_state(bankroll_for_state)
            write_state(
                self.position_manager,
                self.signal_feed,
                bankroll_for_state,
                running=self.running,
                start_time=self.start_time,
                paper_trading=self.config.PAPER_TRADING,
                btc_signal_state=self.btc_signal_state,
                markets_last_scan=num_markets,
                maker_active=self.config.MAKER_MODE_ENABLED and self._is_maker_hours(),
                markets_with_edge=markets_with_edge,
                risk_state=risk_state,
                daily_pnl=risk_state.get("daily_pnl"),
                market_prices=getattr(self.binance_feed, "latest_prices", None) or {},
            )
            await asyncio.sleep(self.config.SCAN_INTERVAL_SECONDS)

    async def _force_test_trade_once(self):
        """Force a single paper trade to verify full pipeline: scan → edge → order → position → trades.csv."""
        logger.info("=" * 60)
        logger.info("  FORCE TEST TRADE — placing 1 paper trade on highest-volume market")
        logger.info("=" * 60)
        try:
            markets = await self.scanner.fetch_active_15min_markets()
            if not markets:
                logger.warning("Force test: no markets found — cannot place test trade")
                return
            # Pick highest volume/liquidity market
            market = max(markets, key=lambda m: (m.total_depth_usdc or 0, m.recent_volume_usd or 0))
            mid = market.order_book.mid_price if market.order_book else 0.5
            if mid is None or mid <= 0:
                mid = 0.50
            side = Side.YES if mid <= 0.55 else Side.NO
            size = min(10.0, self.config.MAX_BET_SIZE)
            edge = EdgeResult(
                has_edge=True,
                side=side,
                signal_count=2,
                ob_imbalance_signal=True,
                kelly_signal=True,
                strategy_name="FORCE_TEST",
                asset=self._asset(market.question),
                kelly_edge=0.05,
                kelly_size=size,
                entry_price=mid,
                reason="Force test trade — pipeline verification",
            )
            if self.position_manager.has_position(market.condition_id):
                logger.warning("Force test: already have position in market — skipping")
                return
            order_id = await self.executor.place_order(market, edge)
            if order_id:
                self.position_manager.add_position(market, edge)
                self.risk_manager.record_trade_open()
                logger.info(f"*** FORCE TEST TRADE PLACED *** {market.question[:50]} | {side.value} ${size} | ID={order_id}")
                logger.info("  -> Check trades.csv and dashboard to confirm pipeline works")
            else:
                logger.error("Force test: order placement failed")
        except Exception as e:
            logger.error(f"Force test trade failed: {e}", exc_info=True)

    def _asset(self, question) -> str:
        """Extract asset (BTC/ETH/SOL/XRP) from question string or Market object."""
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
            self.get_effective_bankroll(),
            running=False,
            start_time=self.start_time,
            paper_trading=self.config.PAPER_TRADING,
        )
        # Positions closed in _cleanup() when run() exits


if __name__ == "__main__":
    # Live mode: always derive CLOB creds from POLY_PRIVATE_KEY. Builder Keys (from
    # polymarket.com/settings?tab=builder) are for attribution only and cause 401.
    live_mode = not config.PAPER_TRADING and not getattr(config, "DRY_RUN", False)
    if live_mode and not ensure_clob_creds(config, force_derive=True):
            logger.error(
                "Live trading requires CLOB API credentials. Set POLY_API_KEY/SECRET/PASSPHRASE, "
                "or leave them empty to derive from POLY_PRIVATE_KEY. "
                "Builder Keys do NOT work for CLOB auth — credentials must be derived from your wallet key."
            )
            sys.exit(1)
    bot = PolymarketBot(config)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        bot.shutdown()
