"""
server.py — FastAPI backend for the Polymarket Bot Dashboard.
Reads trades.csv, bot.log, and bot_state.json; exposes REST + WebSocket endpoints.
Fetches live USDC balance from CLOB API when credentials available.
"""

import asyncio
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import aiofiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Paths relative to project root (server.py lives at project root)
BASE_DIR = Path(__file__).parent
TRADES_CSV = BASE_DIR / "trades.csv"
BOT_LOG = BASE_DIR / "bot.log"
STATE_FILE = BASE_DIR / "bot_state.json"

app = FastAPI(title="Polymarket Bot Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Balance cache (live CLOB API, rate-limited) ──────────────────────────────
_balance_cache: dict = {"value": None, "ts": 0}
BALANCE_CACHE_TTL = 8  # Seconds between balance fetches


async def fetch_live_balance() -> Optional[float]:
    """Fetch USDC balance from Polymarket CLOB API. Cached for 8s to avoid rate limits."""
    global _balance_cache
    now = time.time()
    if _balance_cache["value"] is not None and (now - _balance_cache["ts"]) < BALANCE_CACHE_TTL:
        return _balance_cache["value"]
    try:
        from config import BotConfig
        from clob_client import ClobClient

        cfg = BotConfig()
        if not cfg.API_KEY or not cfg.API_SECRET:
            return None
        if cfg.PAPER_TRADING:
            # In paper mode, derive from state + trades
            return None  # Will use state file
        client = ClobClient(cfg)
        await client.start()
        try:
            resp = await client.get_balance()
            # Handle multiple response formats
            val = None
            if isinstance(resp, (int, float)):
                val = float(resp)
            elif isinstance(resp, dict):
                val = resp.get("balance") or resp.get("usdc") or resp.get("available")
                if val is None and "balances" in resp:
                    bals = resp["balances"]
                    if isinstance(bals, list) and bals:
                        b = bals[0]
                        val = b.get("currentBalance") or b.get("buyingPower") or b.get("assetAvailable")
                if val is not None:
                    val = float(val)
            if val is not None:
                _balance_cache["value"] = round(val, 2)
                _balance_cache["ts"] = now
                return _balance_cache["value"]
        finally:
            await client.close()
    except Exception:
        pass
    return None


def read_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "running": False,
        "paper_trading": True,
        "open_positions": [],
        "bankroll": 1000.0,
        "session_pnl": 0.0,
        "trades_today": 0,
        "win_rate": 0.0,
        "signal_feed": [],
        "uptime_seconds": 0,
    }


