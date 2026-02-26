"""
server.py — FastAPI backend for the Polymarket Bot Dashboard.
Reads trades.csv, bot.log, and bot_state.json; exposes REST + WebSocket endpoints.
Fetches live USDC balance from CLOB API when credentials available.
All metrics computed from trades.csv and bot_state.json — no placeholder data.
"""

import asyncio
import csv
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import aiofiles
import aiohttp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Paths relative to project root (server.py lives at project root)
BASE_DIR = Path(__file__).parent
TRADES_CSV = BASE_DIR / "trades.csv"
STATE_FILE = BASE_DIR / "bot_state.json"
UPDATE_INTERVAL_SEC = 8  # Dashboard refresh interval (balance, status)


def _get_log_path() -> Path:
    """Use same LOG_FILE as bot (from config/env) so dashboard matches terminal output."""
    try:
        from config import BotConfig
        log_file = BotConfig().LOG_FILE or "bot.log"
        p = Path(log_file)
        return p if p.is_absolute() else BASE_DIR / log_file
    except Exception:
        return BASE_DIR / "bot.log"

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


def compute_trade_stats(trades: List[dict], today: str) -> dict:
    """Compute comprehensive trade performance stats from trades.csv."""
    closed = [t for t in trades if t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()]
    wins = [t for t in closed if float(t.get("pnl_usdc", 0) or 0) > 0]
    losses = [t for t in closed if float(t.get("pnl_usdc", 0) or 0) <= 0]
    today_trades = [t for t in closed if (t.get("exit_time") or "").startswith(today)]

    total_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in closed)
    today_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in today_trades)
    total_wins = sum(float(t.get("pnl_usdc", 0)) for t in wins)
    total_losses = abs(sum(float(t.get("pnl_usdc", 0)) for t in losses))

    avg_win = total_wins / len(wins) if wins else 0
    avg_loss = total_losses / len(losses) if losses else 0
    profit_factor = total_wins / total_losses if total_losses > 0 else (float("inf") if total_wins > 0 else 0)
    largest_win = max((float(t.get("pnl_usdc", 0)) for t in wins), default=0)
    largest_loss = min((float(t.get("pnl_usdc", 0)) for t in losses), default=0)

    # Win/loss streak (from most recent)
    streak = 0
    streak_type = None
    for t in reversed(closed):
        pnl = float(t.get("pnl_usdc", 0) or 0)
        if streak == 0:
            streak_type = "win" if pnl > 0 else "loss"
            streak = 1
        elif (pnl > 0 and streak_type == "win") or (pnl <= 0 and streak_type == "loss"):
            streak += 1
        else:
            break

    today_wins = [t for t in today_trades if float(t.get("pnl_usdc", 0) or 0) > 0]
    win_rate_today = len(today_wins) / len(today_trades) * 100 if today_trades else None
    win_rate_all = len(wins) / len(closed) * 100 if closed else 0

    # Trades per hour: today's trades / hours elapsed today
    now = datetime.utcnow()
    hours_elapsed = now.hour + now.minute / 60 + now.second / 3600
    trades_per_hour = len(today_trades) / hours_elapsed if hours_elapsed > 0 and today_trades else 0

    # Last trade time
    last_trade_time = None
    if closed:
        exits = [(t, t.get("exit_time", "")) for t in closed]
        exits = [x for x in exits if x[1]]
        if exits:
            exits.sort(key=lambda x: x[1], reverse=True)
            last_trade_time = exits[0][1]

    return {
        "total_trades": len(closed),
        "trades_today": len(today_trades),
        "all_time_pnl": round(total_pnl, 2),
        "today_pnl": round(today_pnl, 2),
        "win_rate_all": round(win_rate_all, 1),
        "win_rate_today": round(win_rate_today, 1) if win_rate_today is not None else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "streak": streak,
        "streak_type": streak_type,
        "trades_per_hour": round(trades_per_hour, 1),
        "last_trade_time": last_trade_time,
        "wins": len(wins),
        "losses": len(losses),
    }


def get_config_values() -> dict:
    """Read key config values for dashboard (min edge, loss limit, etc.)."""
    try:
        from config import BotConfig
        c = BotConfig()
        return {
            "min_edge_pct": round(c.MIN_EDGE_PCT * 100, 1),
            "daily_loss_limit_pct": round(c.DAILY_LOSS_LIMIT_PCT * 100, 0),
            "daily_goal_usd": c.DAILY_PROFIT_GOAL_USD,
            "max_positions": c.MAX_POSITIONS,
            "max_bet_size": c.MAX_BET_SIZE,
            "min_edge_signals": c.MIN_EDGE_SIGNALS,
        }
    except Exception:
        return {}


