"""Live ROI dashboard — sourced from Kalshi.

Pulls balance, positions, fills, and settlements directly from Kalshi.
The local SQLite ``trades`` table is consulted only for signal-time
context (model_prob, edge) attached to each Kalshi fill.

Usage:
    python -m research.live_roi
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.db import connect as db_connect  # noqa: E402
from core.environment import resolve_environment  # noqa: E402
from core.kalshi_client import KalshiClient  # noqa: E402

DB_PATH = REPO_ROOT / "data" / "paper_trades.db"


def _pretty_age(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


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
        settlements_today = await client.get_settlements(
            min_ts=int(midnight.timestamp())
        )
        settlements_all = await client.get_settlements(limit=200)
    finally:
        await client.close()

    return {
        "env": env.label,
        "balance": balance,
        "positions": positions,
        "settlements_today": settlements_today,
        "settlements_all": settlements_all,
    }


def _settlement_pnl(s: dict) -> float:
    revenue = float(s.get("revenue") or 0.0) / 100.0
    yes_cost = float(s.get("yes_total_cost_dollars") or 0.0)
    no_cost = float(s.get("no_total_cost_dollars") or 0.0)
    fee = float(s.get("fee_cost") or 0.0)
    return revenue - yes_cost - no_cost - fee


def main() -> int:
    data = asyncio.run(_gather())

    data["positions"] = [
        p for p in data["positions"]
        if int(p.get("yes_count") or 0) != 0 or int(p.get("no_count") or 0) != 0
    ]
    bankroll = data["balance"]
    print(f"\n=== Crypto live ROI ({data['env']}) — source: Kalshi ===")
    print(f"Cash balance: ${bankroll:,.2f}\n")

    settlements_all = data["settlements_all"]
    pnl_total = sum(_settlement_pnl(s) for s in settlements_all)
    pnl_today = sum(_settlement_pnl(s) for s in data["settlements_today"])

    wins = sum(1 for s in settlements_all if _settlement_pnl(s) > 0)
    losses = sum(1 for s in settlements_all if _settlement_pnl(s) < 0)
    breakevens = len(settlements_all) - wins - losses
    total_resolved = wins + losses
    win_rate = (wins / total_resolved) if total_resolved else 0.0

    # ROI denominator: today's bankroll less today's realised P&L
    # ≈ cash basis at the start of the day. Avoids the "$100k basis" lie.
    bankroll_basis = max(1.0, bankroll - pnl_today)
    today_roi = pnl_today / bankroll_basis if bankroll_basis else 0.0

    print(f"Wins:        {wins}")
    print(f"Losses:      {losses}")
    if breakevens:
        print(f"Breakeven:   {breakevens}")
    print(f"Open:        {len(data['positions'])}")
    print(f"Win rate:    {win_rate:.1%}  ({wins}/{total_resolved})")
    print(f"Cumulative settled P&L (last 200): ${pnl_total:+,.2f}")
    print(f"Today P&L:   ${pnl_today:+,.2f}  ({today_roi:+.3%} vs ${bankroll_basis:,.0f})")

    if data["positions"]:
        print(f"\nOpen positions ({len(data['positions'])}):")
        print(f"  {'ticker':38}  {'YES':>4}  {'NO':>4}  {'exposure':>10}")
        print(f"  {'-' * 38}  {'-' * 4}  {'-' * 4}  {'-' * 10}")
        for p in data["positions"]:
            t = (p.get("ticker") or p.get("market_ticker") or "")[:38]
            yc = int(p.get("yes_count") or 0)
            nc = int(p.get("no_count") or 0)
            exp = float(p.get("market_exposure") or 0) / 100.0
            print(f"  {t:38}  {yc:>4}  {nc:>4}  ${exp:>9.2f}")
    else:
        print("\nNo open positions.")

    # Decision-audit join: latency from local DB
    if DB_PATH.exists():
        try:
            conn = db_connect(str(DB_PATH))
            row = conn.execute(
                "SELECT AVG(signal_latency_ms), COUNT(*) FROM trades "
                "WHERE signal_latency_ms IS NOT NULL AND signal_latency_ms > 0"
            ).fetchone()
            conn.close()
            if row and row[1]:
                print(f"\nMean signal→order latency (decisions log): {row[0]:.0f}ms over {row[1]} rows")
        except sqlite3.Error:
            pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
