"""Health check sourced from Kalshi (the source of truth).

Prints account balance, open positions, recent fills, and recent
settlements. The local SQLite ``decisions``/``trades`` table is consulted
only to attach signal-time context (model_prob, edge) to each fill.

Usage:
    python -m research.health_check
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.db import connect as db_connect  # noqa: E402
from core.environment import resolve_environment  # noqa: E402
from core.kalshi_client import KalshiClient  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "paper_trades.db"
STALE_HOURS = 6
RECENT_FILLS_LIMIT = 8


def _age(ts_str: str) -> str:
    try:
        ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        delta = datetime.now(tz=timezone.utc) - ts
        s = int(delta.total_seconds())
        if s < 60:
            return f"{s}s ago"
        if s < 3600:
            return f"{s // 60}m ago"
        return f"{s // 3600}h {(s % 3600) // 60}m ago"
    except Exception:
        return str(ts_str)


def _fill_ts_iso(fill: dict) -> str:
    raw = fill.get("created_time") or fill.get("ts") or ""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
    return str(raw)


def _decision_context(conn: sqlite3.Connection, ticker: str) -> dict:
    """Return latest decision-audit row for a ticker, or empty dict."""
    if not ticker:
        return {}
    try:
        cur = conn.execute(
            "SELECT model_prob, edge, side FROM trades "
            "WHERE ticker = ? ORDER BY id DESC LIMIT 1",
            (ticker,),
        )
        row = cur.fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    return {"model_prob": row[0], "edge": row[1], "side": row[2]}


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
        midnight = datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        fills_today = await client.get_fills(min_ts=int(midnight.timestamp()))
        fills_recent = await client.get_fills(limit=RECENT_FILLS_LIMIT)
        settlements_today = await client.get_settlements(
            min_ts=int(midnight.timestamp())
        )
    finally:
        await client.close()

    return {
        "env": env.label,
        "balance": balance,
        "positions": positions,
        "fills_today": fills_today,
        "fills_recent": fills_recent,
        "settlements_today": settlements_today,
    }


def main() -> int:
    data = asyncio.run(_gather())
    data["positions"] = [
        p for p in data["positions"]
        if int(p.get("yes_count") or 0) != 0 or int(p.get("no_count") or 0) != 0
    ]

    conn: sqlite3.Connection | None = None
    if DB_PATH.exists():
        try:
            conn = db_connect(str(DB_PATH))
        except sqlite3.Error:
            conn = None

    bar = "═" * 50
    print(f"\n{bar}")
    print(f"  KINZIE HEALTH — {data['env']}")
    print(f"  {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  source: Kalshi REST")
    print(bar)

    def _spnl(s: dict) -> float:
        rev = float(s.get("revenue") or 0) / 100.0
        yc = float(s.get("yes_total_cost_dollars") or 0.0)
        nc = float(s.get("no_total_cost_dollars") or 0.0)
        fee = float(s.get("fee_cost") or 0.0)
        return rev - yc - nc - fee

    print(f"\n  Balance (Kalshi)   : ${data['balance']:,.2f}")
    daily_pnl = sum(_spnl(s) for s in data["settlements_today"])
    print(f"  Today realised P&L : ${daily_pnl:+,.2f}  "
          f"({len(data['settlements_today'])} settlements)")
    print(f"  Open positions     : {len(data['positions'])}")
    print(f"  Fills today        : {len(data['fills_today'])}")

    if data["fills_recent"]:
        last_ts = _fill_ts_iso(data["fills_recent"][0])
        if last_ts:
            print(f"  Last fill          : {_age(last_ts)}")
            try:
                last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
                stale = (datetime.now(tz=timezone.utc) - last) > timedelta(hours=STALE_HOURS)
            except Exception:
                stale = False
        else:
            stale = False
    else:
        print("  Last fill          : none")
        stale = False

    if data["positions"]:
        print(f"\n  ── Open positions (Kalshi) ──")
        print(f"  {'TICKER':<32} {'YES':>5} {'NO':>5} {'EXPOSURE':>10}")
        print(f"  {'-' * 32} {'-' * 5} {'-' * 5} {'-' * 10}")
        for p in data["positions"]:
            t = (p.get("ticker") or p.get("market_ticker") or "")[:32]
            yc = int(p.get("yes_count") or 0)
            nc = int(p.get("no_count") or 0)
            exp = float(p.get("market_exposure") or 0) / 100.0
            print(f"  {t:<32} {yc:>5} {nc:>5} ${exp:>9.2f}")

    if data["fills_recent"]:
        print(f"\n  ── Recent fills (Kalshi) ──")
        print(f"  {'TICKER':<32} {'SIDE':<4} {'COUNT':>5} {'PRICE':>6}  AGE")
        print(f"  {'-' * 32} {'-' * 4} {'-' * 5} {'-' * 6}  {'-' * 8}")
        for f in data["fills_recent"]:
            t = (f.get("ticker") or f.get("market_ticker") or "")[:32]
            side = (f.get("side") or "?").upper()[:4]
            count = int(float(f.get("count") or f.get("count_fp") or 0))
            price = float(f.get("yes_price") or f.get("yes_price_dollars") or 0)
            if price >= 1.0:
                price /= 100.0
            ts = _fill_ts_iso(f)
            ctx = _decision_context(conn, t) if conn else {}
            ctx_s = (
                f"  edge={ctx['edge']:.3f}" if ctx.get("edge") is not None else ""
            )
            print(f"  {t:<32} {side:<4} {count:>5} {price:>6.3f}  {_age(ts)}{ctx_s}")

    if conn:
        conn.close()

    print(f"\n{bar}\n")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