async def tail_log(n: int = 100) -> List[str]:
    log_path = _get_log_path()
    if not log_path.exists():
        return ["[No log file found — bot not yet started]"]
    try:
        async with aiofiles.open(log_path, "r") as f:
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
        log_path = _get_log_path()
        if log_path.exists():
            with open(log_path) as f:
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
    today = datetime.utcnow().date().isoformat()

    # Full stats
    stats = compute_trade_stats(trades, today)
    closed = [t for t in trades if t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()]
    session_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in closed)

    # Live USDC balance from CLOB API
    live_balance = await fetch_live_balance()
    bankroll = float(state.get("bankroll", 1000.0))
    if live_balance is not None:
        bankroll = live_balance
    starting_bankroll = float(state.get("starting_bankroll", bankroll))

    # Session P&L %
    session_pnl_pct = (session_pnl / starting_bankroll * 100) if starting_bankroll else 0

    try:
        from config import BotConfig
        goal = BotConfig().DAILY_PROFIT_GOAL_USD
    except Exception:
        goal = 1000.0

    risk_state = (state.get("bot_activity") or {}).get("risk_state") or {}
    daily_loss_limit = bankroll * 0.20  # 20%
    daily_pnl = stats.get("today_pnl", 0)
    loss_limit_used_pct = (abs(daily_pnl) / daily_loss_limit * 100) if daily_pnl < 0 and daily_loss_limit > 0 else 0

    goal_tracking = {
        "daily_pnl": stats.get("today_pnl", 0),
        "daily_goal_usd": round(goal, 2),
        "progress_pct": round(min(100, max(0, (daily_pnl / goal) * 100)), 1),
    }

    return {
        **state,
        "bankroll": round(bankroll, 2),
        "starting_bankroll": round(starting_bankroll, 2),
        "session_pnl": round(session_pnl, 2),
        "session_pnl_pct": round(session_pnl_pct, 2),
        "all_time_pnl": stats.get("all_time_pnl", 0),
        "trades_today": stats.get("trades_today", 0),
        "win_rate": stats.get("win_rate_all", 0),
        "win_rate_today": stats.get("win_rate_today"),
        "total_trades": stats.get("total_trades", 0),
        "balance_source": "live" if live_balance is not None else "state",
        "status": _detect_status(state),
        "goal_tracking": goal_tracking,
        "trade_stats": stats,
        "config": get_config_values(),
        "risk_state": risk_state,
        "daily_loss_limit_used_pct": round(loss_limit_used_pct, 1),
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


@app.get("/api/stats")
async def get_stats():
    """Full trade performance stats: avg win/loss, profit factor, streak, etc."""
    trades = read_trades()
    today = datetime.utcnow().date().isoformat()
    return compute_trade_stats(trades, today)


@app.get("/api/config")
async def get_config():
    """Key config values for dashboard (min edge, loss limit, etc.)."""
    return get_config_values()


@app.get("/api/market-prices")
async def get_market_prices():
    """BTC/ETH price and funding rates from CoinGecko + Binance. May fail in geo-restricted regions."""
    result = {"btc_usd": None, "eth_usd": None, "btc_24h_change": None, "eth_24h_change": None, "btc_funding": None, "eth_funding": None}
    try:
        async with aiohttp.ClientSession() as session:
            # CoinGecko for spot (US-accessible)
            url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    btc = data.get("bitcoin", {})
                    eth = data.get("ethereum", {})
                    result["btc_usd"] = btc.get("usd")
                    result["eth_usd"] = eth.get("usd")
                    result["btc_24h_change"] = btc.get("usd_24h_change")
                    result["eth_24h_change"] = eth.get("usd_24h_change")
            # Binance funding (may 451 in US)
            for sym, key in [("BTCUSDT", "btc_funding"), ("ETHUSDT", "eth_funding")]:
                try:
                    furl = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
                    async with session.get(furl, timeout=aiohttp.ClientTimeout(total=3)) as fr:
                        if fr.status == 200:
                            fd = await fr.json()
                            rate = float(fd.get("lastFundingRate", 0))
                            result[key] = round(rate * 100, 4)  # as %
                except Exception:
                    pass
    except Exception:
        pass
    return result


@app.get("/api/goal-tracking")
async def get_goal_tracking():
    """$1000/day goal tracker: daily P&L progress, projected total, days to double."""
    state = read_state()
    trades = read_trades()
    today = datetime.utcnow().date().isoformat()

    closed = [t for t in trades if t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()]
    daily_pnl = sum(
        float(t["pnl_usdc"]) for t in closed
        if t.get("exit_time", "").startswith(today)
    )
    try:
        from config import BotConfig
        goal = BotConfig().DAILY_PROFIT_GOAL_USD
    except Exception:
        goal = 1000.0

    bankroll = float(state.get("bankroll", 1000))
    starting = float(state.get("starting_bankroll", bankroll))

    # Projected daily total: extrapolate from hourly pace if we have trades
    trades_today = [t for t in closed if t.get("exit_time", "").startswith(today)]
    hours_elapsed = datetime.utcnow().hour + datetime.utcnow().minute / 60
    if hours_elapsed > 0 and len(trades_today) > 0:
        pace = daily_pnl / hours_elapsed
        projected = pace * 24
    else:
        projected = daily_pnl

    # Days to double bankroll at current daily rate
    if daily_pnl > 0 and bankroll > 0:
        daily_return_pct = daily_pnl / bankroll
        if daily_return_pct > 0:
            import math
            days_to_double = math.log(2) / math.log(1 + daily_return_pct)
        else:
            days_to_double = float("inf")
    else:
        days_to_double = None

    risk_state = (state.get("bot_activity") or {}).get("risk_state") or {}
    return {
        "daily_pnl": round(daily_pnl, 2),
        "daily_goal_usd": round(goal, 2),
        "progress_pct": round(min(100, max(0, (daily_pnl / goal) * 100)), 1),
        "projected_daily_total": round(projected, 2),
        "days_to_double": round(days_to_double, 1) if days_to_double and days_to_double != float("inf") else None,
        "trades_today": len(trades_today),
        "trading_paused": risk_state.get("trading_paused", False),
        "pause_reason": risk_state.get("pause_reason", ""),
    }


REPORTS_DIR = BASE_DIR / "reports"


def list_reports(limit: int = 7) -> List[dict]:
    """List last N daily reports (not weekly)."""
    if not REPORTS_DIR.exists():
        return []
    reports = []
    for f in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        if f.stem.startswith("weekly-"):
            continue
        if len(reports) >= limit:
            break
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("report_type") == "daily":
                reports.append({
                    "date": data.get("date", f.stem),
                    "summary": data.get("performance_summary", {}),
                    "path": str(f),
                })
        except Exception:
            pass
    return reports


@app.get("/api/reports")
async def get_reports():
    """List last 7 daily reports for dashboard."""
    return {"reports": list_reports(7)}


def _valid_report_date(date: str) -> bool:
    """Ensure date is YYYY-MM-DD format to prevent path traversal."""
    if not date or len(date) != 10:
        return False
    try:
        datetime.strptime(date, "%Y-%m-%d")
        return True
    except ValueError:
        return False


@app.get("/api/reports/{date}")
async def get_report_by_date(date: str):
    """Get full report for a specific date (YYYY-MM-DD)."""
    if not _valid_report_date(date):
        return {"error": "Invalid date format", "date": date}
    path = REPORTS_DIR / f"{date}.json"
    if not path.exists():
        return {"error": "Report not found", "date": date}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/generate-report")
async def generate_report(send_email: bool = False, send_discord: bool = False):
    """
    Manually trigger daily report generation.
    Returns the generated report. Optionally send via email/discord.
    """
    try:
        from daily_report import run_daily_report, generate_daily_report
        report = run_daily_report(send_email_flag=send_email, send_discord_flag=send_discord)
        return {"success": True, "report": report, "date": report.get("date")}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.get("/api/reports/{date}/csv")
async def download_report_csv(date: str):
    """Download report as CSV."""
    if not _valid_report_date(date):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Invalid date format"}, status_code=400)
    path = REPORTS_DIR / f"{date}.json"
    if not path.exists():
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "Report not found"}, status_code=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        ps = data.get("performance_summary", {})
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Date", data.get("date")])
        writer.writerow(["Net P&L (USD)", ps.get("net_pnl_usd")])
        writer.writerow(["Net P&L (%)", ps.get("net_pnl_pct")])
        writer.writerow(["Trades", ps.get("total_trades")])
        writer.writerow(["Win Rate", ps.get("win_rate")])
        writer.writerow(["Starting Bankroll", ps.get("starting_bankroll")])
        writer.writerow(["Ending Bankroll", ps.get("ending_bankroll")])
        for name, val in data.get("crypto_performance", {}).get("by_strategy", {}).items():
            writer.writerow([f"Strategy: {name}", val.get("pnl")])
        from fastapi.responses import Response
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=report-{date}.csv"}
        )
    except Exception as e:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(e)}, status_code=500)


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


