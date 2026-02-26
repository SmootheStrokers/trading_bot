"""
daily_report.py — Generates and delivers daily and weekly trading reports.
Runs at 11:59 PM UTC via scheduler in main.py. Also callable via /api/generate-report.
"""

import csv
import json
import os
import smtplib
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("daily_report")

BASE_DIR = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"
TRADES_CSV = BASE_DIR / "trades.csv"
STATE_FILE = BASE_DIR / "bot_state.json"
SESSION_START_FILE = BASE_DIR / "session_start.json"

# Strategy display names for reports
STRATEGY_DISPLAY = {
    "BTC_MOMENTUM": "BTC Momentum",
    "ETH_LAG": "ETH Lag",
    "SOL_SQUEEZE": "SOL Squeeze",
    "MAKER": "Maker",
    "XRP_CATALYST": "XRP Catalyst",
    "ORPHAN": "Orphan",
    "": "Other",
}


def _load_config() -> dict:
    """Load report-related config from env."""
    def _get(k: str, default: Any = None) -> Any:
        v = os.getenv(k)
        if v is None or v == "":
            return default
        if isinstance(default, bool):
            return v.lower() in ("true", "1", "yes")
        if isinstance(default, int):
            try:
                return int(v)
            except ValueError:
                return default
        return v

    return {
        "daily_report_enabled": _get("DAILY_REPORT_ENABLED", True),
        "report_email_to": _get("REPORT_EMAIL_TO", ""),
        "report_email_from": _get("REPORT_EMAIL_FROM", ""),
        "report_email_password": _get("REPORT_EMAIL_PASSWORD", ""),
        "report_send_time_utc": _get("REPORT_SEND_TIME_UTC", "23:59"),
        "discord_webhook_url": _get("DISCORD_WEBHOOK_URL", ""),
        "weekly_report_day": _get("WEEKLY_REPORT_DAY", "sunday").lower(),
    }


def _read_trades() -> List[dict]:
    """Read all trades from trades.csv."""
    try:
        from config import BotConfig
        path = Path(BotConfig().TRADE_LOG_FILE)
    except Exception:
        path = TRADES_CSV
    if not path.exists():
        return []
    trades = []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(dict(row))
    except Exception as e:
        logger.warning(f"Could not read trades: {e}")
    return trades


def _read_state() -> dict:
    """Read bot_state.json."""
    if not STATE_FILE.exists():
        return {}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_session_start() -> dict:
    """Read session_start.json for starting bankroll."""
    if not SESSION_START_FILE.exists():
        return {}
    try:
        with open(SESSION_START_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _compute_bankroll_from_trades(starting: float, trades: List[dict], up_to_date: Optional[str] = None) -> float:
    """Compute bankroll = starting + sum of pnl up to (and including) up_to_date."""
    total = starting
    for t in trades:
        et = (t.get("exit_time") or "")[:10]
        if up_to_date and et and et > up_to_date:
            continue
        pnl = t.get("pnl_usdc")
        if pnl and str(pnl).strip():
            try:
                total += float(pnl)
            except (ValueError, TypeError):
                pass
    return total


def _is_nba_market(question: str) -> bool:
    """Detect NBA-related markets from question text."""
    q = (question or "").lower()
    return "nba" in q or "basketball" in q or "lebron" in q or "lakers" in q or "warriors" in q


def _get_strategy_display(s: str) -> str:
    return STRATEGY_DISPLAY.get((s or "").strip().upper(), (s or "Other").replace("_", " "))


def _compute_streaks(trades: List[dict]) -> tuple:
    """Return (max_win_streak, max_loss_streak) from ordered trades."""
    if not trades:
        return 0, 0
    ordered = sorted(trades, key=lambda t: t.get("exit_time", ""))
    max_win, max_loss = 0, 0
    cur_win, cur_loss = 0, 0
    for t in ordered:
        pnl = float(t.get("pnl_usdc", 0) or 0)
        if pnl > 0:
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)
    return max_win, max_loss


