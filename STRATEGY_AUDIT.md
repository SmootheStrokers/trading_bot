# Strategy Audit & $1000/Day Feasibility

**Audit Date:** February 24, 2025  
**Scope:** Full codebase audit + optimizations for $1000/day target

---

## 1. Strategy Audit Summary

### Current Strategy Overview

Your bot trades **Polymarket 15-minute crypto Up/Down markets** (BTC, ETH, SOL, XRP) using five strategies:

| Strategy | Trigger | Edge Source |
|----------|---------|--------------|
| **BTC Momentum** | 0.3%+ move from window open, 70% tick consistency | Binance spot + Polymarket lag |
| **ETH Lag** | BTC fired first, ETH odds within 8¢ of 50 | ETH reprices after BTC |
| **SOL Squeeze** | Negative funding + oversold RSI + uptick | Funding + technicals |
| **Maker** | Low volatility, 11 PM–5 AM ET | Spread capture |
| **XRP Catalyst** | Manual catalyst flag | Event-driven |

**Entry gate:** 3 of 4 base signals + Kelly edge ≥ 4–5% + directional agreement.

### Honest Assessment: Is $1000/Day Realistic?

**With a $1,000 bankroll: No.**

- $1000/day = **100% return per day** → not sustainable.
- Even at 10% daily return, bankroll would double roughly every 7 days.
- With $100 max bet and ~50% win rate, expected profit per trade ≈ $5–15; you’d need ~70–200 winning trades per day.

**What it would take with $1,000:**

| Metric | Required | Current |
|--------|----------|---------|
| Win rate | ~65%+ | Unknown (needs live data) |
| Avg trade size | $80–100 | Capped at $100 |
| Avg profit per trade | $15–20 | ~$10–20 (if TP hits) |
| Trades/day | 50–70 | ~5–20 (market-limited) |
| Edge per trade | 5%+ | 4–5% (MIN_EDGE_PCT) |

**Conclusion:** $1000/day with $1,000 bankroll is not realistic. The bot’s structure is sound, but the math doesn’t support that target at this size.

---

## 2. What You’d Need for $1000/Day

### Bankroll Needed

Assuming:

- **10% daily return** (aggressive but possible over short periods)
- Target: **$1000/day**
- Required bankroll: **$10,000** (at 10% daily)

At a more conservative **5% daily:**

- Required bankroll: **$20,000**

### Target Metrics (Example)

For $1000/day with $10k bankroll:

- **Win rate:** 60–65%
- **Avg bet:** 2–5% of bankroll ($200–500)
- **Avg profit per trade:** $30–50
- **Trades/day:** 25–35

---

## 3. Biggest Weaknesses (Addressed)

| Weakness | Status |
|----------|--------|
| No daily loss limit | ✅ `DAILY_LOSS_LIMIT_PCT=0.20` — hard stop at 20% |
| No per-trade max loss | ✅ `PER_TRADE_MAX_LOSS_PCT=0.10` — caps size at 10% bankroll |
| Fixed position sizing | ✅ `POSITION_SIZING_MODE` + Kelly/fractional Kelly |
| Low-volume markets | ✅ `MIN_MARKET_VOLUME_USD` + sort by liquidity/volume |
| No min edge threshold | ✅ `MIN_EDGE_PCT=0.04` (configurable 3–5%) |
| Limited Binance funding use | ✅ Funding for BTC/ETH in signal logic |
| No trade frequency limit | ✅ `MAX_TRADES_PER_HOUR=20` |
| Overtrading after losses | ✅ Loss-streak cooldown (higher edge required) |
| No goal tracking | ✅ Dashboard $1000/day goal panel |

---

## 4. Improvements Implemented

### Risk Management

- **Daily loss limit:** Trading paused if daily loss ≥ 20% of bankroll.
- **Per-trade max loss:** Position size capped at 10% of bankroll.
- **Trade frequency:** Max 20 trades/hour.
- **Loss-streak rule:** After 2+ consecutive losses, require 2% extra edge.
- **No martingale/revenge logic:** Only adds stricter filters after losses.

### Position Sizing

- **`POSITION_SIZING_MODE`:** `kelly` | `fractional_kelly` | `bankroll_pct`
- **`KELLY_FRACTION=0.5`:** Half-Kelly by default.
- Dynamic cap: `min(kelly_size, bankroll * PER_TRADE_MAX_LOSS_PCT)`.

### Market Selection

- Markets sorted by **liquidity** then **volume**.
- `MIN_MARKET_VOLUME_USD=1000` (configurable).
- Thin markets filtered out to reduce slippage.

### Edge Detection

- **`MIN_EDGE_PCT`:** Minimum edge (default 4%).
- Funding rate used for BTC/ETH; negative funding + YES adds edge boost.
- Trades only when `kelly_edge >= max(MIN_KELLY_EDGE, MIN_EDGE_PCT)`.

### Dashboard

- Daily P&L vs $1000 goal.
- Progress bar ($X / $1000).
- Projected daily total from current pace.
- Days to double bankroll.
- Trading pause warning when daily loss limit hit.

---

## 5. Highest-Leverage Improvements

1. **Increase bankroll**  
   $1k → $5–10k makes $1000/day much more plausible at 10–20% daily return.

2. **Tune MIN_EDGE_PCT**  
   Backtest with 3%, 4%, 5%; higher edge reduces trade count but improves quality.

3. **Run extended paper trading**  
   Measure win rate, avg trade size, trades/day, then adjust sizing and goal.

4. **Consider more markets**  
   Broaden beyond 15-min crypto if Polymarket adds suitable events.

5. **Improve market timing**  
   ACTIVE_HOURS (9 AM–4 PM ET) is a constraint; test extending or adjusting hours.

---

## 6. Config Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `DAILY_PROFIT_GOAL_USD` | 1000 | Target daily profit ($) |
| `DAILY_LOSS_LIMIT_PCT` | 0.20 | Hard stop at 20% daily loss |
| `PER_TRADE_MAX_LOSS_PCT` | 0.10 | Max 10% bankroll per trade |
| `MIN_EDGE_PCT` | 0.04 | Minimum 4% edge to trade |
| `POSITION_SIZING_MODE` | fractional_kelly | kelly / fractional_kelly / bankroll_pct |
| `KELLY_FRACTION` | 0.5 | Half-Kelly when fractional |
| `MAX_TRADES_PER_HOUR` | 20 | Trade frequency limit |
| `MIN_MARKET_VOLUME_USD` | 1000 | Prefer high-volume markets |
