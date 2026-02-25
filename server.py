"""
server.py — FastAPI backend for the Polymarket Bot Dashboard.
Reads trades.csv, bot.log, and bot_state.json; exposes REST + WebSocket endpoints.
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

app = FastAPI(title="Polymarket Bot Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory state (populated by bot via shared state file) ──────────────────
# In production, wire this to PositionManager directly via import or IPC.
# For now we read a state JSON file the bot writes on each cycle.
STATE_FILE = BASE_DIR / "bot_state.json"


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


@app.get("/api/status")
async def get_status():
    state = read_state()
    trades = read_trades()

    # Compute session stats from trades
    closed = [t for t in trades if t.get("pnl_usdc")]
    session_pnl = sum(float(t["pnl_usdc"]) for t in closed if t["pnl_usdc"])
    wins = [t for t in closed if float(t.get("pnl_usdc", 0)) > 0]
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0

    # Today's trades
    today = datetime.utcnow().date().isoformat()
    trades_today = sum(
        1 for t in closed
        if t.get("exit_time", "").startswith(today)
    )

    return {
        **state,
        "session_pnl": round(session_pnl, 2),
        "trades_today": trades_today,
        "win_rate": round(win_rate, 1),
        "total_trades": len(closed),
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

            closed = [t for t in trades if t.get("pnl_usdc")]
            session_pnl = sum(float(t["pnl_usdc"]) for t in closed if t["pnl_usdc"])
            wins = [t for t in closed if float(t.get("pnl_usdc", 0)) > 0]
            win_rate = len(wins) / len(closed) * 100 if closed else 0.0

            payload = {
                "type": "update",
                "status": {
                    **state,
                    "session_pnl": round(session_pnl, 2),
                    "win_rate": round(win_rate, 1),
                    "total_trades": len(closed),
                },
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
