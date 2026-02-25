"""
models.py — Shared data structures used across all modules.
"""

from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    YES = "YES"
    NO = "NO"


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    yes_bids: List[OrderBookLevel] = field(default_factory=list)  # Buy YES
    yes_asks: List[OrderBookLevel] = field(default_factory=list)  # Sell YES
    timestamp: Optional[datetime] = None

    @property
    def best_yes_bid(self) -> Optional[float]:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_yes_ask(self) -> Optional[float]:
        return self.yes_asks[0].price if self.yes_asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_yes_bid and self.best_yes_ask:
            return (self.best_yes_bid + self.best_yes_ask) / 2
        return None

    @property
    def total_bid_depth(self) -> float:
        return sum(l.price * l.size for l in self.yes_bids)

    @property
    def total_ask_depth(self) -> float:
        return sum(l.price * l.size for l in self.yes_asks)


@dataclass
class PriceTick:
    price: float
    volume: float
    timestamp: datetime


@dataclass
class Market:
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    end_date_iso: str
    end_timestamp: float
    order_book: Optional[OrderBook] = None
    no_order_book: Optional[OrderBook] = None  # NO token book for imbalance cross-check
    price_history: List[PriceTick] = field(default_factory=list)
    asset: str = ""                      # "BTC", "ETH", "SOL", "XRP" — populated by scanner
    window_open_price: float = 0.0       # Binance spot price at window open — from BinanceFeed

    @property
    def seconds_remaining(self) -> float:
        return self.end_timestamp - datetime.now(timezone.utc).timestamp()

    @property
    def total_depth_usdc(self) -> float:
        """Total order book depth in USDC (bids + asks)."""
        if not self.order_book:
            return 0.0
        return self.order_book.total_bid_depth + self.order_book.total_ask_depth

    @property
    def recent_volume_usd(self) -> float:
        """Sum of (price × volume) over last 20 price ticks. High-volume markets preferred."""
        if not self.price_history or len(self.price_history) < 2:
            return 0.0
        recent = self.price_history[-20:]
        return sum(t.price * t.volume for t in recent)


@dataclass
class EdgeResult:
    has_edge: bool
    side: Optional[Side]
    signal_count: int

    # Individual signal results
    ob_imbalance_signal: bool = False
    momentum_signal: bool = False
    volume_signal: bool = False
    kelly_signal: bool = False

    # Strategy identification
    strategy_name: str = ""          # "BTC_MOMENTUM", "ETH_LAG", "SOL_SQUEEZE", "MAKER", "XRP_CATALYST"
    asset: str = ""                  # "BTC", "ETH", "SOL", "XRP"

    # New signal results
    eth_lag_signal: bool = False     # Strategy 2
    sol_squeeze_signal: bool = False # Strategy 3
    xrp_catalyst_signal: bool = False # Strategy 5

    # Live price data at evaluation time
    spot_price: float = 0.0          # Binance spot price at evaluation
    pct_move_from_open: float = 0.0  # % move from window open price
    funding_rate: float = 0.0        # SOL funding rate (0.0 for other assets)
    rsi_value: float = 0.0           # RSI at evaluation time (SOL primarily)

    # Kelly sizing
    estimated_prob: float = 0.0
    implied_prob: float = 0.0
    kelly_edge: float = 0.0
    kelly_size: float = 0.0

    # Entry details
    entry_price: float = 0.0
    reason: str = ""


@dataclass
class Position:
    condition_id: str
    question: str
    side: Side
    token_id: str
    entry_price: float
    size_usdc: float
    shares: float
    entry_time: datetime
    end_timestamp: float
    order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: Optional[float] = None
    strategy_name: str = ""          # For trades.csv "strategy" column
    current_price: Optional[float] = None  # Live price from CLOB (for dashboard)

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    @property
    def seconds_remaining(self) -> float:
        return self.end_timestamp - datetime.now(timezone.utc).timestamp()

    @property
    def current_value(self) -> Optional[float]:
        """Approximate current P&L given a live price."""
        return None  # Updated by position manager with live price
