"""
config.py — All tunable parameters in one place.
Edit this file to control the bot's behavior.
"""

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BotConfig:
    # ── Polymarket CLOB API ──────────────────────────────────────────────────
    CLOB_API_URL: str = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
    PRIVATE_KEY: str = os.getenv("POLY_PRIVATE_KEY", "")          # Wallet private key
    PROXY_WALLET: Optional[str] = os.getenv("PROXY_WALLET")       # For positions lookup (Data API)
    API_KEY: str = os.getenv("POLY_API_KEY", "")                   # CLOB API key
    API_SECRET: str = os.getenv("POLY_API_SECRET", "")
    API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")
    CHAIN_ID: int = int(os.getenv("CHAIN_ID", "137"))              # 137 = Polygon mainnet

    # ── Paper vs Live Trading ──────────────────────────────────────────────────
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() in ("true", "1", "yes")
    # When True: no real orders placed; simulates with paper balance. Use for testing.
    # Set PAPER_TRADING=false to enable real money trading.

    # ── Capital Management ───────────────────────────────────────────────────
    BANKROLL: float = float(os.getenv("BANKROLL", "1000.0"))       # Total capital in USDC
    MAX_KELLY_FRACTION: float = 0.25   # Cap Kelly bet at 25% of full Kelly (safety)
    MIN_BET_SIZE: float = 5.0          # Minimum order in USDC
    MAX_BET_SIZE: float = 100.0        # Hard cap per trade in USDC (legacy)
    MAX_POSITION_SIZE_USD: float = float(os.getenv("MAX_POSITION_SIZE_USD", "25.0"))  # Cap per trade ($20-25 = 2-2.5% of $1k)
    MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "20"))  # Allow ~$500/$25 = 20 positions at 50% risk
    MAX_PORTFOLIO_RISK: float = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "0.50"))  # 50% bankroll at risk across all positions

    # ── $1000/Day Goal & Risk Limits ─────────────────────────────────────────
    DAILY_PROFIT_GOAL_USD: float = float(os.getenv("DAILY_PROFIT_GOAL_USD", "1000.0"))
    DAILY_LOSS_LIMIT_PCT: float = 0.20  # Hard stop: pause all trading if daily loss >= 20% bankroll
    PER_TRADE_MAX_LOSS_PCT: float = 0.10  # Max 10% of bankroll per trade (caps position size)
    MAX_TRADES_PER_HOUR: int = 20       # Rate limit to avoid overtrading
    LOSS_STREAK_REQUIRE_HIGHER_EDGE: int = 2   # After N consecutive losses, require +2% edge
    POSITION_SIZING_MODE: str = os.getenv("POSITION_SIZING_MODE", "fractional_kelly")  # kelly | fractional_kelly | bankroll_pct
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))  # 0.25 = quarter-Kelly (conservative sizing)

    # ── Edge Filter (core profit gate) ──────────────────────────────────────
    MIN_EDGE_SIGNALS: int = int(os.getenv("MIN_EDGE_SIGNALS", "2"))  # Require 2 of 4 signals (was 3)
                                       # Signals: OB imbalance, momentum, volume, Kelly
    MIN_KELLY_EDGE: float = float(os.getenv("MIN_KELLY_EDGE", "0.02"))  # 2% Kelly edge (more trades)
    MIN_EDGE_PCT: float = float(os.getenv("MIN_EDGE_PCT", "0.02"))  # 2% min edge
    MIN_LIQUIDITY_USDC: float = 500.0  # Market must have at least $500 in order book
    MIN_MARKET_VOLUME_USD: float = float(os.getenv("MIN_MARKET_VOLUME_USD", "500.0"))  # $500 min (was 1000)
    BASE_KELLY_BOOST: float = 0.08     # Default edge boost (calibrate via backtest); overridden per strategy
    MAX_SPREAD_CENTS: float = 0.08     # Max bid-ask spread (8¢) to avoid illiquid markets

    # ── Order Book Imbalance ─────────────────────────────────────────────────
    OB_IMBALANCE_THRESHOLD: float = float(os.getenv("OB_IMBALANCE_THRESHOLD", "0.52"))  # 52% = signal (more trades)
    OB_DEPTH_LEVELS: int = 5               # How many price levels to analyze

    # ── Momentum / Price Velocity ─────────────────────────────────────────────
    MOMENTUM_WINDOW: int = int(os.getenv("MOMENTUM_WINDOW", "5"))  # 5 ticks (15-min has sparse history)
    MOMENTUM_MIN_MOVE: float = float(os.getenv("MOMENTUM_MIN_MOVE", "0.01"))  # 1% move (more trades)
    MOMENTUM_DIRECTION_CONSISTENCY: float = float(os.getenv("MOMENTUM_CONSISTENCY", "0.60"))  # 60% ticks same direction

    # ── Volume Spike Detection ───────────────────────────────────────────────
    VOLUME_SPIKE_MULTIPLIER: float = float(os.getenv("VOLUME_SPIKE_MULTIPLIER", "1.5"))  # 1.5x baseline (more trades)
    VOLUME_ROLLING_WINDOW: int = int(os.getenv("VOLUME_ROLLING_WINDOW", "10"))  # 10 ticks (15-min markets sparse)

    # ── Execution ────────────────────────────────────────────────────────────
    ORDER_TYPE: str = "GTC"            # GTC = Good Till Cancelled, FOK = Fill Or Kill
    SLIPPAGE_TOLERANCE: float = 0.02   # Max 2% slippage from mid price
    RETRY_ATTEMPTS: int = 3
    RETRY_DELAY_SECONDS: float = 1.0
    RETRY_429_DELAY_SECONDS: float = 5.0   # Longer backoff for rate limit (429)
    CLOB_PAGINATION_DELAY_SECONDS: float = 0.5   # Min delay between paginated requests
    CLOB_REQUEST_DELAY: float = float(os.getenv("CLOB_REQUEST_DELAY", "0.5"))  # 500ms between all CLOB requests

    # ── Position Management ───────────────────────────────────────────────────
    TAKE_PROFIT_MULTIPLIER: float = 1.8    # Exit when price hits 1.8x entry (80% profit)
    STOP_LOSS_THRESHOLD: float = 0.35      # Legacy: fixed price (ignored if STOP_LOSS_PCT set)
    STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.15"))  # 15% drop from entry (e.g. 0.40->0.34)
    MIN_HOLD_SECONDS: int = int(os.getenv("MIN_HOLD_SECONDS", "30"))  # Ignore stop loss for first 30s
    CLOSE_ON_RESTART: bool = os.getenv("CLOSE_ON_RESTART", "false").lower() in ("true", "1", "yes")
    TIME_STOP_BUFFER_SECONDS: int = 90     # Force-exit 90s before market resolves
    POLL_POSITIONS_INTERVAL: int = 15      # Check open positions every 15 seconds

    # ── Orphan / Maker reconciliation ────────────────────────────────────────
    ORPHAN_RECONCILE_INTERVAL_SECONDS: int = 120  # Reconcile CLOB positions every 2 min

    # ── Scanner ───────────────────────────────────────────────────────────────
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))  # Re-scan interval (30s default)
    MARKET_MIN_TIME_REMAINING: int = 60     # Only enter if ≥1 minute left
    MARKET_MAX_TIME_REMAINING: int = 900    # 15 min max for slug discovery; tag fallback uses 90 days

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: Optional[str] = os.getenv("LOG_FILE", "bot.log")
    TRADE_LOG_FILE: str = "trades.csv"

    # ── Dry Run (testing) ─────────────────────────────────────────────────────
    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

    # ── Price Feed (Binance geo-restricted in some regions; use CoinGecko fallback) ─
    PRICE_FEED_SOURCE: str = os.getenv("PRICE_FEED_SOURCE", "binance")  # "binance" | "coingecko"

    # ── Strategy 1: BTC Momentum Carry ───────────────────────────────────────────
    BTC_MOMENTUM_THRESHOLD: float = 0.003      # 0.3% move required
    BTC_MOMENTUM_MAX_ENTRY: float = 0.015      # Kill switch: don't enter if move > 1.5% already
    BTC_MOMENTUM_WINDOW_MINUTES: int = 5       # Window open price lookback
    ACTIVE_HOURS_START: int = 9                # 9 AM ET
    ACTIVE_HOURS_END: int = 16                 # 4 PM ET
    ACTIVE_HOURS_ENABLED: bool = os.getenv("ACTIVE_HOURS_ENABLED", "false").lower() in ("true", "1", "yes")

    # ── Strategy 2: ETH Lag Trade ─────────────────────────────────────────────────
    ETH_LAG_EXPIRY_SECONDS: int = 90          # How long BTC signal stays valid for ETH entry
    ETH_LAG_MAX_REPRICING: float = 0.08       # ETH odds must be within 8c of 0.50 in BTC direction
    ETH_LAG_MIN_BTC_MOVE: float = 0.004       # BTC must have moved 0.4% to trigger ETH lag
    ETH_LAG_SIGNAL_BOOST: float = 0.12        # Stronger Kelly edge boost for confirmed lag trades

    # ── Strategy 3: SOL Short-Squeeze ────────────────────────────────────────────
    SOL_FUNDING_RATE_THRESHOLD: float = -0.001   # Funding must be this negative or more
    SOL_RSI_OVERSOLD_THRESHOLD: float = 38.0     # RSI must be below this
    SOL_SQUEEZE_SIGNAL_BOOST: float = 0.15       # Extra Kelly edge boost for squeeze setups
    SOL_MIN_EDGE_SIGNALS: int = 2                # SOL only needs 2 of 4 base signals (lower bar)
    SOL_SQUEEZE_MAX_ENTRY_MINUTES: float = 3.0   # Only enter squeeze trades in first 3 min of window

    # ── Strategy 4: Maker Market Making ──────────────────────────────────────────
    MAKER_MODE_ENABLED: bool = True
    MAKER_SPREAD_TARGET: float = 0.04           # Place orders 4c apart (bid at 48c, ask at 52c)
    MAKER_MODE_HOURS_START: int = 23            # Best hours: 11 PM ET
    MAKER_MODE_HOURS_END: int = 5               # Until 5 AM ET
    MAKER_MAX_POSITION_SIZE: float = 50.0       # Per-side position in USDC
    MAKER_VOLATILITY_KILL: float = 0.008        # Cancel maker orders if price moves 0.8% suddenly

    # ── Strategy 5: XRP Catalyst ──────────────────────────────────────────────────
    XRP_REQUIRE_CATALYST: bool = os.getenv("XRP_REQUIRE_CATALYST", "false").lower() in ("true", "1", "yes")
    XRP_CATALYST_ACTIVE: bool = False
    XRP_CATALYST_DIRECTION: str = "UP"          # "UP" or "DOWN"
    XRP_CATALYST_EXPIRY_MINUTES: int = 60       # Auto-expire catalyst flag after 60 minutes
    XRP_CATALYST_SET_TIME: Optional[str] = None # ISO timestamp when flag was set
    XRP_CATALYST_SIGNAL_BOOST: float = 0.18     # Maximum Kelly boost for catalyst trades
    XRP_NO_CATALYST_MIN_SIGNALS: int = int(os.getenv("XRP_MIN_SIGNALS", "2"))  # 2 signals when no catalyst

    # ── Daily/Weekly Reports ───────────────────────────────────────────────────────
    DAILY_REPORT_ENABLED: bool = os.getenv("DAILY_REPORT_ENABLED", "true").lower() in ("true", "1", "yes")
    REPORT_EMAIL_TO: str = os.getenv("REPORT_EMAIL_TO", "")
    REPORT_EMAIL_FROM: str = os.getenv("REPORT_EMAIL_FROM", "")
    REPORT_EMAIL_PASSWORD: str = os.getenv("REPORT_EMAIL_PASSWORD", "")
    REPORT_SEND_TIME_UTC: str = os.getenv("REPORT_SEND_TIME_UTC", "23:59")  # HH:MM
    DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")
    WEEKLY_REPORT_DAY: str = os.getenv("WEEKLY_REPORT_DAY", "sunday").lower()
