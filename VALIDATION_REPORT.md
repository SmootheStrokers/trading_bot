# Comprehensive Validation Report — Polymarket 15-Min Up/Down Trading Bot

**Validation Date:** February 23, 2025  
**Scope:** Full production codebase — profitability, workflow, safety, critical issues, recommendations

---

## 1. PROFITABILITY ANALYSIS

### 1.1 Strategy Framework

**Architecture:** The bot uses **5 strategy-specific signals**, not a classic momentum/mean-reversion/volatility framework:

| Strategy | Type | Logic | Location |
|----------|------|-------|----------|
| **BTC Momentum Carry** | Momentum | 0.3%+ spot move from window open; 70% tick consistency | `edge_filter._check_btc_momentum_carry` |
| **ETH Lag** | Momentum-follow | BTC fired within 90s; ETH odds within 8¢ of 0.50 | `edge_filter._check_eth_lag_trade` |
| **SOL Short-Squeeze** | Contrarian/Volatility | Funding < -0.001, RSI < 38, 0.2% uptick; BTC neutral/up | `edge_filter._check_sol_squeeze` |
| **Maker** | Low-volatility | Stddev < 0.005; 11 PM–5 AM ET only | `main._is_low_volatility`, `executor.place_maker_pair` |
| **XRP Catalyst** | Event-driven | External flag in `catalyst_flag.json` | `edge_filter._check_xrp_catalyst` |

**Gaps:**
- **No mean reversion strategy.** SOL squeeze is contrarian (oversold bounce) but not classic mean reversion.
- **No explicit volatility breakout** — momentum uses Polymarket price history, not spot volatility bands.

### 1.2 Signal Generation Logic

**Base signals (4):** OB imbalance, momentum, volume spike, Kelly. All evaluated in `edge_filter.evaluate()`.

| Signal | Logic | Potential Issue |
|--------|-------|-----------------|
| OB Imbalance | Bid/ask depth ratio ≥ 60% | Uses only YES side; ignores NO depth structure |
| Momentum | ≥4% move, 70% tick consistency | Polymarket price history may be stale/sparse |
| Volume Spike | Recent vol ≥ 2.5× rolling avg | Zero baseline → `avg_vol == 0` returns False (safe) |
| Kelly | est_prob = implied + boost; edge ≥ 5% | Fixed boost (8–18%) — no calibration to actual edge |

**Strategy signal credits:**

```python
# edge_filter.py lines 174-182
if eth_lag_signal:
    strategy_credits = 2
elif xrp_catalyst_signal:
    strategy_credits = 3
elif btc_mom_signal or sol_squeeze_signal:
    strategy_credits = 1
```

ETH Lag gets 2 credits (lowers bar to 1 additional signal); XRP Catalyst gets 3 (lowers to 1). Logic is consistent.

### 1.3 Ensemble Voting / Signal Weighting

**There is no ensemble voting or weighted signal system.** The logic is:

1. **Signal count:** `effective_count >= min_signals` (per-asset)
2. **Directional consensus:** All firing signals must agree on YES or NO (`_resolve_directional_side`, `_directions_agree`)
3. **Kelly gate:** `kelly_edge >= MIN_KELLY_EDGE` and `kelly_size >= MIN_BET_SIZE`

**No weights** — each signal contributes 0 or 1. Strategy credits are fixed, not learned or backtest-tuned.

### 1.4 P&L Calculations

**Formula:** `pnl = (exit_price - entry_price) * shares` — correct for both YES and NO.

- **YES:** `entry_price` = YES mid; `exit_price` = YES last trade. ✓
- **NO:** `entry_price` = 1 - mid; `token_id` = no_token_id; CLOB returns NO price. ✓

**File:** `position_manager.py` line 181:
```python
pos.pnl = (pos.exit_price - pos.entry_price) * pos.shares
```

**Caveat:** P&L assumes full fill at `exit_price`. No verification that the exit order filled; in thin markets, actual fill may differ.

### 1.5 Unprofitable Trade Logic

| Issue | Location | Impact |
|-------|----------|--------|
| **Fixed Kelly boost** | `edge_filter._check_kelly` | `estimated_prob = implied + 0.08–0.18` — if true edge is lower, trades are -EV |
| **Momentum on Polymarket price** | `_check_momentum` | Polymarket ticks may lag spot; momentum can be stale |
| **SOL squeeze timing** | `_check_sol_squeeze` | 3-min entry window — may enter too late after uptick |
| **Maker not monitored** | `main.maker_loop` | Maker positions never added to `position_manager` — no TP/SL/time stop |

---

## 2. WORKFLOW VALIDATION

### 2.1 Data Flow

