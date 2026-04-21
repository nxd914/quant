"""
Audit-trail replay backtester.

Reads the SQLite paper_trades.db audit trail (which already captures
spot_price_at_signal, realized_vol, kelly_fraction, resolution, pnl_usdc)
and recomputes what the model would have predicted for each fill.
Compares model predictions to actual outcomes.

Does NOT replay tick-by-tick. Pure deterministic replay from the audit log.

Usage:
    python3 -m research.replay_backtest
    python3 -m research.replay_backtest --db path/to/paper_trades.db
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DB_DEFAULT = Path(__file__).resolve().parents[1] / "data" / "paper_trades.db"
MIN_FILLS_FOR_SHARPE = 20


@dataclass(frozen=True)
class FillRecord:
    order_id: str
    ticker: str
    side: str
    model_prob: float
    market_prob: float
    edge: float
    size_usdc: float
    kelly_fraction: float
    realized_vol: float
    resolution: Optional[str]
    pnl_usdc: Optional[float]


def _load_fills(db_path: Path) -> list[FillRecord]:
    if not db_path.exists():
        print(f"[replay_backtest] No database at {db_path}. Run the daemon in paper mode first.")
        sys.exit(0)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT order_id, ticker, side, model_prob, market_prob, edge,
                   size_usdc, kelly_fraction, realized_vol, resolution, pnl_usdc
            FROM trades
            WHERE UPPER(status) IN ('FILLED', 'RESOLVED')
            ORDER BY placed_at ASC
            """
        ).fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[replay_backtest] DB query failed: {exc}")
        conn.close()
        sys.exit(1)
    finally:
        conn.close()

    return [
        FillRecord(
            order_id=r["order_id"],
            ticker=r["ticker"],
            side=r["side"],
            model_prob=r["model_prob"] or 0.0,
            market_prob=r["market_prob"] or 0.0,
            edge=r["edge"] or 0.0,
            size_usdc=r["size_usdc"] or 0.0,
            kelly_fraction=r["kelly_fraction"] or 0.0,
            realized_vol=r["realized_vol"] or 0.0,
            resolution=r["resolution"],
            pnl_usdc=r["pnl_usdc"],
        )
        for r in rows
    ]


def _sharpe(returns: list[float], fills_per_day: int = 4) -> Optional[float]:
    if len(returns) < MIN_FILLS_FOR_SHARPE:
        return None
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / max(n - 1, 1)
    std = math.sqrt(variance)
    if std == 0:
        return None
    annualized_factor = math.sqrt(fills_per_day * 252)
    return (mean / std) * annualized_factor


def _calibration_buckets(fills: list[FillRecord]) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[tuple[float, bool]]] = {
        "0.50–0.60": [], "0.60–0.70": [], "0.70–0.80": [], "0.80–0.90": [], "0.90–1.00": [],
    }
    for f in fills:
        if f.resolution not in ("YES", "NO"):
            continue
        won = (f.side == "yes" and f.resolution == "YES") or (f.side == "no" and f.resolution == "NO")
        p = f.model_prob
        if 0.50 <= p < 0.60:
            buckets["0.50–0.60"].append((p, won))
        elif 0.60 <= p < 0.70:
            buckets["0.60–0.70"].append((p, won))
        elif 0.70 <= p < 0.80:
            buckets["0.70–0.80"].append((p, won))
        elif 0.80 <= p < 0.90:
            buckets["0.80–0.90"].append((p, won))
        elif 0.90 <= p <= 1.00:
            buckets["0.90–1.00"].append((p, won))

    result: dict[str, dict[str, float]] = {}
    for label, entries in buckets.items():
        if not entries:
            result[label] = {"n": 0, "mean_model": 0.0, "realized_win_rate": 0.0}
            continue
        mean_model = sum(e[0] for e in entries) / len(entries)
        realized = sum(1 for _, w in entries if w) / len(entries)
        result[label] = {"n": len(entries), "mean_model": mean_model, "realized_win_rate": realized}
    return result


def run(db_path: Path) -> None:
    fills = _load_fills(db_path)
    total = len(fills)
    resolved = [f for f in fills if f.resolution in ("YES", "NO") and f.pnl_usdc is not None]

    print(f"\n{'═' * 60}")
    print("  Kinzie — Audit Trail Replay Backtest")
    print(f"{'═' * 60}")
    print(f"  Database : {db_path}")
    print(f"  Total fills (filled + resolved): {total}")
    print(f"  Resolved fills with P&L:         {len(resolved)}")

    if not resolved:
        print("\n  No resolved fills yet. Run the daemon in paper mode.")
        print(f"{'═' * 60}\n")
        return

    pnls = [f.pnl_usdc for f in resolved]  # type: ignore[misc]
    wins = [f for f in resolved if (f.pnl_usdc or 0) > 0]
    losses = [f for f in resolved if (f.pnl_usdc or 0) <= 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(resolved)
    mean_edge = sum(f.edge for f in resolved) / len(resolved)
    mean_model_prob = sum(f.model_prob for f in resolved) / len(resolved)

    print(f"\n  {'Metric':<30} {'Value':>12}")
    print(f"  {'─' * 44}")
    print(f"  {'Resolved fills':<30} {len(resolved):>12}")
    print(f"  {'Win rate':<30} {win_rate:>11.1%}")
    print(f"  {'Total P&L (USDC)':<30} {total_pnl:>+12.2f}")
    print(f"  {'Mean P&L per fill (USDC)':<30} {total_pnl / len(resolved):>+12.2f}")
    print(f"  {'Mean model probability':<30} {mean_model_prob:>11.1%}")
    print(f"  {'Mean edge at entry':<30} {mean_edge:>11.1%}")
    print(f"  {'Largest win (USDC)':<30} {max(pnls):>+12.2f}")
    print(f"  {'Largest loss (USDC)':<30} {min(pnls):>+12.2f}")

    sharpe = _sharpe(pnls)
    if sharpe is not None:
        print(f"  {'Annualized Sharpe (est.)':<30} {sharpe:>12.2f}")
    else:
        print(f"  {'Sharpe':<30} {'< ' + str(MIN_FILLS_FOR_SHARPE) + ' fills — pending':>12}")

    print(f"\n  Calibration (model prob vs. realized win rate):")
    print(f"  {'Bucket':<14} {'N':>5} {'Model %':>10} {'Realized %':>12} {'Delta':>8}")
    print(f"  {'─' * 52}")
    calibration = _calibration_buckets(resolved)
    for bucket, stats in calibration.items():
        n = stats["n"]
        if n == 0:
            print(f"  {bucket:<14} {'—':>5}")
            continue
        model_pct = stats["mean_model"] * 100
        realized_pct = stats["realized_win_rate"] * 100
        delta = realized_pct - model_pct
        print(f"  {bucket:<14} {n:>5} {model_pct:>9.1f}% {realized_pct:>11.1f}% {delta:>+7.1f}%")

    print(f"\n{'═' * 60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit-trail replay backtest")
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help="Path to paper_trades.db")
    args = parser.parse_args()
    run(args.db)


if __name__ == "__main__":
    main()
