"""
state_writer.py — Writes bot state to bot_state.json for dashboard consumption.
Call write_state() from the main scan loop after each cycle.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from position_manager import PositionManager

logger = logging.getLogger("state_writer")
STATE_FILE = Path("bot_state.json")


def write_state(
    position_manager: "PositionManager",
    signal_feed: list,
    bankroll: float,
    running: bool = True,
    start_time: datetime = None,
    paper_trading: bool = True,
    btc_signal_state: dict = None,
    markets_last_scan: int = 0,
    maker_active: bool = False,
):
    """
    Write current bot state to bot_state.json.
    The dashboard server reads this file every 2 seconds.
    """
    try:
        positions_data = []
        for pos in position_manager.positions.values():
            if pos.is_open:
                positions_data.append({
                    "condition_id": pos.condition_id,
                    "question": pos.question,
                    "side": pos.side.value,
                    "token_id": pos.token_id,
                    "entry_price": pos.entry_price,
                    "current_price": pos.entry_price,  # Updated by live price fetch
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
        bot_activity["maker_active"] = maker_active

        state = {
            "running": running,
            "paper_trading": paper_trading,
            "open_positions": positions_data,
            "bankroll": bankroll,
            "uptime_seconds": uptime,
            "signal_feed": signal_feed[-50:],  # Keep last 50 evaluations (incl. strategy_name)
            "bot_activity": bot_activity,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        with open(STATE_FILE, "w") as f:
            json.dump(state, f, default=str)

    except Exception as e:
        logger.warning(f"State write failed: {e}")
