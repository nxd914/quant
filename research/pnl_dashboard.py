"""P&L dashboard sourced from Kalshi.

Cumulative + per-day P&L from Kalshi settlements, current open
positions from Kalshi positions. The local DB is consulted only to
attach signal-time context (model_prob, edge) to settled tickers.

Usage:
    python -m research.pnl_dashboard
"""

from __future__ import annotations

import asyncio
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.db import connect as db_connect  # noqa: E402
from core.environment import resolve_environment  # noqa: E402
from core.kalshi_client import KalshiClient  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "paper_trades.db"


def _settlement_pnl(s: dict) -> float:
    revenue = float(s.get("revenue") or 0.0) / 100.0
    yes_cost = float(s.get("yes_total_cost_dollars") or 0.0)
    no_cost = float(s.get("no_total_cost_dollars") or 0.0)
    fee = float(s.get("fee_cost") or 0.0)
    return revenue - yes_cost - no_cost - fee


def _settlement_day(s: dict) -> str:
    raw = s.get("settled_time") or s.get("settle_time") or ""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc).strftime("%Y-%m-%d")
    # Kalshi returns ISO strings (e.g. "2026-05-02T17:31:24.508834Z")
    return str(raw)[:10]


def _settlement_ticker(s: dict) -> str:
    return s.get("ticker") or s.get("market_ticker") or ""


def _daily_sharpe(settlements: list[dict], bankroll_basis: float) -> float:
    if not settlements or bankroll_basis <= 0:
        return 0.0
    daily_pnl: dict[str, float] = defaultdict(float)
    for s in settlements:
        day = _settlement_day(s)
        if day:
            daily_pnl[day] += _settlement_pnl(s)
    returns = [pnl / bankroll_basis for _, pnl in sorted(daily_pnl.items())]
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(365)


async def _gather() -> dict:
    env = resolve_environment()
    client = KalshiClient(
        api_key=env.api_key,
        private_key_path=env.private_key_path,
        base_url=env.rest_base_url,
    )
    await client.open()
    try:
        balance = await client.get_balance()
        positions = await client.get_positions()
        settlements = await client.get_settlements(limit=200)
    finally:
        await client.close()
    return {
        "env": env.label,
        "balance": balance,
        "positions": positions,
        "settlements": settlements,
    }


def _decision_ctx(conn: sqlite3.Connection | None, ticker: str) -> str:
    if not conn or not ticker:
        return ""
    try:
        row = conn.execute(
            "SELECT model_prob, edge FROM trades WHERE ticker = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    if not row or row[1] is None:
        return ""
    return f"  edge={row[1]:.3f}"


def main() -> None:
    data = asyncio.run(_gather())
    settlements = data["settlements"]
    open_positions = [
        p for p in data["positions"]
        if int(p.get("yes_count") or 0) != 0 or int(p.get("no_count") or 0) != 0
    ]
    data["positions"] = open_positions
    realized = sum(_settlement_pnl(s) for s in settlements)
    wins = sum(1 for s in settlements if _settlement_pnl(s) > 0)
    losses = sum(1 for s in settlements if _settlement_pnl(s) < 0)
    total = wins + losses
    win_rate = (wins / total * 100.0) if total else 0.0
    sharpe = _daily_sharpe(settlements, max(1.0, data["balance"] - realized))

    conn: sqlite3.Connection | None = None
    if DB_PATH.exists():
        try:
            conn = db_connect(str(DB_PATH))
        except sqlite3.Error:
            conn = None

    print("════════════════════════════════════")
    print(f"  KINZIE P&L DASHBOARD — {data['env']}")
    print(f"  source: Kalshi settlements")
    print("════════════════════════════════════")
    print(f"  Cash balance       : ${data['balance']:,.2f}")
    print(f"  Realised P&L (200) : {'+' if realized >= 0 else ''}${realized:,.2f}")
    print(f"  Open positions     : {len(data['positions'])}")
    print(f"  Settled markets    : {len(settlements)}")
    print(f"  Win rate           : {win_rate:.1f}%  ({wins}W / {losses}L)")
    print(f"  Sharpe (daily)     : {sharpe:.2f}")

    if data["positions"]:
        print("\nOPEN POSITIONS")
        print(f"  {'TICKER':<32} {'YES':>5} {'NO':>5} {'EXPOSURE':>10}")
        for p in data["positions"]:
            t = (p.get("ticker") or p.get("market_ticker") or "")[:32]
            yc = int(p.get("yes_count") or 0)
            nc = int(p.get("no_count") or 0)
            exp = float(p.get("market_exposure") or 0) / 100.0
            print(f"  {t:<32} {yc:>5} {nc:>5} ${exp:>9.2f}")

    print("\nRECENT SETTLEMENTS (last 20)")
    print(f"  {'TICKER':<32} {'RES':<5} {'P&L':>8}")
    for s in settlements[:20]:
        t = _settlement_ticker(s)[:32]
        res = (s.get("market_result") or "?")[:5]
        pnl = _settlement_pnl(s)
        ctx = _decision_ctx(conn, t)
        print(f"  {t:<32} {res:<5} {'+' if pnl >= 0 else ''}${pnl:.2f}{ctx}")

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
