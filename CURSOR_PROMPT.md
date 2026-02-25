# Cursor Agent Prompt — Polymarket Bot Dashboard

## Step 1: Project Review

Please read and fully understand the following files in this project before doing anything else:

```
main.py
config.py
models.py
clob_client.py
market_scanner.py
edge_filter.py
executor.py
position_manager.py
state_writer.py
logger.py
```

Pay close attention to:

- **`models.py`** — The core data structures: `Market`, `Position`, `EdgeResult`, `OrderBook`, `PriceTick`, `Side`. These are the data shapes the UI must display.
- **`edge_filter.py`** — The 4-signal profit gate: `ob_imbalance_signal`, `momentum_signal`, `volume_signal`, `kelly_signal`. The UI must visualize which signals fired per trade.
- **`position_manager.py`** — Exit logic (TAKE_PROFIT, STOP_LOSS, TIME_STOP, SHUTDOWN) and the `trades.csv` log format.
- **`config.py`** — All tunable parameters. The UI should display the active config values.
- **`main.py`** — The two async loops: `scan_loop` and `monitor_loop`. The UI should reflect both.
- **`state_writer.py`** — Writes `bot_state.json` for the dashboard. Must be called from `scan_loop` each cycle so the dashboard receives live status.

---

## Step 2: Build the Dashboard

Build a **`dashboard/`** folder containing a complete monitoring dashboard for this bot.

### Tech Stack
- **React + Vite** (or plain HTML/CSS/JS if preferred for simplicity)
- Tailwind CSS or pure CSS
- Recharts (or Chart.js) for price/PnL charts
- WebSocket or polling from a small FastAPI backend (`server.py`)

### Backend (`server.py`)
Build a FastAPI server that:
1. Reads `trades.csv` and exposes it via `GET /api/trades`
2. Reads `bot.log` (tail last 200 lines) via `GET /api/logs`
3. Exposes a `GET /api/status` endpoint returning:
   - `running: bool`
   - `open_positions: list` (from PositionManager state)
   - `bankroll: float`
   - `session_pnl: float`
   - `trades_today: int`
   - `win_rate: float`
4. WebSocket at `ws://localhost:8000/ws` that streams live log lines and position updates every 2 seconds

### Frontend Requirements

The dashboard must display:

#### Header / Status Bar
- Bot status indicator (RUNNING / STOPPED) with animated pulse
- Live bankroll and session P&L (green/red)
- Current time + uptime counter
- Active config: `MIN_EDGE_SIGNALS`, `MAX_KELLY_FRACTION`, `BANKROLL`

#### Open Positions Panel
For each open position show:
- Market question (truncated)
- Side (YES/NO) with color coding
- Entry price → current price with live delta
- Unrealized P&L in $ and %
- Time remaining bar (countdown to market resolution)
- Which exit condition is closest (TP / SL / TIME)

#### Signal Feed
Live stream of EdgeResult evaluations:
- Market name
- 4 signal pills: `OB` `MOM` `VOL` `KELLY` — green if fired, gray if not
- Signal count badge (e.g. "3/4")
- Side direction arrow
- Kelly size and edge %
- ENTERED badge if trade was taken, SKIPPED if not

#### Trade History Table
From `trades.csv`:
- All columns visible
- Color-coded P&L (green profit / red loss)
- Reason badges (TAKE_PROFIT / STOP_LOSS / TIME_STOP)
- Sortable by time, P&L, duration
- Session summary row at bottom: total trades, win rate, total P&L

#### P&L Chart
- Cumulative P&L over time (line chart)
- Per-trade P&L bar chart

#### Live Log Tail
- Last 50 log lines from `bot.log`
- Color-coded by level: INFO (white), WARNING (amber), ERROR (red)
- Auto-scrolling

---

## Design Direction

**Aesthetic: Dark terminal-finance hybrid.**

Think Bloomberg Terminal meets a modern quant trading desk — not a generic crypto dashboard.

- Background: near-black `#0a0a0f` with subtle grid texture
- Accent color: cold electric blue `#00d4ff` for active/profit states
- Loss color: sharp red `#ff3a5c`
- Font: `JetBrains Mono` or `IBM Plex Mono` for numbers/data, a refined sans for labels
- Panels: dark glass cards with `1px` borders in `rgba(255,255,255,0.06)`
- Numbers animate when they update (count-up effect)
- Signal pills have a subtle glow when active
- Time remaining bar depletes with color shift (blue → amber → red as time runs out)
- No gradients on backgrounds — use solid darks with sharp accent lines

The overall feel: you are watching a precision machine execute its strategy. Clinical. Sharp. Authoritative.

---

## File Output

Create the following files (or equivalent structure):

```
polymarket/
├── server.py           # FastAPI backend (reads trades.csv, bot.log, bot_state.json)
├── index.html          # Main dashboard (plain HTML/CSS/JS or React entry point)
├── requirements.txt    # Include: fastapi, uvicorn, aiofiles, websockets
```

**For React/Vite**: Alternatively use `dashboard/` with `src/`, `components/`, `hooks/`, and `package.json`.

**Verification**: Run the bot (`python main.py`), then start the dashboard (`uvicorn server:app --reload` or `python server.py`), and open `index.html` in a browser. Ensure `state_writer.write_state()` is called from `main.py`'s scan loop so the dashboard receives live bot state.
