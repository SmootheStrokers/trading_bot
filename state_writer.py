"""
state_writer.py — Writes bot state to bot_state.json for dashboard consumption.
Call write_state() from the main scan loop after each cycle.
Tracks starting_bankroll and computes current bankroll from trades.
"""

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from position_manager import PositionManager

logger = logging.getLogger("state_writer")
# Use project root so bot and dashboard server read same file
_BASE = Path(__file__).resolve().parent
STATE_FILE = _BASE / "bot_state.json"
SESSION_START_FILE = _BASE / "session_start.json"


def _trades_csv_path() -> Path:
    try:
        from config import BotConfig
        p = Path(BotConfig().TRADE_LOG_FILE)
        return p if p.is_absolute() else _BASE / p
    except Exception:
        return _BASE / "trades.csv"


def _read_starting_bankroll(initial: float) -> float:
    """Read starting bankroll from session file; create if missing."""
    if not SESSION_START_FILE.exists():
        try:
            SESSION_START_FILE.write_text(json.dumps({
                "starting_bankroll": initial,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
        except Exception:
            pass
        return initial
    try:
        data = json.loads(SESSION_START_FILE.read_text())
        return float(data.get("starting_bankroll", initial))
    except Exception:
        return initial


def _compute_bankroll_from_trades(starting: float) -> float:
    """Sum P&L from trades.csv and add to starting bankroll."""
    trades_path = _trades_csv_path()
    if not trades_path.exists():
        return starting
    try:
        total_pnl = 0.0
        with open(trades_path, newline="") as f:
            for row in csv.DictReader(f):
                pnl = row.get("pnl_usdc")
                if pnl and str(pnl).strip():
                    try:
                        total_pnl += float(pnl)
                    except (ValueError, TypeError):
                        pass
        return starting + total_pnl
    except Exception:
        return starting


def write_state(
    position_manager: "PositionManager",
    signal_feed: list,
    bankroll: float,
    running: bool = True,
    start_time: datetime = None,
    paper_trading: bool = True,
    btc_signal_state: dict = None,
    markets_last_scan: int = 0,
    markets_with_edge: int = 0,
    maker_active: bool = False,
    risk_state: dict = None,
    daily_pnl: float = None,
    market_prices: dict = None,
):
    """
    Write current bot state to bot_state.json.
    The dashboard server reads this file every 2 seconds.
    """
    try:
        starting_bankroll = _read_starting_bankroll(bankroll)
        # In paper mode, derive current from trades; otherwise use passed bankroll
        if paper_trading:
            bankroll = _compute_bankroll_from_trades(starting_bankroll)

        positions_data = []
        for pos in position_manager.positions.values():
            if pos.is_open:
                # Use live current_price from position monitor when available
                curr_price = getattr(pos, "current_price", None)
                if curr_price is None:
                    curr_price = pos.entry_price
                positions_data.append({
                    "condition_id": pos.condition_id,
                    "question": pos.question,
                    "side": pos.side.value,
                    "token_id": pos.token_id,
                    "entry_price": pos.entry_price,
                    "current_price": curr_price,
                    "shares": pos.shares,
                    "size_usdc": pos.size_usdc,
                    "entry_time": pos.entry_time.isoformat() if pos.entry_time else None,
                    "seconds_remaining": pos.seconds_remaining,
                    "strategy_name": getattr(pos, "strategy_name", None) or "",
                })

        uptime = 0
        if start_time:
            now = datetime.now(timezone.utc)
            uptime = int((now - start_time).total_seconds())

        # Bot activity: what the bot is doing right now (for dashboard)
        bot_activity = {}
        if btc_signal_state:
            bot_activity["btc_signal_fired"] = btc_signal_state.get("fired", False)
            bot_activity["btc_signal_side"] = btc_signal_state.get("side")
            bot_activity["btc_pct_move"] = btc_signal_state.get("pct_move", 0)
            if bot_activity.get("btc_signal_side"):
                bot_activity["btc_signal_side"] = getattr(
                    bot_activity["btc_signal_side"], "value", bot_activity["btc_signal_side"]
                )
        bot_activity["markets_last_scan"] = markets_last_scan
        bot_activity["markets_with_edge"] = markets_with_edge
        bot_activity["maker_active"] = maker_active
        recent_signals = signal_feed[-10:] if signal_feed else []
        bot_activity["eth_lag_active"] = any(
            (s.get("eth_lag_signal") if isinstance(s, dict) else getattr(s, "eth_lag_signal", False))
            for s in recent_signals
        )

        if risk_state:
            bot_activity["risk_state"] = risk_state
        if daily_pnl is not None:
            bot_activity["daily_pnl"] = round(daily_pnl, 2)

        state = {
            "running": running,
            "paper_trading": paper_trading,
            "open_positions": positions_data,
            "bankroll": round(bankroll, 2),
            "starting_bankroll": round(starting_bankroll, 2),
            "uptime_seconds": uptime,
            "signal_feed": signal_feed[-50:],
            "bot_activity": bot_activity,
            "market_prices": {
                "btc_usd": round(market_prices.get("BTC", 0), 2) if market_prices and market_prices.get("BTC") else None,
                "eth_usd": round(market_prices.get("ETH", 0), 2) if market_prices and market_prices.get("ETH") else None,
                "sol_usd": round(market_prices.get("SOL", 0), 2) if market_prices and market_prices.get("SOL") else None,
                "xrp_usd": round(market_prices.get("XRP", 0), 2) if market_prices and market_prices.get("XRP") else None,
            } if market_prices else {},
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        with open(STATE_FILE, "w") as f:
            json.dump(state, f, default=str)

    except Exception as e:
        logger.warning(f"State write failed: {e}")