```
MarketScanner.fetch_active_15min_markets()
  → gamma_client (slug or tag)
  → _enrich_market (order book, price history from CLOB)
  → MIN_LIQUIDITY_USDC gate (500)

StrategyRouter.route(market, binance_feed, btc_signal_state)
  → EdgeFilter.evaluate(market, ...)

EdgeFilter.evaluate()
  → Active hours gate
  → Strategy signals (BTC/ETH/SOL/XRP)
  → Base signals (OB, momentum, volume, Kelly)
  → has_edge = count + consensus + Kelly

main.scan_loop:
  if has_edge:
    place_order()  → OrderExecutor
    add_position() → PositionManager  # BUG: no check on place_order success
```

**Risk check flow:** Only `at_capacity()` is checked before order. `MAX_PORTFOLIO_RISK` is **never enforced** (see 3.1).

### 2.2 Async Loops

| Loop | Sync | Exits when |
|------|------|------------|
| `scan_loop` | `while self.running` | `running=False` |
| `maker_loop` | `while self.running` | `running=False` |
| `monitor_loop` | `while _should_keep_running()` | `stop_predicate()` returns False |
| `catalyst_watcher` | `while self.running` | `running=False` |

All loops run via `asyncio.gather()` — concurrent. Shared state: `btc_signal_state`, `signal_feed`, `maker_positions` — **no locks**. Low risk in current design; refactors could introduce races.

### 2.3 Position Tracking per Asset

**Tracking:** `position_manager.positions` keyed by `condition_id`. `has_position(condition_id)` and `at_capacity()` treat all assets equally.

**Gap:** No per-asset exposure cap. Could open 5 BTC positions with no ETH diversification check.

### 2.4 15-Minute Market Windows

| Component | Implementation |
|-----------|----------------|
| **Gamma discovery** | Slug `{coin}-updown-15m-{ts}` or tag filter; `secs_remaining` 60–900 |
| **BinanceFeed window** | `minute // 15` UTC; `_update_window_open_prices()` on boundary |
| **SOL squeeze window** | `window_start = end_timestamp - 900`; `minutes_into_window` |

**Timezone:** BinanceFeed uses UTC. Polymarket questions often use ET. If market "12:00–12:15 AM ET" resolves at 5:15 UTC, the UTC :00 and :15 boundaries align for that case. Generic ET↔UTC alignment is not guaranteed for all markets.

### 2.5 Order Fill Reconciliation

**None.** The bot:

1. Calls `place_order()` → returns `order_id` or `None`
2. Immediately calls `add_position()` **regardless of return value**
3. Never checks order status or fill

**Impact:** If `place_order()` fails (returns `None`), a phantom position is tracked. If a limit order never fills, the bot still assumes a filled position and monitors it.

**File:** `main.py` lines 216–218:
```python
await self.executor.place_order(market, edge_result)
self.position_manager.add_position(market, edge_result)  # No success check!
```

---

## 3. SAFETY & RISK CHECKS

### 3.1 Eight Risk Rules — Audit

| # | Rule | Config | Enforced? | Location |
|---|------|--------|-----------|----------|
| 1 | Max positions | `MAX_POSITIONS=5` | ✅ | `position_manager.at_capacity()` |
| 2 | Kelly fraction cap | `MAX_KELLY_FRACTION=0.25` | ✅ | `edge_filter._check_kelly` |
| 3 | Bet size limits | `MIN_BET_SIZE`, `MAX_BET_SIZE` | ✅ | `edge_filter._check_kelly` |
| 4 | BTC momentum kill | `BTC_MOMENTUM_MAX_ENTRY=0.015` | ✅ | `edge_filter.evaluate` |
| 5 | Slippage tolerance | `SLIPPAGE_TOLERANCE=0.02` | ✅ | `executor._compute_limit_price` |
| 6 | Take profit / Stop loss | `TAKE_PROFIT_MULTIPLIER`, `STOP_LOSS_THRESHOLD` | ✅ | `position_manager._evaluate_position` |
| 7 | Time stop | `TIME_STOP_BUFFER_SECONDS=90` | ✅ | `position_manager._evaluate_position` |
| 8 | **Portfolio risk cap** | `MAX_PORTFOLIO_RISK=0.30` | ❌ **Never enforced** | — |

**`MAX_PORTFOLIO_RISK`** is defined in `config.py` line 32 but never referenced in the codebase.

### 3.2 Orphan Position Detection

**Status: ❌ NOT IMPLEMENTED**

- `clob_client.get_positions()` exists but is never called.
- No reconciliation between CLOB positions and `position_manager.positions`.
- Maker mode: YES/NO placed; if only one fills, one-sided exposure is never detected.

### 3.3 Paper Trading Default

**✅ Correct**

- `config.py` line 23: `PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ...`
- `.env.example`: `PAPER_TRADING=true`, `DRY_RUN=true`

### 3.4 Live Trading Default

**✅ Disabled**