@app.get("/api/chart-data")
async def get_chart_data():
    """Chart data: daily P&L last 7 days, win/loss distribution, trade frequency by hour."""
    trades = read_trades()
    closed = [t for t in trades if t.get("pnl_usdc") and t.get("exit_time")]
    today = datetime.utcnow().date().isoformat()

    # Daily P&L last 7 days
    daily_pnl = defaultdict(float)
    for i in range(7):
        d = (datetime.utcnow() - timedelta(days=i)).date().isoformat()
        daily_pnl[d] = 0
    for t in closed:
        et = (t.get("exit_time") or "")[:10]
        if et in daily_pnl:
            daily_pnl[et] += float(t.get("pnl_usdc", 0) or 0)
    daily_bars = [{"date": d, "pnl": round(daily_pnl[d], 2)} for d in sorted(daily_pnl.keys(), reverse=True)]

    # Win/loss distribution (bin by P&L range)
    pnls = [float(t.get("pnl_usdc", 0)) for t in closed]
    win_loss_dist = {"wins": len([p for p in pnls if p > 0]), "losses": len([p for p in pnls if p <= 0])}

    # Trade frequency by hour (UTC) - today only
    by_hour = defaultdict(int)
    for t in closed:
        et = t.get("exit_time", "")
        if et.startswith(today) and "T" in et:
            try:
                h = int(et[11:13])
                by_hour[h] += 1
            except (ValueError, IndexError):
                pass
    freq_by_hour = [{"hour": h, "count": by_hour.get(h, 0)} for h in range(24)]

    # Exit reason distribution
    by_reason = defaultdict(int)
    for t in closed:
        r = t.get("reason") or "OTHER"
        by_reason[r] += 1
    exit_reason_dist = [{"reason": k, "count": v} for k, v in sorted(by_reason.items(), key=lambda x: -x[1])]

    # P&L histogram buckets ($ ranges)
    bucket_edges = [-float("inf"), -25, -10, -5, 0, 5, 10, 25, float("inf")]
    bucket_labels = ["<-$25", "$-25 to -10", "$-10 to -5", "$-5 to 0", "$0 to 5", "$5 to 10", "$10 to 25", ">$25"]
    pnl_buckets = [0] * (len(bucket_edges) - 1)
    for t in closed:
        p = float(t.get("pnl_usdc", 0) or 0)
        for i in range(len(bucket_edges) - 1):
            if bucket_edges[i] <= p < bucket_edges[i + 1]:
                pnl_buckets[i] += 1
                break
    pnl_histogram = [{"label": bucket_labels[i], "count": pnl_buckets[i]} for i in range(len(bucket_labels))]

    # Daily trade count last 14 days
    daily_count = defaultdict(int)
    for i in range(14):
        d = (datetime.utcnow() - timedelta(days=i)).date().isoformat()
        daily_count[d] = 0
    for t in closed:
        et = (t.get("exit_time") or "")[:10]
        if et in daily_count:
            daily_count[et] += 1
    daily_trades_14d = [{"date": d, "count": daily_count[d]} for d in sorted(daily_count.keys(), reverse=True)]

    return {
        "daily_pnl_7d": daily_bars,
        "win_loss_dist": win_loss_dist,
        "trades_by_hour": freq_by_hour,
        "exit_reason_dist": exit_reason_dist,
        "pnl_histogram": pnl_histogram,
        "daily_trades_14d": daily_trades_14d,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            state = read_state()
            trades = read_trades()
            logs = await tail_log(50)
            today = datetime.utcnow().date().isoformat()
            stats = compute_trade_stats(trades, today)

            live_balance = await fetch_live_balance()
            bankroll = float(state.get("bankroll", 1000.0))
            if live_balance is not None:
                bankroll = live_balance
            starting = float(state.get("starting_bankroll", bankroll))
            session_pnl = stats.get("all_time_pnl", 0)  # All trades in file = session
            session_pnl_pct = (session_pnl / starting * 100) if starting else 0

            try:
                from config import BotConfig
                goal = BotConfig().DAILY_PROFIT_GOAL_USD
            except Exception:
                goal = 1000.0
            daily_pnl = stats.get("today_pnl", 0)
            risk_state = (state.get("bot_activity") or {}).get("risk_state") or {}

            status = {
                **state,
                "bankroll": round(bankroll, 2),
                "starting_bankroll": round(starting, 2),
                "session_pnl": round(session_pnl, 2),
                "session_pnl_pct": round(session_pnl_pct, 2),
                "all_time_pnl": stats.get("all_time_pnl", 0),
                "trades_today": stats.get("trades_today", 0),
                "win_rate": stats.get("win_rate_all", 0),
                "win_rate_today": stats.get("win_rate_today"),
                "total_trades": stats.get("total_trades", 0),
                "balance_source": "live" if live_balance is not None else "state",
                "status": _detect_status(state),
                "goal_tracking": {
                    "daily_pnl": round(daily_pnl, 2),
                    "daily_goal_usd": round(goal, 2),
                    "progress_pct": round(min(100, max(0, (daily_pnl / goal) * 100)), 1),
                },
                "trade_stats": stats,
                "config": get_config_values(),
                "risk_state": risk_state,
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
            await asyncio.sleep(UPDATE_INTERVAL_SEC)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)
