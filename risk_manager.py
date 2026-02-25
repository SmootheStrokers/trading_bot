"""
risk_manager.py — Central risk controls for the Polymarket bot.

Implements:
  - Daily loss limit (hard stop at 20% of bankroll)
  - Per-trade max loss limit
  - Trade frequency tracking (avoids overtrading)
  - NO martingale or revenge trading logic
"""

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

if __package__:
    from config import BotConfig
else:
    from config import BotConfig

logger = logging.getLogger("risk_manager")


class RiskManager:
    """Manages all risk controls. Bot must check can_trade() before each entry."""

    def __init__(self, config: BotConfig):
        self.config = config
        self._daily_pnl: float = 0.0
        self._daily_reset_date: Optional[str] = None
        self._trades_this_hour: int = 0
        self._last_hour_reset: Optional[datetime] = None
        self._consecutive_losses: int = 0  # For monitoring only — no revenge logic
        self._trading_paused: bool = False
        self._pause_reason: str = ""

    def _get_trade_log_path(self) -> Path:
        try:
            return Path(self.config.TRADE_LOG_FILE)
        except Exception:
            return Path("trades.csv")

    def _load_daily_pnl(self, bankroll: float) -> float:
        """Compute today's P&L from trades.csv. Reset at midnight UTC."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self._daily_reset_date == today and self._daily_pnl is not None:
            return self._daily_pnl

        self._daily_reset_date = today
        total = 0.0
        path = self._get_trade_log_path()
        if not path.exists():
            return 0.0
        try:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    exit_time = row.get("exit_time", "")
                    if not exit_time.strip():
                        continue
                    if not exit_time.startswith(today):
                        continue
                    pnl = row.get("pnl_usdc")
                    if pnl and str(pnl).strip():
                        total += float(pnl)
        except Exception as e:
            logger.warning(f"Could not load daily PnL: {e}")
        self._daily_pnl = total
        return total

    def _reset_hourly_count_if_needed(self):
        now = datetime.now(timezone.utc)
        if self._last_hour_reset is None:
            self._last_hour_reset = now
            self._trades_this_hour = 0
            return
        elapsed = (now - self._last_hour_reset).total_seconds()
        if elapsed >= 3600:
            self._last_hour_reset = now
            self._trades_this_hour = 0

    def record_trade_open(self):
        """Call when a new position is opened (for frequency limiting)."""
        self._reset_hourly_count_if_needed()
        self._trades_this_hour += 1

    def record_trade_close(self, pnl: float):
        """Call when a position closes. Updates daily PnL and consecutive losses."""
        self._daily_pnl += pnl
        if pnl <= 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

    def can_trade(self, bankroll: float, proposed_size_usdc: float) -> Tuple[bool, str]:
        """
        Returns (True, "") if trading is allowed, (False, reason) otherwise.
        Checks: daily loss limit, max trades/hour, trading paused.
        """
        if self._trading_paused:
            return False, f"Trading paused: {self._pause_reason}"

        daily_pnl = self._load_daily_pnl(bankroll)
        loss_limit = bankroll * (getattr(self.config, "DAILY_LOSS_LIMIT_PCT", 0.20))
        if daily_pnl <= -loss_limit:
            self._trading_paused = True
            self._pause_reason = f"Daily loss limit reached (${abs(daily_pnl):.2f} >= ${loss_limit:.2f})"
            logger.warning(f"RISK: {self._pause_reason} — all trading paused until next day")
            return False, self._pause_reason

        max_per_hour = getattr(self.config, "MAX_TRADES_PER_HOUR", 20)
        self._reset_hourly_count_if_needed()
        if self._trades_this_hour >= max_per_hour:
            return False, f"Max trades per hour ({max_per_hour}) reached"

        # Per-trade max loss: in binary markets, max loss = position size
        per_trade_limit_pct = getattr(self.config, "PER_TRADE_MAX_LOSS_PCT", 0.10)
        max_size = bankroll * per_trade_limit_pct
        if proposed_size_usdc > max_size:
            return False, f"Proposed size ${proposed_size_usdc:.2f} exceeds per-trade limit (${max_size:.2f})"

        return True, ""

    def get_daily_pnl(self, bankroll: float) -> float:
        """Current day's realized P&L."""
        return self._load_daily_pnl(bankroll)

    def get_daily_loss_limit(self, bankroll: float) -> float:
        """Max allowed daily loss in USDC."""
        return bankroll * getattr(self.config, "DAILY_LOSS_LIMIT_PCT", 0.20)

    def get_consecutive_losses(self) -> int:
        return self._consecutive_losses

    def get_trades_this_hour(self) -> int:
        self._reset_hourly_count_if_needed()
        return self._trades_this_hour

    def is_paused(self) -> bool:
        return self._trading_paused

    def reset_daily_at_midnight(self):
        """Call at midnight UTC to allow trading again after daily loss pause."""
        today = datetime.now(timezone.utc).date().isoformat()
        if self._daily_reset_date and self._daily_reset_date != today:
            self._trading_paused = False
            self._pause_reason = ""
            self._daily_pnl = 0.0
            self._daily_reset_date = today
            logger.info("RiskManager: new day — daily counters reset")

    def get_max_position_size(self, bankroll: float) -> float:
        """Max USDC per trade (per-trade loss limit). Capped by config MAX_BET_SIZE."""
        per_trade_pct = getattr(self.config, "PER_TRADE_MAX_LOSS_PCT", 0.10)
        cap_from_risk = bankroll * per_trade_pct
        return min(cap_from_risk, self.config.MAX_BET_SIZE)

    def get_state(self, bankroll: float) -> dict:
        """For dashboard: current risk state."""
        daily_pnl = self.get_daily_pnl(bankroll)
        loss_limit = self.get_daily_loss_limit(bankroll)
        return {
            "daily_pnl": round(daily_pnl, 2),
            "daily_loss_limit_usdc": round(loss_limit, 2),
            "trades_this_hour": self.get_trades_this_hour(),
            "trading_paused": self._trading_paused,
            "pause_reason": self._pause_reason,
            "consecutive_losses": self._consecutive_losses,
        }