def _fetch_market_conditions() -> dict:
    """Fetch BTC/ETH prices and funding rates (async-friendly via aiohttp if available)."""
    result = {
        "btc_usd": None,
        "eth_usd": None,
        "btc_24h_change": None,
        "eth_24h_change": None,
        "btc_funding": None,
        "eth_funding": None,
    }
    try:
        import aiohttp
        import asyncio

        async def _fetch():
            async with aiohttp.ClientSession() as session:
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
                for sym, key in [("BTCUSDT", "btc_funding"), ("ETHUSDT", "eth_funding")]:
                    try:
                        furl = f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={sym}"
                        async with session.get(furl, timeout=aiohttp.ClientTimeout(total=3)) as fr:
                            if fr.status == 200:
                                fd = await fr.json()
                                result[key] = round(float(fd.get("lastFundingRate", 0)) * 100, 4)
                    except Exception:
                        pass

        asyncio.run(_fetch())
    except Exception as e:
        logger.debug(f"Market conditions fetch failed: {e}")
    return result


def generate_daily_report(date_str: Optional[str] = None) -> dict:
    """
    Generate a full daily report for the given date (YYYY-MM-DD).
    If date_str is None, uses today UTC.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).date().isoformat()

    trades = _read_trades()
    state = _read_state()
    session = _read_session_start()

    # Trades that closed on this day
    day_trades = [
        t for t in trades
        if (t.get("exit_time") or "").startswith(date_str)
        and t.get("pnl_usdc") and str(t.get("pnl_usdc", "")).strip()
    ]
    closed_day = [t for t in day_trades if t.get("pnl_usdc")]
    pnls = [float(t.get("pnl_usdc", 0) or 0) for t in closed_day]

    # Bankroll
    starting_bankroll = float(session.get("starting_bankroll", state.get("bankroll", 1000)))
    ending_bankroll = _compute_bankroll_from_trades(starting_bankroll, trades, up_to_date=date_str)

    # Bankroll at start of day = cumulative after previous day
    prev_date = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).date().isoformat()
    bankroll_start_of_day = _compute_bankroll_from_trades(starting_bankroll, trades, up_to_date=prev_date)

    net_pnl = sum(pnls)
    net_pnl_pct = (net_pnl / bankroll_start_of_day * 100) if bankroll_start_of_day else 0

    # Goal
    try:
        from config import BotConfig
        daily_goal = BotConfig().DAILY_PROFIT_GOAL_USD
    except Exception:
        daily_goal = 1000.0
    goal_progress_pct = min(100, max(0, (net_pnl / daily_goal) * 100)) if daily_goal else 0

    # Best/worst trade
    best_trade = None
    worst_trade = None
    if closed_day:
        wins = [t for t in closed_day if float(t.get("pnl_usdc", 0) or 0) > 0]
        losses = [t for t in closed_day if float(t.get("pnl_usdc", 0) or 0) <= 0]
        if wins:
            best = max(wins, key=lambda t: float(t.get("pnl_usdc", 0)))
            best_trade = {
                "market": (best.get("question") or "")[:60],
                "size_usdc": float(best.get("size_usdc", 0) or 0),
                "profit": float(best.get("pnl_usdc", 0) or 0),
            }
        if losses:
            worst = min(losses, key=lambda t: float(t.get("pnl_usdc", 0)))
            worst_trade = {
                "market": (worst.get("question") or "")[:60],
                "size_usdc": float(worst.get("size_usdc", 0) or 0),
                "loss": float(worst.get("pnl_usdc", 0) or 0),
            }

    # Win rate, profit factor, streaks
    wins_count = len([t for t in closed_day if float(t.get("pnl_usdc", 0) or 0) > 0])
    win_rate = (wins_count / len(closed_day) * 100) if closed_day else 0
    total_wins = sum(float(t.get("pnl_usdc", 0)) for t in closed_day if float(t.get("pnl_usdc", 0) or 0) > 0)
    total_losses = abs(sum(float(t.get("pnl_usdc", 0)) for t in closed_day if float(t.get("pnl_usdc", 0) or 0) <= 0))
    profit_factor = total_wins / total_losses if total_losses > 0 else (float("inf") if total_wins > 0 else 0)
    max_win_streak, max_loss_streak = _compute_streaks(closed_day)

    # Crypto by strategy
    crypto_strategies = ["BTC_MOMENTUM", "ETH_LAG", "SOL_SQUEEZE", "MAKER", "XRP_CATALYST"]
    strategy_pnl = defaultdict(float)
    strategy_trades = defaultdict(int)
    nba_trades = []
    crypto_trades = []
    for t in closed_day:
        strat = (t.get("strategy") or "").strip() or "OTHER"
        pnl = float(t.get("pnl_usdc", 0) or 0)
        strategy_pnl[strat] += pnl
        strategy_trades[strat] += 1
        if _is_nba_market(t.get("question") or ""):
            nba_trades.append(t)
        else:
            crypto_trades.append(t)

    best_strategy = None
    worst_strategy = None
    if strategy_pnl:
        sorted_strats = sorted(strategy_pnl.items(), key=lambda x: -x[1])
        if sorted_strats:
            best_strategy = sorted_strats[0][0]
            worst_strategy = sorted_strats[-1][0]

    nba_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in nba_trades)
    nba_wins = len([t for t in nba_trades if float(t.get("pnl_usdc", 0) or 0) > 0])
    nba_win_rate = (nba_wins / len(nba_trades) * 100) if nba_trades else None
    best_nba = max(nba_trades, key=lambda t: float(t.get("pnl_usdc", 0) or 0)) if nba_trades else None
    worst_nba = min(nba_trades, key=lambda t: float(t.get("pnl_usdc", 0) or 0)) if nba_trades else None

    # Risk summary
    risk_state = (state.get("bot_activity") or {}).get("risk_state") or {}
    loss_limit_triggered = risk_state.get("trading_paused", False) and "loss" in (risk_state.get("pause_reason") or "").lower()
    daily_loss_limit_usdc = float(state.get("bankroll", 1000)) * 0.20
    max_drawdown = 0.0  # Approximate from equity curve
    loss_limit_used_pct = (abs(net_pnl) / daily_loss_limit_usdc * 100) if net_pnl < 0 and daily_loss_limit_usdc > 0 else 0
    rejected_count = 0  # Not currently tracked; placeholder
    rejected_reasons = []

    # Market conditions
    market_cond = _fetch_market_conditions()
    markets_scanned = (state.get("bot_activity") or {}).get("markets_last_scan", 0)
    markets_with_edge = (state.get("bot_activity") or {}).get("markets_with_edge", 0)

    # Average edge on trades taken (from signal_feed if available - we don't store per trade)
    avg_edge = 0.0  # Placeholder; would need kelly_edge in trades

    report = {
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_type": "daily",
        "performance_summary": {
            "starting_bankroll": round(bankroll_start_of_day, 2),
            "ending_bankroll": round(ending_bankroll, 2),
            "net_pnl_usd": round(net_pnl, 2),
            "net_pnl_pct": round(net_pnl_pct, 2),
            "daily_goal_usd": daily_goal,
            "goal_progress_pct": round(goal_progress_pct, 1),
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "total_trades": len(closed_day),
            "win_rate": round(win_rate, 1),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
            "largest_win_streak": max_win_streak,
            "largest_loss_streak": max_loss_streak,
        },
        "crypto_performance": {
            "by_strategy": {
                _get_strategy_display(s): {"pnl": round(v, 2), "trades": strategy_trades.get(s, 0)}
                for s, v in strategy_pnl.items()
            },
            "best_strategy": _get_strategy_display(best_strategy) if best_strategy else None,
            "worst_strategy": _get_strategy_display(worst_strategy) if worst_strategy else None,
            "crypto_trades": len(crypto_trades),
            "nba_trades": len(nba_trades),
        },
        "nba_performance": {
            "nba_pnl": round(nba_pnl, 2),
            "nba_win_rate": round(nba_win_rate, 1) if nba_win_rate is not None else None,
            "best_nba": {
                "market": (best_nba.get("question") or "")[:60],
                "pnl": round(float(best_nba.get("pnl_usdc", 0) or 0), 2),
            } if best_nba else None,
            "worst_nba": {
                "market": (worst_nba.get("question") or "")[:60],
                "pnl": round(float(worst_nba.get("pnl_usdc", 0) or 0), 2),
            } if worst_nba else None,
            "injury_signals": "No injury signal tracking",
        },
        "risk_summary": {
            "loss_limit_triggered": loss_limit_triggered,
            "max_drawdown_pct": round(max_drawdown, 2),
            "loss_limit_used_pct": round(loss_limit_used_pct, 1),
            "rejected_trades_count": rejected_count,
            "rejected_reasons": rejected_reasons,
        },
        "market_conditions": {
            "btc_usd": market_cond.get("btc_usd"),
            "eth_usd": market_cond.get("eth_usd"),
            "btc_24h_change_pct": market_cond.get("btc_24h_change"),
            "eth_24h_change_pct": market_cond.get("eth_24h_change"),
            "btc_funding_pct": market_cond.get("btc_funding"),
            "eth_funding_pct": market_cond.get("eth_funding"),
            "markets_scanned": markets_scanned,
            "markets_with_edge": markets_with_edge,
            "avg_edge_pct": round(avg_edge * 100, 2),
        },
        "equity_curve": _build_equity_curve(closed_day, bankroll_start_of_day),
    }
    return report


def _build_equity_curve(trades: List[dict], start_bankroll: float) -> List[dict]:
    """Build data points for ASCII equity curve: [{"time": "...", "bankroll": n}, ...]."""
    ordered = sorted(trades, key=lambda t: t.get("exit_time", ""))
    curve = [{"time": "Start", "bankroll": round(start_bankroll, 2)}]
    cum = start_bankroll
    for t in ordered:
        pnl = float(t.get("pnl_usdc", 0) or 0)
        cum += pnl
        et = t.get("exit_time", "")
        curve.append({"time": et[11:19] if len(et) >= 19 else et, "bankroll": round(cum, 2)})
    return curve


def generate_weekly_report(week_end_date: Optional[str] = None) -> dict:
    """Generate weekly summary. week_end_date is the last day of the week (Sunday)."""
    if week_end_date is None:
        now = datetime.now(timezone.utc)
        # Find most recent Sunday
        days_since_sunday = (now.weekday() + 1) % 7
        if days_since_sunday == 0 and now.hour < 23:
            days_since_sunday = 7
        week_end = now.date() - timedelta(days=days_since_sunday)
        week_end_date = week_end.isoformat()

    reports_dir = REPORTS_DIR
    daily_reports = []
    if reports_dir.exists():
        for f in reports_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if data.get("report_type") == "daily" and data.get("date"):
                    d = data["date"]
                    week_start = (datetime.strptime(week_end_date, "%Y-%m-%d") - timedelta(days=6)).date().isoformat()
                    if week_start <= d <= week_end_date:
                        daily_reports.append(data)
            except Exception:
                pass

    daily_reports.sort(key=lambda r: r.get("date", ""))

    total_pnl = sum(r.get("performance_summary", {}).get("net_pnl_usd", 0) for r in daily_reports)
    best_day = max(daily_reports, key=lambda r: r.get("performance_summary", {}).get("net_pnl_usd", 0)) if daily_reports else None
    worst_day = min(daily_reports, key=lambda r: r.get("performance_summary", {}).get("net_pnl_usd", 0)) if daily_reports else None

    trades = _read_trades()
    session = _read_session_start()
    starting = float(session.get("starting_bankroll", 1000))
    week_start = (datetime.strptime(week_end_date, "%Y-%m-%d") - timedelta(days=6)).date().isoformat()
    bankroll_series = [starting]
    for d in sorted(set((t.get("exit_time") or "")[:10] for t in trades if (t.get("exit_time") or "")[:10])):
        if not d or d < week_start or d > week_end_date:
            continue
        day_pnl = sum(float(t.get("pnl_usdc", 0) or 0) for t in trades if (t.get("exit_time") or "")[:10] == d)
        bankroll_series.append(bankroll_series[-1] + day_pnl)

    # Week-over-week: load previous week's weekly report if exists
    prev_week_end = (datetime.strptime(week_end_date, "%Y-%m-%d") - timedelta(days=7)).date().isoformat()
    prev_report_path = reports_dir / f"weekly-{prev_week_end}.json"
    prev_week_pnl = None
    if prev_report_path.exists():
        try:
            prev_data = json.loads(prev_report_path.read_text(encoding="utf-8"))
            prev_week_pnl = prev_data.get("total_pnl")
        except Exception:
            pass

    # Projected monthly/annual
    days_in_week = 7
    daily_avg = total_pnl / days_in_week if days_in_week else 0
    projected_monthly = daily_avg * 30
    projected_annual = daily_avg * 365

    return {
        "date": week_end_date,
        "report_type": "weekly",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_pnl": round(total_pnl, 2),
        "best_day": best_day.get("date") if best_day else None,
        "best_day_pnl": round(best_day.get("performance_summary", {}).get("net_pnl_usd", 0), 2) if best_day else None,
        "worst_day": worst_day.get("date") if worst_day else None,
        "worst_day_pnl": round(worst_day.get("performance_summary", {}).get("net_pnl_usd", 0), 2) if worst_day else None,
        "bankroll_growth": [round(x, 2) for x in bankroll_series],
        "prev_week_pnl": round(prev_week_pnl, 2) if prev_week_pnl is not None else None,
        "projected_monthly": round(projected_monthly, 2),
        "projected_annual": round(projected_annual, 2),
    }


def _ascii_equity_curve(curve: List[dict], width: int = 40, height: int = 10) -> str:
    """Generate ASCII art equity curve."""
    if not curve or len(curve) < 2:
        return "(no data)"
    values = [p["bankroll"] for p in curve]
    mn, mx = min(values), max(values)
    if mx <= mn:
        mx = mn + 1
    lines = []
    for row in range(height, -1, -1):
        line = ""
        for col in range(width):
            idx = int(col / (width - 1) * (len(values) - 1)) if width > 1 else 0
            val = values[idx]
            norm = (val - mn) / (mx - mn)
            bar_row = int(norm * height)
            if bar_row == row:
                line += "*"
            elif bar_row > row:
                line += "|"
            else:
                line += " "
        lines.append(line)
    return "\n".join(lines)


def _report_to_html(report: dict) -> str:
    """Convert report dict to HTML email body."""
    ps = report.get("performance_summary", {})
    net_pnl = ps.get("net_pnl_usd", 0)
    net_pct = ps.get("net_pnl_pct", 0)
    is_positive = net_pnl >= 0
    color = "#00ff88" if is_positive else "#ff3366"
    sign = "+" if is_positive else ""

    summary_line = f"Day Result: {sign}${net_pnl:.2f} ({sign}{net_pct:.1f}%)"
    curve = report.get("equity_curve", [])
    ascii_curve = _ascii_equity_curve(curve)

    def _fmt_trade(t, key: str, prefix: str = "") -> str:
        if not t:
            return "—"
        m = (t.get("market") or "—")[:50]
        v = t.get(key, 0) or 0
        return f"{m} ({prefix}${v:.2f})"

    rows_perf = [
        ("Starting Bankroll", f"${ps.get('starting_bankroll', 0):.2f}"),
        ("Ending Bankroll", f"${ps.get('ending_bankroll', 0):.2f}"),
        ("Net P&L", f"<span style='color:{color}'>{sign}${net_pnl:.2f} ({sign}{net_pct:.1f}%)</span>"),
        ("Goal Progress", f"{ps.get('goal_progress_pct', 0):.1f}%"),
        ("Total Trades", str(ps.get("total_trades", 0))),
        ("Win Rate", f"{ps.get('win_rate', 0):.1f}%"),
        ("Profit Factor", str(ps.get("profit_factor") or "—")),
        ("Best Trade", _fmt_trade(ps.get("best_trade"), "profit", "+")),
        ("Worst Trade", _fmt_trade(ps.get("worst_trade"), "loss", "")),
    ]

    crypto = report.get("crypto_performance", {})
    strat_rows = ""
    for name, data in crypto.get("by_strategy", {}).items():
        pnl = data.get("pnl", 0)
        c = "#00ff88" if pnl >= 0 else "#ff3366"
        strat_rows += f"<tr><td>{name}</td><td style='color:{c}'>${pnl:.2f}</td><td>{data.get('trades', 0)}</td></tr>"

    risk = report.get("risk_summary", {})
    mc = report.get("market_conditions", {})

    html = f"""
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>PolyMarket Daily Report - {report.get('date', '')}</title></head>
<body style="font-family: 'Segoe UI', Arial, sans-serif; background:#0a0a0f; color:#f0f0f8; padding:24px; margin:0;">
<div style="max-width:600px; margin:0 auto;">
  <h1 style="font-size:24px; border-bottom:1px solid #333; padding-bottom:12px;">PolyMarket Bot — Daily Report</h1>
  <p style="font-size:18px; font-weight:bold; color:{color}; margin:20px 0;">{summary_line}</p>

  <h2 style="font-size:14px; color:#888; text-transform:uppercase; margin-top:24px;">Performance Summary</h2>
  <table style="width:100%; border-collapse:collapse;">
    {chr(10).join(f'<tr><td style="padding:6px 0; color:#888;">{k}</td><td style="text-align:right;">{v}</td></tr>' for k, v in rows_perf)}
  </table>

  <h2 style="font-size:14px; color:#888; text-transform:uppercase; margin-top:24px;">Equity Curve</h2>
  <pre style="background:#111; padding:16px; border-radius:8px; font-family:monospace; font-size:11px; overflow-x:auto;">{ascii_curve}</pre>

  <h2 style="font-size:14px; color:#888; text-transform:uppercase; margin-top:24px;">Crypto by Strategy</h2>
  <table style="width:100%; border-collapse:collapse; border:1px solid #333;">
    <tr style="background:#111;"><th style="text-align:left; padding:10px;">Strategy</th><th>P&L</th><th>Trades</th></tr>
    {strat_rows or '<tr><td colspan="3">No data</td></tr>'}
  </table>
  <p style="margin-top:8px; font-size:12px; color:#888;">Best: {crypto.get('best_strategy') or '—'} | Worst: {crypto.get('worst_strategy') or '—'}</p>
  <p style="font-size:12px; color:#888;">Crypto: {crypto.get('crypto_trades', 0)} trades | NBA: {crypto.get('nba_trades', 0)} trades</p>

  <h2 style="font-size:14px; color:#888; text-transform:uppercase; margin-top:24px;">NBA Performance</h2>
  <p>P&L: ${report.get('nba_performance', {}).get('nba_pnl', 0):.2f} | Win Rate: {report.get('nba_performance', {}).get('nba_win_rate') or '—'}%</p>

  <h2 style="font-size:14px; color:#888; text-transform:uppercase; margin-top:24px;">Risk Summary</h2>
  <p>Loss limit triggered: {risk.get('loss_limit_triggered', False)} | Max drawdown: {risk.get('max_drawdown_pct', 0)}% | Rejected: {risk.get('rejected_trades_count', 0)}</p>

  <h2 style="font-size:14px; color:#888; text-transform:uppercase; margin-top:24px;">Market Conditions</h2>
  <p>BTC: ${mc.get('btc_usd') or '—'} ({mc.get('btc_24h_change_pct') or '—'}%) | ETH: ${mc.get('eth_usd') or '—'} ({mc.get('eth_24h_change_pct') or '—'}%)</p>
  <p>Markets scanned: {mc.get('markets_scanned', 0)} | With edge: {mc.get('markets_with_edge', 0)}</p>

  <p style="margin-top:32px; font-size:11px; color:#666;">Generated {report.get('generated_at', '')}</p>
