# CURSOR AGENT — Strategy Implementation Prompt
# Polymarket 15-Min Up/Down Bot — Full Strategy Integration
# ============================================================

## YOUR MISSION

You are implementing a complete strategy upgrade to a live Polymarket trading bot.
You have two primary inputs:

1. **The Strategy Document** — located at:
   `C:\polymarket\strategies\polymarket_strategies.docx`
   Read this document COMPLETELY before writing a single line of code.
   It defines 5 specific trading strategies for BTC, ETH, SOL, and XRP on
   Polymarket's 15-minute Up/Down markets. Every implementation decision
   you make must be traceable back to this document.

2. **The Existing Bot Codebase** — all files in the current project directory.
   Read ALL of these files before writing any code:
   - `main.py`            → Bot entry point, scan loop, position loop
   - `config.py`          → All tunable parameters
   - `models.py`          → Market, Position, EdgeResult, OrderBook, PriceTick, Side
   - `clob_client.py`     → Async Polymarket CLOB API wrapper
   - `market_scanner.py`  → Discovers and enriches 15-min markets
   - `edge_filter.py`     → THE CORE PROFIT GATE — 4 signals (OB, Momentum, Volume, Kelly)
   - `executor.py`        → Places and signs CLOB orders
   - `position_manager.py`→ Monitors exits (TP / SL / TIME_STOP)
   - `state_writer.py`    → Writes bot state JSON for dashboard
   - `logger.py`          → Logging setup

   Pay special attention to:
   - The `EdgeResult` dataclass in `models.py` — you will be extending it
   - The `EdgeFilter.evaluate()` method in `edge_filter.py` — this is the core gate
   - The `BotConfig` dataclass in `config.py` — all new params go here
   - The `scan_loop()` in `main.py` — Strategy 2 (ETH lag) requires changes here

---

## WHAT THE STRATEGY DOCUMENT TELLS YOU TO BUILD

After reading the document, you will find 5 strategies. Here is the implementation
blueprint for each. Follow this EXACTLY.

---

### STRATEGY 1: BTC Momentum Carry
**File changes: `edge_filter.py`, `config.py`, `models.py`**

The document states: "If BTC moves 0.3%+ from window open price within first 3-5 minutes,
the probability of closing in that direction rises significantly above 50%."

**What to build:**

Add a new signal method `_check_btc_momentum_carry()` to `EdgeFilter` that:
- Accepts a `spot_price: float` (current Binance BTC price) and `window_open_price: float`
- Computes `pct_move = (spot_price - window_open_price) / window_open_price`
- Returns `(True, Side.YES)` if `pct_move >= BTC_MOMENTUM_THRESHOLD` (config: 0.003 = 0.3%)
- Returns `(True, Side.NO)` if `pct_move <= -BTC_MOMENTUM_THRESHOLD`
- Returns `(False, None)` otherwise
- Includes a directional consistency check: at least 70% of last 5 price ticks must align

Add to `BotConfig`:
```python
BTC_MOMENTUM_THRESHOLD: float = 0.003      # 0.3% move required
BTC_MOMENTUM_MAX_ENTRY: float = 0.015      # Kill switch: don't enter if move > 1.5% already
BTC_MOMENTUM_WINDOW_MINUTES: int = 5       # Window open price lookback
ACTIVE_HOURS_START: int = 9                # 9 AM ET
ACTIVE_HOURS_END: int = 16                 # 4 PM ET
ACTIVE_HOURS_ENABLED: bool = True          # Enforce US/EU hours gate
```

**Kill switch logic (document requirement):**
In `EdgeFilter.evaluate()`, if `pct_move > BTC_MOMENTUM_MAX_ENTRY`, log
`"BTC move already {pct_move:.2%} — too late, edge priced in"` and return no-edge.

**Active hours gate (document requirement):**
In `EdgeFilter.evaluate()`, check current UTC hour against ET conversion.
If `ACTIVE_HOURS_ENABLED` is True and current time is outside 9 AM - 4 PM ET,
skip Strategies 1, 2, and 3 entirely (maker market making still runs 24/7).

---

### STRATEGY 2: Cross-Asset ETH Lag Trade
**File changes: `main.py`, `edge_filter.py`, `models.py`, `config.py`**

**This is the highest-priority strategy in the document. It requires the most significant
architectural change.**

The document states: "ETH's price consistently lags BTC by 30-90 seconds during strong
directional moves. When BTC begins a decisive 15-minute move, ETH's Polymarket odds have
not yet repriced."

