# Polymarket 15-Min Up/Down Trading Bot

**Core philosophy: profit edge or no trade.**

The bot scans all active 15-minute Up/Down markets on Polymarket, evaluates each
against a 4-signal edge filter, and only enters a trade when a strong profit edge
is confirmed. If the setup doesn't meet the bar, the bot sits on its hands.

---

## Architecture

```
main.py
├── market_scanner.py    → Discovers & enriches 15-min Up/Down markets
├── edge_filter.py       → Core profit gate (4 signals)
├── executor.py          → Places & signs CLOB orders
├── position_manager.py  → Monitors exits (TP / SL / time stop)
├── clob_client.py       → Async Polymarket API wrapper
├── models.py            → Shared data structures
└── config.py            → All tunables in one place
```

### Data Flow

```
Every 30s:
  Scanner → fetch active 15-min markets
         → enrich with order book + price history
         → liquidity gate ($500 min depth)

For each market:
  EdgeFilter.evaluate()
    ├── Signal 1: Order Book Imbalance   (bid/ask depth ratio ≥ 60%)
    ├── Signal 2: Momentum               (≥4% move, 70% directional consistency)
    ├── Signal 3: Volume Spike           (2.5× rolling average)
    └── Signal 4: Kelly Criterion        (estimated prob > implied prob + 5%)

  If ≥3 signals fire AND directions agree AND Kelly edge ≥ 5%:
    → Executor places limit buy order
    → PositionManager tracks it

Every 15s:
  PositionManager checks each open position:
    ├── Take profit:  price ≥ 1.8× entry
    ├── Stop loss:    price ≤ 0.35
    └── Time stop:    90s before market resolves → force exit
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Polymarket API keys and bankroll
```

Get your API credentials from: https://polymarket.com → Settings → API Keys

### 3. Paper trading (default: ON)

The bot starts in **paper trading mode** by default: no real orders are placed, and a simulated $1000 balance is used. Trades are logged to `trades.csv` for review.

To enable real money trading only after validating in paper mode:
```bash
# In .env
PAPER_TRADING=false
```

### 4. Configure bot parameters

Edit `config.py` or `.env` to tune:
- `MIN_EDGE_SIGNALS` — how strict the entry gate is (default: 3 of 4)
- `MAX_KELLY_FRACTION` — how much of Kelly to bet (default: 25% = conservative)
- `BANKROLL` — paper balance or real USDC (default: 1000)
- `MAX_POSITIONS` — max simultaneous trades

### 5. Run

```bash
python main.py
```

---

## Key Config Knobs

| Parameter | Default | Effect |
|---|---|---|
| `MIN_EDGE_SIGNALS` | 3 | Raise to 4 for fewer, higher-conviction trades |
| `MAX_KELLY_FRACTION` | 0.25 | Lower = smaller bets, less variance |
| `OB_IMBALANCE_THRESHOLD` | 0.60 | Raise = only trade on extreme OB imbalance |
| `MOMENTUM_MIN_MOVE` | 0.04 | Raise = require stronger price move |
| `VOLUME_SPIKE_MULTIPLIER` | 2.5 | Raise = only on extreme volume spikes |
| `MIN_KELLY_EDGE` | 0.05 | Raise = only take trades with bigger edge |
| `TAKE_PROFIT_MULTIPLIER` | 1.8 | How much profit to target before exit |
| `STOP_LOSS_THRESHOLD` | 0.35 | Absolute price at which to cut losses |

---

## Trade Log

All trades are written to `trades.csv` with:
`condition_id, question, side, entry_price, exit_price, size_usdc, shares, pnl_usdc, entry_time, exit_time, duration_seconds, reason`

---

## Important Notes

### EIP-712 Order Signing
Polymarket CLOB orders require EIP-712 cryptographic signing. The `py-clob-client`
library handles this automatically when initialized with your private key.
The `_build_order_payload` in `executor.py` shows the payload structure — in production
you'll wire this through `py-clob-client`'s `ClobClient.create_order()` which handles signing.

### Kelly Edge Estimation
The current bot uses a fixed `SIGNAL_EDGE_BOOST = 0.08` (8%) on top of implied probability
when signals fire. In a production system, replace this with a trained ML model's
probability output for much more accurate edge estimates.

### Risk Reminder
The bot defaults to **paper trading** (`PAPER_TRADING=true`). Paper trade first to validate strategy and logs. Only set `PAPER_TRADING=false` when ready for real money.
The 25% fractional Kelly setting is conservative but 15-min prediction markets
are inherently noisy. Never trade more than you can afford to lose.

---

## File Structure

```
polymarket_bot/
├── main.py              # Entry point + main loops
├── config.py            # All tunables
├── models.py            # Data structures
├── clob_client.py       # API wrapper
├── market_scanner.py    # Market discovery + enrichment
├── edge_filter.py       # 4-signal profit gate ← most important
├── executor.py          # Order placement
├── position_manager.py  # Exit management + trade logging
├── logger.py            # Logging setup
├── requirements.txt
└── .env.example
```