def read_trades() -> List[dict]:
    trades = []
    if not TRADES_CSV.exists():
        return trades
    try:
        with open(TRADES_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(dict(row))
    except Exception:
        pass
    return trades


async def tail_log(n: int = 100) -> List[str]:
    if not BOT_LOG.exists():
        return ["[No log file found — bot not yet started]"]
    try:
        async with aiofiles.open(BOT_LOG, "r") as f:
            content = await f.read()
        lines = content.strip().split("\n")
        return lines[-n:]
    except Exception:
        return []


# ── REST Endpoints ─────────────────────────────────────────────────────────────

@app.get("/")
async def serve_dashboard():
    """Serve the dashboard UI."""
    index_path = BASE_DIR / "index.html"
    if not index_path.exists():
        return {"error": "Dashboard not found", "hint": "Create index.html in project root"}
    return FileResponse(index_path)


def _detect_status(state: dict) -> str:
    """Determine bot status: running, stopped, or error."""
    if state.get("running"):
        return "running"
    # When stopped, check if last log lines indicate an error
    try:
        if BOT_LOG.exists():
            with open(BOT_LOG) as f:
                lines = f.readlines()
            for line in lines[-20:]:  # Last 20 lines
                if "| ERROR" in line or "Traceback" in line:
                    return "error"
    except Exception:
        pass
    return "stopped"


@app.get("/api/status")
async def get_status():
    state = read_state()
    trades = read_trades()

    # Compute session stats from trades
    closed = [t for t in trades if t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()]
    session_pnl = sum(
        float(t["pnl_usdc"]) for t in closed
        if t.get("pnl_usdc") and str(t["pnl_usdc"]).strip()
    )
    wins = [t for t in closed if float(t.get("pnl_usdc", 0) or 0) > 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0

    # Today's trades
    today = datetime.utcnow().date().isoformat()
    trades_today = sum(
        1 for t in closed
        if t.get("exit_time", "").startswith(today)
    )

    # Live USDC balance from CLOB API (when live trading + creds); else use state
    live_balance = await fetch_live_balance()
    bankroll = state.get("bankroll", 1000.0)
    if live_balance is not None:
        bankroll = live_balance
    starting_bankroll = state.get("starting_bankroll", bankroll)

    status_str = _detect_status(state)

    return {
        **state,
        "bankroll": round(float(bankroll), 2),
        "starting_bankroll": round(float(starting_bankroll), 2),
        "session_pnl": round(session_pnl, 2),
        "trades_today": trades_today,
        "win_rate": round(win_rate, 1),
        "total_trades": len(closed),
        "balance_source": "live" if live_balance is not None else "state",
        "status": status_str,
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/balance")
async def get_balance():
    """
    Live USDC balance from Polymarket CLOB API.
    Returns state bankroll when API unavailable (paper trading, no creds, or error).
    """
    live = await fetch_live_balance()
    state = read_state()
    bankroll = state.get("bankroll", 1000.0)
    if live is not None:
        bankroll = live
    starting = state.get("starting_bankroll", bankroll)
    session_pnl = 0.0
    trades = read_trades()
    closed = [t for t in trades if t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()]
    for t in closed:
        try:
            session_pnl += float(t.get("pnl_usdc", 0) or 0)
        except (ValueError, TypeError):
            pass
    return {
        "balance_usdc": round(float(bankroll), 2),
        "starting_bankroll": round(float(starting), 2),
        "session_pnl": round(session_pnl, 2),
        "source": "live" if live is not None else "state",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/api/trades")
async def get_trades():
    trades = read_trades()
    return {"trades": trades, "count": len(trades)}


@app.get("/api/logs")
async def get_logs():
    lines = await tail_log(200)
    return {"lines": lines}


@app.get("/api/pnl-series")
async def get_pnl_series():
    trades = read_trades()
    closed = [t for t in trades if t.get("pnl_usdc") and t.get("exit_time")]
    closed.sort(key=lambda t: t.get("exit_time", ""))

    cumulative = 0.0
    series = []
    for t in closed:
        pnl = float(t.get("pnl_usdc", 0))
        cumulative += pnl
        series.append({
            "time": t["exit_time"],
            "pnl": round(pnl, 2),
            "cumulative": round(cumulative, 2),
            "reason": t.get("reason", ""),
        })
    return {"series": series}


# ── WebSocket ──────────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        for ws in list(self.active):
            try:
                await ws.send_json(data)
            except Exception:
                self.active.remove(ws)


manager = ConnectionManager()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            state = read_state()
            trades = read_trades()
            logs = await tail_log(50)

            closed = [t for t in trades if t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()]
            session_pnl = sum(
                float(t["pnl_usdc"]) for t in closed
                if t.get("pnl_usdc") and str(t["pnl_usdc"]).strip()
            )
            wins = [t for t in closed if float(t.get("pnl_usdc", 0) or 0) > 0]
            win_rate = len(wins) / len(closed) * 100 if closed else 0.0

            # Live balance (cached 8s)
            live_balance = await fetch_live_balance()
            bankroll = state.get("bankroll", 1000.0)
            if live_balance is not None:
                bankroll = live_balance

            status_str = _detect_status(state)
            status = {
                **state,
                "bankroll": round(float(bankroll), 2),
                "starting_bankroll": round(float(state.get("starting_bankroll", bankroll)), 2),
                "session_pnl": round(session_pnl, 2),
                "win_rate": round(win_rate, 1),
                "total_trades": len(closed),
                "balance_source": "live" if live_balance is not None else "state",
                "status": status_str,
            }

            payload = {
                "type": "update",
                "status": status,
                "recent_trades": trades[-100:],
                "signal_feed": state.get("signal_feed", []),
                "logs": logs[-50:],
                "timestamp": datetime.utcnow().isoformat(),
            }
            await websocket.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