**What to build:**

**Step 1 — Shared BTC state in `main.py`:**
Add a `btc_signal_state` dict to `PolymarketBot.__init__()`:
```python
self.btc_signal_state = {
    "fired": False,
    "side": None,          # Side.YES or Side.NO
    "pct_move": 0.0,
    "timestamp": None,     # datetime when BTC signal fired
    "window_open_price": 0.0,
}
```

In `scan_loop()`, when processing the BTC market:
- If BTC's EdgeResult fires Strategy 1 (pct_move >= 0.4%), update `btc_signal_state`
- Set `fired=True`, record direction, pct_move, and timestamp
- This state is passed to ETH's EdgeFilter evaluation

After `ETH_LAG_EXPIRY_SECONDS` (config: 90 seconds), reset `btc_signal_state.fired = False`

**Step 2 — ETH lag signal in `edge_filter.py`:**
Add `_check_eth_lag_trade()` method:
```python
def _check_eth_lag_trade(self, btc_signal_state: dict, eth_mid_price: float) -> Tuple[bool, Optional[Side]]:
```
- Returns `(True, btc_side)` if ALL of:
  - `btc_signal_state["fired"]` is True
  - Time since BTC signal < `ETH_LAG_EXPIRY_SECONDS` (90s)
  - ETH Polymarket odds are still within `ETH_LAG_MAX_REPRICING` (config: 0.08) of 0.50
    (meaning the odds haven't already moved more than 8 cents from 50/50 in BTC's direction)
  - ETH spot price is moving in BTC's direction (use price history last 3 ticks)
- Returns `(False, None)` otherwise
- Log: `f"ETH LAG SIGNAL: BTC moved {pct_move:.2%} {btc_side} — ETH odds at {eth_mid:.3f}, lag window open"`

Add to `BotConfig`:
```python
ETH_LAG_EXPIRY_SECONDS: int = 90          # How long BTC signal stays valid for ETH entry
ETH_LAG_MAX_REPRICING: float = 0.08       # ETH odds must be within 8c of 0.50 in BTC direction
ETH_LAG_MIN_BTC_MOVE: float = 0.004       # BTC must have moved 0.4% to trigger ETH lag
ETH_LAG_SIGNAL_BOOST: float = 0.12        # Stronger Kelly edge boost for confirmed lag trades
```

