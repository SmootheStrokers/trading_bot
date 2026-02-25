# Dashboard Audit — Complete

## Removed

| Item | Reason |
|------|--------|
| **loadDemoData()** | Fake placeholder data; dashboard now shows real/empty state only |
| **Cumulative P&L chart** | Redundant with Equity Curve (same data) |
| **Active Config panel** | All values were hardcoded; replaced with live Bot Health |
| **Duplicate Bot Activity panel** | Merged into Bot Health |
| **Wrong uptime** | Was using page load time; now uses `state.uptime_seconds` |

## Added

| Item | Source |
|------|--------|
| Session P&L % | Computed from session_pnl / starting_bankroll |
| All-time P&L | From trades.csv sum |
| Trade Performance panel | Avg win/loss, profit factor, largest win/loss, streak, win rate today |
| Bot Health panel | Status, last trade, trades/hr, daily loss limit %, min edge, strategies, BTC signal, maker |
| Market Conditions panel | BTC/ETH price, funding rates (CoinGecko+Binance), markets scanned, with edge |
| Daily P&L chart (7d) | /api/chart-data |
| Win/Loss doughnut chart | From trades |
| Time column in trade log | exit_time |
| Time in trade + Size | In position cards |
| Mobile-responsive CSS | @media queries |
| /api/stats | Full trade performance stats |
| /api/config | Live config values |
| /api/market-prices | BTC/ETH price + funding |
| /api/chart-data | Daily P&L 7d, win/loss dist |
| markets_with_edge | Bot now writes to state |

## Data Sources (All Live)

- **Header**: state (bot_state.json) + trades.csv + balance API (8s)
- **Goal tracker**: trades.csv daily sum + config DAILY_PROFIT_GOAL
- **Trade performance**: trades.csv (compute_trade_stats)
- **Positions**: state.open_positions
- **Bot Health**: state + trade_stats + config
- **Market Conditions**: /api/market-prices (CoinGecko, Binance) + state.bot_activity
- **Charts**: trades.csv + /api/chart-data
- **Log**: bot.log

## Update Interval

- WebSocket: 8 seconds
- Balance poll: 8 seconds
- Market prices poll: 8 seconds
- Fallback poll: 8 seconds