- Both PAPER_TRADING and DRY_RUN default to true. Live trading requires explicit `PAPER_TRADING=false` and `DRY_RUN=false`.

### 3.5 Race Conditions / Shared State

| Shared State | Writers | Lock? |
|--------------|---------|-------|
| `btc_signal_state` | scan_loop | ❌ |
| `signal_feed` | scan_loop | ❌ |
| `maker_positions` | maker_loop | ❌ |
| `position_manager.positions` | scan_loop, monitor_loop, _cleanup | ❌ |

**Risk:** Low for current flow. If parallel scans or maker/scan overlap increases, add `asyncio.Lock` for critical sections.

---

## 4. CRITICAL ISSUES

### 4.1 Money-Loss Bugs

| Bug | Severity | Description |
|-----|----------|-------------|
| **Position added on order failure** | HIGH | `add_position` called without checking `place_order` return. Failed orders still create tracked positions. |
| **Maker positions not monitored** | HIGH | Maker YES/NO orders go to `maker_positions` only. No TP/SL/time stop; unfilled or partially filled maker exposure is unmanaged. |
| **MAX_PORTFOLIO_RISK ignored** | MEDIUM | 30% bankroll risk cap is not enforced. |
| **Stale last-trade price** | MEDIUM | CLOB `last-trade-price` defaults to 0.5 when no trades; can distort TP/SL decisions. |

### 4.2 Signal Logic Flaws

| Issue | Description |
|-------|-------------|
| **Kelly boost not calibrated** | Fixed 8–18% boost may overstate edge. |
| **OB imbalance ignores NO side** | Only YES bids/asks used; NO depth could contradict. |
| **Momentum uses Polymarket ticks** | May lag spot; Polymarket history can be sparse. |

### 4.3 Missing Components

| Component | Status |
|-----------|--------|
| Orphan detection | Missing |
| Order fill verification | Missing |
| Portfolio risk enforcement | Missing |
| Maker position monitoring | Missing |
| Daily loss cap | Missing |
| Max trades per hour | Missing |

### 4.4 Implementation Gaps

- **BinanceFeed / Polymarket window alignment:** UTC 15-min boundaries may not match all Polymarket ET windows.
- **Maker volatility kill:** `MAKER_VOLATILITY_KILL` defined but no logic cancels maker orders on sudden moves.
- **Error handling:** Order placement has no retry; single failure aborts the order.

---

## 5. RECOMMENDATIONS

### 5.1 Before Live Trading

1. **Check order success before adding position** — ✅ *Fixed.* `add_position` now only runs when `place_order` returns a non-None `order_id`.

2. **Enforce MAX_PORTFOLIO_RISK** — ✅ *Implemented.* `position_manager.would_exceed_portfolio_risk()` blocks orders when total exposure would exceed 30% of bankroll.

3. **Implement orphan detection** — ✅ *Implemented.* `orphan_handler.py` reconciles CLOB positions; orphans added to `position_manager` for TP/SL/time-stop monitoring. Runs every `ORPHAN_RECONCILE_INTERVAL_SECONDS` (default 120s).

4. **Monitor maker positions** — ✅ *Addressed.* Maker fills appear as CLOB positions; orphan reconciliation picks them up and adds to `position_manager`.

### 5.2 Profitability Improvements

1. **Calibrate Kelly boost** — ✅ `config.BASE_KELLY_BOOST` (default 0.08); replace with backtest values.
2. **Include NO side in OB imbalance** — ✅ `_check_order_book_imbalance` requires YES/NO agreement when no_ob provided.
3. **Use spot price for momentum** — optionally blend BinanceFeed spot with Polymarket price.
4. **Add minimum liquidity / spread checks** — ✅ `MAX_SPREAD_CENTS` in scanner.

### 5.3 Testing

1. **Order failure path** — ✅ `TestOrderSuccessBeforePosition.test_no_position_when_order_returns_none`
2. **P&L edge cases** — ✅ `TestPnLEdgeCases` (YES/NO profit and loss)
3. **Signal consistency** — covered by existing strategy tests
4. **Maker fill behavior** — orphan reconciliation handles maker fills
5. **Integration** — ✅ `TestOrderSuccessBeforePosition` simulates main.py order flow; `TestPortfolioRiskCap` verifies risk enforcement

---

## Summary Table

| Category | Status | Critical Gaps |
|----------|--------|---------------|
| Profitability | ⚠️ | Fixed Kelly boost; maker not monitored |
| Workflow | ⚠️ | No fill reconciliation; position added on order failure |
| Safety | ⚠️ | MAX_PORTFOLIO_RISK not enforced; no orphan detection |
| Critical bugs | ❌ | Position-on-failure; maker not monitored |

**Verdict:** Fix the position-on-order-failure and maker monitoring issues before live trading. Implement portfolio risk enforcement and orphan detection for robust production use.
