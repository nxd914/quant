"""
Multi-strategy P&L dashboard.

Reads the four per-strategy trade tables in `data/paper_trades.db` and prints a
single per-strategy summary plus a portfolio total. Crypto numbers are also
broken out by `environment` column (local_sim / paper / live) so historical
sim-fill rows don't get conflated with demo-book or production rows.

Tables:
  - trades          (crypto)         status FILLED/RESOLVED, has `environment`
  - econ_trades     (econ)           open = resolved_at IS NULL
  - sports_trades   (sports)         open = resolved_at IS NULL
  - weather_trades  (weather)        open = resolved_at IS NULL
"""

import sqlite3
from core.db import connect as db_connect
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "paper_trades.db"

STRATEGIES = [
    ("crypto", "trades", True),
    ("econ", "econ_trades", False),
    ("sports", "sports_trades", False),
    ("weather", "weather_trades", False),
]


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _summary(rows: list[sqlite3.Row], has_status: bool) -> dict:
    if has_status:
        open_rows = [r for r in rows if r["status"] == "FILLED"]
        resolved = [r for r in rows if r["status"] == "RESOLVED"]
    else:
        open_rows = [r for r in rows if not r["resolved_at"]]
        resolved = [r for r in rows if r["resolved_at"]]

    realized = sum((r["pnl_usdc"] or 0.0) for r in resolved)
    wins = sum(1 for r in resolved if (r["pnl_usdc"] or 0.0) > 0)
    win_rate = (wins / len(resolved) * 100.0) if resolved else 0.0
    return {
        "total": len(rows),
        "open": len(open_rows),
        "resolved": len(resolved),
        "wins": wins,
        "realized": realized,
        "win_rate": win_rate,
    }


def _print_row(label: str, s: dict) -> None:
    pnl_str = f"{'+' if s['realized'] >= 0 else ''}${s['realized']:,.2f}"
    print(
        f"  {label:<22} {s['total']:>6} {s['open']:>6} {s['resolved']:>9} "
        f"{s['win_rate']:>7.1f}% {pnl_str:>12}"
    )


def main() -> None:
    if not DB_PATH.exists():
        print(f"Error: Database not found at {DB_PATH}")
        return

    conn = db_connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("════════════════════════════════════════════════════════════════")
    print("  KINZIE MULTI-STRATEGY P&L DASHBOARD")
    print("════════════════════════════════════════════════════════════════")
    print(f"  {'STRATEGY':<22} {'TRADES':>6} {'OPEN':>6} {'RESOLVED':>9} "
          f"{'WIN%':>8} {'REALIZED':>12}")
    print("  " + "-" * 62)

    portfolio_pnl = 0.0
    portfolio_total = 0
    portfolio_resolved = 0
    portfolio_wins = 0

    for name, table, has_status in STRATEGIES:
        if not _table_exists(conn, table):
            print(f"  {name:<22} {'(table missing)':>40}")
            continue
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        col_names = {d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description}
        has_env = "environment" in col_names
        if has_status and has_env:
            envs = sorted({(r["environment"] or "unknown") for r in rows})
            for env in envs:
                env_rows = [r for r in rows if (r["environment"] or "unknown") == env]
                s = _summary(env_rows, has_status=True)
                _print_row(f"{name} [{env}]", s)
                if env.lower() in ("paper", "live"):
                    portfolio_pnl += s["realized"]
                    portfolio_total += s["total"]
                    portfolio_resolved += s["resolved"]
                    portfolio_wins += s["wins"]
        elif has_status:
            s = _summary(rows, has_status=True)
            _print_row(f"{name} [legacy]", s)
            portfolio_pnl += s["realized"]
            portfolio_total += s["total"]
            portfolio_resolved += s["resolved"]
            portfolio_wins += s["wins"]
        else:
            s = _summary(rows, has_status=False)
            _print_row(name, s)
            portfolio_pnl += s["realized"]
            portfolio_total += s["total"]
            portfolio_resolved += s["resolved"]
            portfolio_wins += s["wins"]

    print("  " + "-" * 62)
    win_rate = (portfolio_wins / portfolio_resolved * 100.0) if portfolio_resolved else 0.0
    pnl_str = f"{'+' if portfolio_pnl >= 0 else ''}${portfolio_pnl:,.2f}"
    print(
        f"  {'PORTFOLIO (excl. sim)':<22} {portfolio_total:>6} {'-':>6} "
        f"{portfolio_resolved:>9} {win_rate:>7.1f}% {pnl_str:>12}"
    )
    print()
    print("  Note: 'PORTFOLIO' excludes crypto local_sim rows.")


if __name__ == "__main__":
    main()