</div>
</body>
</html>
"""
    return html


def _report_to_discord_embed(report: dict) -> dict:
    """Build Discord webhook embed payload."""
    ps = report.get("performance_summary", {})
    net_pnl = ps.get("net_pnl_usd", 0)
    is_positive = net_pnl >= 0
    color = 0x00FF88 if is_positive else 0xFF3366
    sign = "+" if is_positive else ""

    fields = [
        {"name": "Net P&L", "value": f"{sign}${net_pnl:.2f} ({sign}{ps.get('net_pnl_pct', 0):.1f}%)", "inline": True},
        {"name": "Trades", "value": str(ps.get("total_trades", 0)), "inline": True},
        {"name": "Win Rate", "value": f"{ps.get('win_rate', 0):.1f}%", "inline": True},
        {"name": "Starting", "value": f"${ps.get('starting_bankroll', 0):.2f}", "inline": True},
        {"name": "Ending", "value": f"${ps.get('ending_bankroll', 0):.2f}", "inline": True},
        {"name": "Goal Progress", "value": f"{ps.get('goal_progress_pct', 0):.1f}%", "inline": True},
    ]
    crypto = report.get("crypto_performance", {})
    for name, data in crypto.get("by_strategy", {}).items():
        pnl = data.get("pnl", 0)
        fields.append({"name": name, "value": f"${pnl:.2f} ({data.get('trades', 0)} trades)", "inline": True})

    return {
        "embeds": [{
            "title": f"PolyMarket Daily Report — {report.get('date', '')}",
            "description": f"**Day Result: {sign}${net_pnl:.2f} ({sign}{ps.get('net_pnl_pct', 0):.1f}%)**",
            "color": color,
            "fields": fields,
            "footer": {"text": f"Generated {report.get('generated_at', '')[:19]}"},
        }],
    }


def send_email(report: dict, config: dict) -> bool:
    """Send report via SMTP (Gmail)."""
    to_addr = config.get("report_email_to", "").strip()
    from_addr = config.get("report_email_from", "").strip()
    password = config.get("report_email_password", "").strip()
    if not to_addr or not from_addr or not password:
        logger.debug("Email not configured — skipping send")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"PolyMarket Daily Report — {report.get('date', '')}"
        msg["From"] = from_addr
        msg["To"] = to_addr
        html = _report_to_html(report)
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info(f"Daily report email sent to {to_addr}")
        return True
    except Exception as e:
        logger.error(f"Email send failed: {e}", exc_info=True)
        return False


def send_discord(report: dict, config: dict) -> bool:
    """Send report to Discord webhook."""
    url = config.get("discord_webhook_url", "").strip()
    if not url:
        logger.debug("Discord webhook not configured — skipping")
        return False
    try:
        import urllib.request
        payload = json.dumps(_report_to_discord_embed(report)).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                logger.info("Daily report sent to Discord")
                return True
    except Exception as e:
        logger.error(f"Discord send failed: {e}", exc_info=True)
    return False


def save_report(report: dict, filename: Optional[str] = None) -> Path:
    """Save report to reports/ directory. Prune old reports (keep 90 days)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = report.get("date", datetime.now(timezone.utc).date().isoformat())
    if filename is None:
        filename = f"{date_str}.json"
    path = REPORTS_DIR / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"Report saved to {path}")

    # Prune: delete reports older than 90 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).date()
    for f in REPORTS_DIR.glob("*.json"):
        try:
            if f.stem.count("-") == 2:
                parts = f.stem.split("-")
                if len(parts) == 3:
                    d = datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
                    if d < cutoff:
                        f.unlink()
                        logger.debug(f"Pruned old report {f.name}")
        except Exception:
            pass
    return path


def run_daily_report(send_email_flag: bool = True, send_discord_flag: bool = True) -> dict:
    """
    Generate daily report, save to JSON, optionally send via email and Discord.
    Returns the report dict.
    """
    config = _load_config()
    report = generate_daily_report()
    save_report(report)
    if send_email_flag and config.get("report_email_to"):
        send_email(report, config)
    if send_discord_flag and config.get("discord_webhook_url"):
        send_discord(report, config)
    return report


def run_weekly_report() -> dict:
    """Generate and save weekly report."""
    report = generate_weekly_report()
    save_report(report, filename=f"weekly-{report.get('date', '')}.json")
    return report
