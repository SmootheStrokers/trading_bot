"""
position_manager.py — Tracks open positions and manages exits.

Exit conditions (first to trigger wins):
  1. Take profit — price reaches TAKE_PROFIT_MULTIPLIER × entry
  2. Stop loss   — price drops to STOP_LOSS_THRESHOLD
  3. Time stop   — TIME_STOP_BUFFER_SECONDS before market resolves
"""

import asyncio
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional

from config import BotConfig
from clob_client import ClobClient
from executor import OrderExecutor
from models import Market, EdgeResult, Position, Side

logger = logging.getLogger("position_manager")


class PositionManager:
    def __init__(
        self,
        config: BotConfig,
        client: ClobClient,
        executor: OrderExecutor,
        stop_predicate: Optional[Callable[[], bool]] = None,
        on_trade_close: Optional[Callable[[float], None]] = None,
    ):
        self.config = config
        self.positions: Dict[str, Position] = {}  # condition_id → Position
        self.client = client
        self.executor = executor
        self._stop_predicate = stop_predicate  # callable returning True while running
        self._on_trade_close = on_trade_close  # callback(pnl) when position closes
        self._init_trade_log()

    # ── Public Interface ──────────────────────────────────────────────────────

    def has_position(self, condition_id: str) -> bool:
        """True only if we have an OPEN position in this market. Each 15-min window has a unique
        condition_id, so expired positions don't block new windows. Allows multiple positions
        per asset (e.g. BTC window 1, BTC window 2) across different market windows."""
        return condition_id in self.positions and self.positions[condition_id].is_open

    def at_capacity(self) -> bool:
        open_count = sum(1 for p in self.positions.values() if p.is_open)
        return open_count >= self.config.MAX_POSITIONS

    def total_open_exposure_usdc(self) -> float:
        """Total USDC at risk across all open positions (size_usdc)."""
        return sum(p.size_usdc for p in self.positions.values() if p.is_open)

    def would_exceed_portfolio_risk(self, additional_size_usdc: float) -> bool:
        """True if adding this size would exceed MAX_PORTFOLIO_RISK of bankroll."""
        cap = self.config.BANKROLL * self.config.MAX_PORTFOLIO_RISK
        current = self.total_open_exposure_usdc()
        return (current + additional_size_usdc) > cap

    def add_position(self, market: Market, edge: EdgeResult):
        token_id = (
            market.yes_token_id if edge.side == Side.YES
            else market.no_token_id
        )
        entry_price = edge.entry_price if edge.side == Side.YES else 1.0 - edge.entry_price
        shares = edge.kelly_size / entry_price if entry_price > 0 else 0

        pos = Position(
            condition_id=market.condition_id,
            question=market.question,
            side=edge.side,
            token_id=token_id,
            entry_price=entry_price,
            size_usdc=edge.kelly_size,
            shares=shares,
            entry_time=datetime.utcnow(),
            end_timestamp=market.end_timestamp,
            strategy_name=getattr(edge, "strategy_name", "") or "",
        )
        self.positions[market.condition_id] = pos
        logger.info(f"Position opened: {edge.side.value} {market.question[:50]}")
        self._log_trade_open(pos)

    def close_all(self):
        """Synchronous close-all (legacy). Prefer close_all_async for proper awaiting."""
        for pos in list(self.positions.values()):
            if pos.is_open:
                logger.warning(f"Emergency close: {pos.question[:50]}")
                asyncio.create_task(self._exit_position(pos, reason="SHUTDOWN"))

    async def close_all_async(self):
        """Await exit of all open positions (for clean shutdown)."""
        open_positions = [p for p in self.positions.values() if p.is_open]
        if not open_positions:
            return
        logger.info(f"Shutdown: closing {len(open_positions)} positions...")
        tasks = [self._exit_position(pos, reason="SHUTDOWN") for pos in open_positions]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Shutdown: all positions closed")

    def _should_keep_running(self) -> bool:
        """True while the monitor loop should continue."""
        if self._stop_predicate is None:
            return True
        try:
            return self._stop_predicate()
        except Exception:
            return True

    # ── Monitoring Loop ───────────────────────────────────────────────────────

    async def monitor_loop(self):
        """Continuously monitor open positions for exit conditions."""
        while self._should_keep_running():
            try:
                open_positions = [p for p in self.positions.values() if p.is_open]
                if open_positions:
                    await self._check_exits(open_positions)
            except Exception as e:
                logger.error(f"Position monitor error: {e}", exc_info=True)
            await asyncio.sleep(self.config.POLL_POSITIONS_INTERVAL)

    async def _check_exits(self, positions: list):
        tasks = [self._evaluate_position(pos) for pos in positions]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _evaluate_position(self, pos: Position):
        """Check a single position against exit conditions."""
        try:
            current_price = await self._get_current_price(pos.token_id)
            if current_price is None:
                return

            pos.current_price = current_price  # For dashboard display
            secs_left = pos.seconds_remaining

            # ── Exit 1: Time stop ─────────────────────────────────────────
            if secs_left <= self.config.TIME_STOP_BUFFER_SECONDS:
                logger.info(f"TIME STOP — {secs_left:.0f}s left | {pos.question[:50]}")
                await self._exit_position(pos, current_price, reason="TIME_STOP")
                return

            # ── Exit 2: Take profit ───────────────────────────────────────
            take_profit_price = pos.entry_price * self.config.TAKE_PROFIT_MULTIPLIER
            if current_price >= take_profit_price:
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                logger.info(
                    f"TAKE PROFIT +{pnl_pct:.1%} | "
                    f"entry={pos.entry_price:.3f} now={current_price:.3f} | "
                    f"{pos.question[:50]}"
                )
                await self._exit_position(pos, current_price, reason="TAKE_PROFIT")
                return

            # ── Exit 3: Stop loss ─────────────────────────────────────────
            if current_price <= self.config.STOP_LOSS_THRESHOLD:
                pnl_pct = (current_price - pos.entry_price) / pos.entry_price
                logger.warning(
                    f"STOP LOSS {pnl_pct:.1%} | "
                    f"entry={pos.entry_price:.3f} now={current_price:.3f} | "
                    f"{pos.question[:50]}"
                )
                await self._exit_position(pos, current_price, reason="STOP_LOSS")
                return

            # Still holding
            unrealized_pnl = (current_price - pos.entry_price) * pos.shares
            logger.debug(
                f"HOLD {pos.side.value} | price={current_price:.3f} | "
                f"PnL=${unrealized_pnl:+.2f} | {secs_left:.0f}s left"
            )

        except Exception as e:
            logger.error(f"Position eval error for {pos.condition_id}: {e}")

    async def _exit_position(
        self, pos: Position,
        exit_price: Optional[float] = None,
        reason: str = "UNKNOWN"
    ):
        """Place a sell order and mark position as closed."""
        min_sell_price = max((exit_price or pos.entry_price) * 0.97, 0.01)

        order_id = await self.executor.place_exit_order(
            token_id=pos.token_id,
            shares=pos.shares,
            min_price=min_sell_price,
            label=reason,
        )

        pos.exit_price = exit_price or pos.entry_price
        pos.exit_time = datetime.utcnow()
        pos.pnl = (pos.exit_price - pos.entry_price) * pos.shares

        logger.info(
            f"Position closed [{reason}] | PnL: ${pos.pnl:+.2f} | "
            f"{pos.question[:50]}"
        )
        self._log_trade_close(pos, reason)
        if self._on_trade_close and pos.pnl is not None:
            try:
                self._on_trade_close(pos.pnl)
            except Exception as e:
                logger.warning(f"on_trade_close callback error: {e}")

    async def _get_current_price(self, token_id: str) -> Optional[float]:
        try:
            resp = await self.client.get_last_trade_price(token_id)
            return float(resp.get("price", 0)) or None
        except Exception as e:
            logger.warning(f"Price fetch failed for {token_id}: {e}")
            return None

    # ── Trade Logging ─────────────────────────────────────────────────────────

    def _get_trade_log_path(self) -> Path:
        """Resolve trades.csv to project root so bot and dashboard read same file."""
        p = Path(self.config.TRADE_LOG_FILE)
        if p.is_absolute():
            return p
        return Path(__file__).resolve().parent / p.name

    def _init_trade_log(self):
        try:
            path = self._get_trade_log_path()
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                if f.tell() == 0:
                    writer.writerow([
                        "condition_id", "question", "side", "entry_price",
                        "exit_price", "size_usdc", "shares", "pnl_usdc",
                        "entry_time", "exit_time", "duration_seconds", "reason",
                        "strategy",
                    ])
        except Exception as e:
            logger.warning(f"Could not init trade log: {e}")

    def _log_trade_open(self, pos: Position):
        pass  # Entry logged at close for complete record

    def _log_trade_close(self, pos: Position, reason: str):
        try:
            duration = (
                (pos.exit_time - pos.entry_time).total_seconds()
                if pos.exit_time else None
            )
            path = self._get_trade_log_path()
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    pos.condition_id,
                    pos.question[:100],
                    pos.side.value,
                    f"{pos.entry_price:.4f}",
                    f"{pos.exit_price:.4f}" if pos.exit_price else "",
                    f"{pos.size_usdc:.2f}",
                    f"{pos.shares:.4f}",
                    f"{pos.pnl:.2f}" if pos.pnl is not None else "",
                    pos.entry_time.isoformat() if pos.entry_time else "",
                    pos.exit_time.isoformat() if pos.exit_time else "",
                    f"{duration:.0f}" if duration else "",
                    reason,
                    getattr(pos, "strategy_name", "") or "",
                ])
            # Log summary so user can confirm trades are recorded
            try:
                today = datetime.now(timezone.utc).date().isoformat()
                trades_today = 0
                today_pnl = 0.0
                if path.exists():
                    with open(path, newline="") as rf:
                        for row in csv.DictReader(rf):
                            et = row.get("exit_time", "")
                            if et.startswith(today):
                                trades_today += 1
                                p = row.get("pnl_usdc")
                                if p and str(p).strip():
                                    today_pnl += float(p)
                logger.info(
                    f"TRADE COMPLETE | Total trades today: {trades_today} | Session P&L: ${today_pnl:+.2f}"
                )
            except Exception as e:
                logger.debug(f"Trade summary failed: {e}")
        except Exception as e:
            logger.warning(f"Trade log write failed: {e}")

    # ── Stats ─────────────────────────────────────────────────────────────────

    def print_session_stats(self):
        closed = [p for p in self.positions.values() if not p.is_open]
        if not closed:
            logger.info("No closed trades this session.")
            return
        total_pnl = sum(p.pnl or 0 for p in closed)
        wins = [p for p in closed if (p.pnl or 0) > 0]
        losses = [p for p in closed if (p.pnl or 0) <= 0]
        win_rate = len(wins) / len(closed) * 100 if closed else 0
        logger.info(
            f"\n{'='*50}\n"
            f"  SESSION STATS\n"
            f"  Trades:   {len(closed)}\n"
            f"  Win rate: {win_rate:.1f}%\n"
            f"  Total PnL: ${total_pnl:+.2f}\n"
            f"{'='*50}"
        )