**Step 3 — Pass BTC state into ETH evaluation:**
Modify `EdgeFilter.evaluate()` signature to accept optional `btc_signal_state: dict = None`.
When evaluating ETH markets (detect by checking if "ethereum" or "ETH" is in market question),
call `_check_eth_lag_trade()` and include its result as a 5th signal.
ETH lag signal firing counts as 2 signal credits (it's the highest-confidence signal).
This means ETH only needs 1 additional signal (from OB, Momentum, Volume, Kelly) to trade
when the lag signal fires.

---

### STRATEGY 3: SOL Short-Squeeze Detection
**File changes: `edge_filter.py`, `config.py`, `models.py`, `clob_client.py`**

The document states: "SOL is the most heavily shorted asset. Its funding rate is deeply
negative (-0.001826%). When BTC shows even modest strength and SOL spot twitches upward,
short sellers are forced to cover rapidly, creating violent squeeze moves."

**What to build:**

**Step 1 — Funding rate fetcher in `clob_client.py`:**
Add a new method `get_funding_rate(symbol: str) -> float`. This fetches from Binance
Futures API (not Polymarket): `GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}USDT`
Parse `lastFundingRate` from the response. Cache result for 5 minutes (funding updates every 8h).

**Step 2 — RSI calculator (add to `edge_filter.py`):**
Add a static helper `_calculate_rsi(prices: List[float], period: int = 14) -> float`.
Standard Wilder's RSI implementation. Returns float 0-100.

**Step 3 — Squeeze signal in `edge_filter.py`:**
Add `_check_sol_squeeze()` method:
```python
def _check_sol_squeeze(self, market: Market, funding_rate: float, btc_is_neutral_or_up: bool) -> Tuple[bool, Side]:
```
- Compute RSI from `market.price_history` prices
- Returns `(True, Side.YES)` if ALL of:
  - `funding_rate <= SOL_FUNDING_RATE_THRESHOLD` (config: -0.001 = deeply negative)
  - RSI of SOL price history < `SOL_RSI_OVERSOLD_THRESHOLD` (config: 38)
  - `btc_is_neutral_or_up` is True (BTC not falling hard)
  - Most recent 3 price ticks show an uptick of 0.2%+ from local low
- Returns `(False, None)` otherwise
- Log: `f"SOL SQUEEZE: funding={funding_rate:.6f}, RSI={rsi:.1f}, uptick confirmed"`

Add to `BotConfig`:
```python
SOL_FUNDING_RATE_THRESHOLD: float = -0.001   # Funding must be this negative or more
SOL_RSI_OVERSOLD_THRESHOLD: float = 38.0     # RSI must be below this
SOL_SQUEEZE_SIGNAL_BOOST: float = 0.15       # Extra Kelly edge boost for squeeze setups
SOL_MIN_EDGE_SIGNALS: int = 2                # SOL only needs 2 of 4 base signals (lower bar)
SOL_SQUEEZE_MAX_ENTRY_MINUTES: float = 3.0   # Only enter squeeze trades in first 3 min of window
```

**Per-asset signal thresholds (document requirement):**
Modify `EdgeFilter.evaluate()` to check which asset the market is for:
```python
asset = self._detect_asset(market.question)
# Returns: "BTC", "ETH", "SOL", "XRP", or "UNKNOWN"
min_signals = {
    "BTC": config.MIN_EDGE_SIGNALS,      # 3
    "ETH": config.MIN_EDGE_SIGNALS,      # 3 (reduced to 1 when lag fires)
    "SOL": config.SOL_MIN_EDGE_SIGNALS,  # 2 when squeeze conditions met
    "XRP": 4,                            # Stricter — no edge without catalyst
}.get(asset, config.MIN_EDGE_SIGNALS)
```

Add `_detect_asset(question: str) -> str` helper that parses the market question string
for "bitcoin"/"BTC", "ethereum"/"ETH", "solana"/"SOL", "XRP"/"ripple".

---

### STRATEGY 4: Maker Market Making
**File changes: `executor.py`, `position_manager.py`, `config.py`, `main.py`**

The document states: "Place limit orders on both YES and NO sides during low-volatility
windows. If both fill, profit is guaranteed: (52c - 48c) + rebates = 4% gain."

**What to build:**

**Step 1 — Maker mode config:**
Add to `BotConfig`:
```python
MAKER_MODE_ENABLED: bool = True             # Enable market making alongside directional
MAKER_SPREAD_TARGET: float = 0.04           # Place orders 4c apart (bid at 48c, ask at 52c)
MAKER_MODE_HOURS_START: int = 23            # Best hours: 11 PM ET
MAKER_MODE_HOURS_END: int = 5               # Until 5 AM ET
MAKER_MAX_POSITION_SIZE: float = 50.0       # Per-side position in USDC
MAKER_VOLATILITY_KILL: float = 0.008        # Cancel maker orders if price moves 0.8% suddenly
```

**Step 2 — Maker order logic in `executor.py`:**
Add `place_maker_pair()` method:
```python
async def place_maker_pair(self, market: Market) -> Tuple[Optional[str], Optional[str]]:
    """
    Place maker limit orders on BOTH sides of a market.
    Returns (yes_order_id, no_order_id).
    YES order: limit buy at (mid - MAKER_SPREAD_TARGET/2)
    NO order:  limit buy at (mid - MAKER_SPREAD_TARGET/2) on the NO side
    Both are maker orders (limit, not taker).
    """
```

**Step 3 — Maker loop in `main.py`:**
Add a third async task `maker_loop()` to `asyncio.gather()` in `PolymarketBot.run()`.
The maker loop:
- Runs every 60 seconds
- Only activates if `MAKER_MODE_ENABLED` and current time is in maker hours
- Scans for low-volatility markets (price_history stddev < 0.005 over last 10 ticks)
- Places maker pairs on BTC and ETH markets (highest liquidity, tightest spreads)
- Tracks open maker pairs in a dict `self.maker_positions`
- Cancels any unfilled maker order that is 5+ minutes old (avoid adverse selection)
- If only ONE side of a pair fills, treat the resulting position as a normal directional
  trade and hand it to `position_manager` with normal TP/SL rules

---

### STRATEGY 5: XRP Catalyst Event Trading
**File changes: `config.py`, `edge_filter.py`, `main.py`**

The document states: "XRP is range-bound and consolidating. Its 52% implied odds reflect
genuine uncertainty. Avoid trading XRP without a confirmed catalyst. With catalyst: 65-72%
win rate, entry at 52c resolves at 100c = 88% return."

**What to build:**

**Step 1 — Catalyst flag in `config.py`:**
Add to `BotConfig`:
```python
XRP_CATALYST_ACTIVE: bool = False           # Set True manually when a catalyst fires
XRP_CATALYST_DIRECTION: str = "UP"          # "UP" or "DOWN"
XRP_CATALYST_EXPIRY_MINUTES: int = 60       # Auto-expire catalyst flag after 60 minutes
XRP_CATALYST_SET_TIME: Optional[str] = None # ISO timestamp when flag was set
XRP_CATALYST_SIGNAL_BOOST: float = 0.18     # Maximum Kelly boost for catalyst trades
XRP_NO_CATALYST_MIN_SIGNALS: int = 4        # Require ALL 4 signals if no catalyst
```

**Step 2 — Catalyst check in `edge_filter.py`:**
Add `_check_xrp_catalyst()` method:
```python
def _check_xrp_catalyst(self, config: BotConfig, market_side: Optional[Side]) -> Tuple[bool, Optional[Side]]:
    """Check if XRP catalyst flag is active and aligned with market direction."""
```
- If `config.XRP_CATALYST_ACTIVE` is False → return `(False, None)` (no XRP edge without catalyst)
- If catalyst is active, check if it has expired (compare current time vs XRP_CATALYST_SET_TIME + expiry)
  If expired: set `config.XRP_CATALYST_ACTIVE = False`, log warning, return `(False, None)`
- If active and not expired: return `(True, Side.YES if config.XRP_CATALYST_DIRECTION == "UP" else Side.NO)`
- Catalyst signal fires counts as 3 signal credits (highest conviction signal)

**Step 3 — Catalyst CLI toggle in `main.py`:**
Add a background task `catalyst_watcher()` that reads a file `catalyst_flag.json` in the
project root every 30 seconds. If the file exists and contains valid JSON
`{"asset": "XRP", "direction": "UP", "reason": "ETF inflow"}`, update config accordingly.
This allows setting a catalyst without restarting the bot:
```bash
echo '{"asset": "XRP", "direction": "UP", "reason": "ETF inflow report"}' > catalyst_flag.json
```

---

## NEW FILES TO CREATE

### `binance_feed.py` — Real-Time Price Feed
Create this new file from scratch. It is the most critical infrastructure addition.

```python
"""
binance_feed.py — Async WebSocket connection to Binance spot price feeds.
Provides real-time BTC, ETH, SOL, XRP prices at sub-100ms latency.
This is the primary oracle for all 5 strategies. DO NOT use REST polling here.
"""
```

Requirements:
- Connect to `wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/ethusdt@ticker/solusdt@ticker/xrpusdt@ticker`
- Maintain a `latest_prices` dict: `{"BTC": float, "ETH": float, "SOL": float, "XRP": float}`
- Maintain a `window_open_prices` dict: resets every 15 minutes on the clock boundary
  (e.g., 09:00, 09:15, 09:30 — when a new Polymarket window opens)
- Maintain a `price_history_buffer` dict: rolling 100-tick deque per asset
- Expose `get_price(symbol)`, `get_pct_move_from_window_open(symbol)`, `get_price_history(symbol)`
- Auto-reconnect on disconnect with exponential backoff (1s, 2s, 4s, 8s max)
- Log connection status but not every tick (too noisy)
- Add `get_funding_rate(symbol)` that fetches from Binance Futures REST endpoint
  `GET https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}USDT`
  Cache for 5 minutes (funding only changes every 8h)

### `strategy_router.py` — Strategy Selection Logic
Create this new file. It lives between `edge_filter.py` and `main.py`.

```python
"""
strategy_router.py — Maps each market to its correct strategy and configures
the EdgeFilter accordingly. The single place where strategy logic is selected.

Strategy assignment:
  BTC markets  → Strategy 1 (Momentum Carry) + Strategy 4 (Maker)
  ETH markets  → Strategy 2 (ETH Lag) with BTC signal input + Strategy 4 (Maker)
  SOL markets  → Strategy 3 (Squeeze Detection)
  XRP markets  → Strategy 5 (Catalyst Only) — no trade without active catalyst
"""
```

Implement `StrategyRouter` class with:
```python
async def route(self, market: Market, binance_feed: BinanceFeed,
                btc_signal_state: dict, config: BotConfig) -> EdgeResult:
    """
    Route a market to the correct strategy evaluation.
    Returns EdgeResult — if has_edge is False, bot does not trade.
    """
```

Logic:
1. Detect asset from market question using `_detect_asset()`
2. Get live Binance price for that asset
3. Get pct_move_from_window_open for that asset
4. Route to correct EdgeFilter method based on asset
5. Apply per-asset kill switches (from document Section 3)
6. Return EdgeResult with `strategy_name` field added (for logging/dashboard)

---

## MODELS.PY EXTENSIONS REQUIRED

Extend the `EdgeResult` dataclass with these new fields:
```python
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
```

Extend the `Market` dataclass with:
```python
asset: str = ""                  # "BTC", "ETH", "SOL", "XRP" — populated by scanner
window_open_price: float = 0.0   # Binance spot price at window open — from BinanceFeed
```

---

## CONFIG.PY FINAL STATE

After all additions, `config.py` must contain all parameters in clearly labeled sections.
Add a new section at the bottom:

```python
# ── Strategy 1: BTC Momentum Carry ───────────────────────────────────────────
BTC_MOMENTUM_THRESHOLD: float = 0.003
BTC_MOMENTUM_MAX_ENTRY: float = 0.015
BTC_MOMENTUM_WINDOW_MINUTES: int = 5
ACTIVE_HOURS_START: int = 9
ACTIVE_HOURS_END: int = 16
ACTIVE_HOURS_ENABLED: bool = True

# ── Strategy 2: ETH Lag Trade ─────────────────────────────────────────────────
ETH_LAG_EXPIRY_SECONDS: int = 90
ETH_LAG_MAX_REPRICING: float = 0.08
ETH_LAG_MIN_BTC_MOVE: float = 0.004
ETH_LAG_SIGNAL_BOOST: float = 0.12

# ── Strategy 3: SOL Short-Squeeze ────────────────────────────────────────────
SOL_FUNDING_RATE_THRESHOLD: float = -0.001
SOL_RSI_OVERSOLD_THRESHOLD: float = 38.0
SOL_SQUEEZE_SIGNAL_BOOST: float = 0.15
SOL_MIN_EDGE_SIGNALS: int = 2
SOL_SQUEEZE_MAX_ENTRY_MINUTES: float = 3.0

# ── Strategy 4: Maker Market Making ──────────────────────────────────────────
MAKER_MODE_ENABLED: bool = True
MAKER_SPREAD_TARGET: float = 0.04
MAKER_MODE_HOURS_START: int = 23
MAKER_MODE_HOURS_END: int = 5
MAKER_MAX_POSITION_SIZE: float = 50.0
MAKER_VOLATILITY_KILL: float = 0.008

# ── Strategy 5: XRP Catalyst ──────────────────────────────────────────────────
XRP_CATALYST_ACTIVE: bool = False
XRP_CATALYST_DIRECTION: str = "UP"
XRP_CATALYST_EXPIRY_MINUTES: int = 60
XRP_CATALYST_SET_TIME: Optional[str] = None
XRP_CATALYST_SIGNAL_BOOST: float = 0.18
XRP_NO_CATALYST_MIN_SIGNALS: int = 4
```

---

## MAIN.PY FINAL ARCHITECTURE

After all changes, `PolymarketBot.run()` must launch FOUR async tasks:
```python
await asyncio.gather(
    self.scan_loop(),               # Directional strategies 1, 2, 3, 5
    self.maker_loop(),              # Strategy 4 — passive maker rebate income
    self.position_manager.monitor_loop(),  # Exit management
    self.catalyst_watcher(),        # XRP catalyst flag file watcher
)
```

The `scan_loop()` must:
1. Initialize `BinanceFeed` before the loop starts and pass it to `StrategyRouter`
2. Use `StrategyRouter.route()` instead of calling `EdgeFilter.evaluate()` directly
3. Update `btc_signal_state` when BTC fires Strategy 1
4. Clear `btc_signal_state` after `ETH_LAG_EXPIRY_SECONDS`
5. Write bot state via `state_writer.write_state()` on every cycle

---

## REQUIREMENTS.TXT UPDATES

Add these dependencies:
```
websockets>=12.0
aiohttp>=3.9.0          # Already present — verify
python-dotenv>=1.0.0    # Already present — verify
```

---

## TESTING REQUIREMENTS

After implementation, create `test_strategies.py` with unit tests for:
1. `test_btc_momentum_carry()` — verify signal fires at 0.3% move, not at 0.2%, kills at 1.5%
2. `test_eth_lag_detection()` — verify fires when BTC state active + ETH odds not repriced
3. `test_sol_squeeze_signal()` — verify fires when funding < -0.001 AND RSI < 38
4. `test_maker_pair_logic()` — verify both sides placed at correct spread
5. `test_xrp_catalyst_expiry()` — verify catalyst auto-expires after configured minutes
6. `test_asset_detection()` — verify "Bitcoin Up or Down - 15 min" → "BTC"
7. `test_active_hours_gate()` — verify directional strategies blocked outside 9AM-4PM ET

Use `unittest.mock` to mock `BinanceFeed` and `ClobClient`. No live API calls in tests.

---

## CRITICAL RULES — DO NOT VIOLATE

1. **NEVER remove existing signal logic** (OB imbalance, momentum, volume, Kelly).
   The new strategies ADD signals on top of the existing 4. They do not replace them.

2. **The profit gate is sacred.** `EdgeResult.has_edge` must remain the single boolean
   that controls whether a trade executes. Nothing bypasses this gate.

3. **Maker orders always, taker orders rarely.** All new order placements must use
   limit/maker orders by default. Taker is only acceptable for ETH lag trades where
   speed matters more than fee savings and the edge is confirmed.

4. **Per-asset min signals** from the strategy document must be respected:
   - BTC: 3 of 4 base signals
   - ETH: 3 of 4 base signals (reduces to 1 when lag signal fires — it counts as 2)
   - SOL: 2 of 4 base signals when squeeze conditions confirmed
   - XRP: 4 of 4 base signals when no catalyst / 1 when catalyst fires (counts as 3)

5. **Kill switches are non-negotiable.** Each strategy has defined conditions under
   which it must NOT trade (documented in Section 3 of the strategy document).
   Every kill switch must be implemented as an early return with a clear log message.

6. **Do not break the dashboard.** `state_writer.write_state()` must remain compatible
   with `dashboard/server.py`. If you add new fields to the state, add them as optional
   keys so the dashboard degrades gracefully rather than crashing.

7. **The `trades.csv` format must not change.** `position_manager.py` log format
   is consumed by the dashboard. Any new fields must be appended as additional columns,
   never inserted in the middle.

---

## IMPLEMENTATION ORDER

Do this in sequence. Do not skip steps. Do not start step N+1 until step N is complete and tested.

```
Step 1:  Read strategy document (C:\polymarket\strategies\polymarket_strategies.docx)
Step 2:  Read ALL existing bot files listed above
Step 3:  Extend models.py (EdgeResult + Market new fields)
Step 4:  Update config.py (all new parameters)
Step 5:  Create binance_feed.py (WebSocket feed — most critical dependency)
Step 6:  Update edge_filter.py (add 4 new signal methods + per-asset routing)
Step 7:  Create strategy_router.py
Step 8:  Update executor.py (add place_maker_pair())
Step 9:  Update main.py (4 async tasks, BinanceFeed init, StrategyRouter wiring)
Step 10: Update position_manager.py (handle maker positions separately)
Step 11: Update state_writer.py (include strategy_name in state output)
Step 12: Create test_strategies.py and run all 7 tests
Step 13: Run the bot with LOG_LEVEL=DEBUG for 5 minutes in dry-run mode
         (add DRY_RUN: bool = True config flag — when True, log trades but don't execute)
Step 14: Verify dashboard still works: python dashboard/server.py
```

---

## FINAL VERIFICATION CHECKLIST

Before marking this task complete, confirm every item:

- [ ] `python main.py` starts without errors
- [ ] Binance WebSocket connects and logs price updates within 5 seconds
- [ ] BTC market correctly identified and routes to Strategy 1
- [ ] ETH market correctly identified and routes to Strategy 2 with BTC state input
- [ ] SOL market correctly identified, funding rate fetched, squeeze logic active
- [ ] XRP market correctly identified, no trades execute with `XRP_CATALYST_ACTIVE=False`
- [ ] Creating `catalyst_flag.json` enables XRP trading within 30 seconds
- [ ] Maker loop activates during configured hours and places both-side orders
- [ ] All 7 unit tests pass
- [ ] Dashboard server starts and receives live state updates
- [ ] `trades.csv` records all strategy names in new `strategy` column
- [ ] No taker orders placed except ETH lag trades (verify in logs)
- [ ] Active hours gate blocks directional trades outside 9AM-4PM ET
- [ ] `DRY_RUN=True` in `.env` prevents any real order execution during testing
```
