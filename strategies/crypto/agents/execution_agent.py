"""
Execution Agent

Receives approved (opportunity, size_usdc) pairs from the risk agent
and places orders on Kalshi via the authenticated API.

Modes (resolved by core.environment.resolve_environment):
  PAPER      — places real orders against the Kalshi DEMO API.
  LIVE       — places real orders against the Kalshi PRODUCTION API.

The agent never reads EXECUTION_MODE directly — the daemon resolves the
Environment once and passes it in.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.db import connect as db_connect
from core.environment import Environment, resolve_environment
from core.kalshi_client import KalshiClient
from ..core.models import Order, OrderStatus, Side, TradeOpportunity

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parents[3] / "data" / "paper_trades.db"


class ExecutionAgent:
    """
    Places orders (paper or live) and persists trade records.
    """

    def __init__(
        self,
        approved_queue: asyncio.Queue[tuple[TradeOpportunity, float]],
        risk_agent=None,   # typed weakly to avoid circular import
        environment: Optional[Environment] = None,
    ) -> None:
        self._approved = approved_queue
        self._risk_agent = risk_agent
        self._env = environment or resolve_environment()
        self._db = self._init_db()
        self._kalshi: Optional[KalshiClient] = None
        logger.info("ExecutionAgent ready | mode=%s", self._env.label)

    async def run(self) -> None:
        while True:
            opp, size_usdc = await self._approved.get()
            order = await self._execute(opp, size_usdc)
            self._persist(order)
            logger.info(
                "Order %s | %s | %.2f USDC | fill=%.4f",
                order.status.value,
                opp.market.title[:50],
                size_usdc,
                order.fill_price or 0.0,
            )

    async def _execute(
        self, opp: TradeOpportunity, size_usdc: float
    ) -> Order:
        return await self._live_order(opp, size_usdc)

    async def _live_order(self, opp: TradeOpportunity, size_usdc: float) -> Order:
        if self._kalshi is None:
            self._kalshi = KalshiClient(
                api_key=self._env.api_key,
                private_key_path=self._env.private_key_path,
                base_url=self._env.rest_base_url,
            )
            await self._kalshi.open()
            logger.info(
                "ExecutionAgent: KalshiClient opened against %s (mode=%s)",
                self._env.rest_base_url,
                self._env.label,
            )

        now = datetime.now(tz=timezone.utc)

        if opp.side == Side.YES:
            fill_price = opp.market.yes_ask
            yes_price_dollars = max(0.01, min(0.99, fill_price))
        else:
            fill_price = opp.market.no_ask
            no_price_dollars = max(0.01, min(0.99, fill_price))
            yes_price_dollars = 1.0 - no_price_dollars

        count = max(1, int(size_usdc / fill_price))

        try:
            resp = await self._kalshi.place_limit_order(
                ticker=opp.market.ticker,
                side=opp.side.value.lower(),
                count=count,
                yes_price_dollars=yes_price_dollars,
                order_group_id=getattr(self, "_order_group_id", None),
            )
        except Exception as exc:
            logger.error("Live order failed for %s: %s", opp.market.ticker, exc)
            return Order(
                opportunity=opp,
                size_usdc=size_usdc,
                status=OrderStatus.REJECTED,
                fill_price=None,
                placed_at=now,
                error=str(exc),
            )

        order_data = resp.get("order", resp)
        order_id = order_data.get("order_id") or order_data.get("id")

        if not order_id:
            error_msg = str(resp)[:200]
            logger.error("Live order rejected for %s: %s", opp.market.ticker, error_msg)
            return Order(
                opportunity=opp,
                size_usdc=size_usdc,
                status=OrderStatus.REJECTED,
                fill_price=None,
                placed_at=now,
                error=error_msg,
            )

        raw_filled = order_data.get("fill_count") or order_data.get("filled_count") or 0
        filled_count = int(float(raw_filled))
        status = OrderStatus.FILLED if filled_count >= count else OrderStatus.PENDING
        actual_size = filled_count * fill_price if filled_count else size_usdc

        return Order(
            opportunity=opp,
            size_usdc=actual_size,
            status=status,
            fill_price=fill_price,
            placed_at=now,
            filled_at=now if status == OrderStatus.FILLED else None,
            order_id=str(order_id),
        )

    def _persist(self, order: Order) -> None:
        opp = order.opportunity
        # Audit: spot price at the moment the signal fired
        spot_price = opp.signal.features.spot_price if opp.signal else 0.0
        # Audit: latency from signal to order placement
        signal_latency_ms = 0.0
        if opp.signal and opp.signal.timestamp:
            delta = (order.placed_at - opp.signal.timestamp).total_seconds()
            signal_latency_ms = delta * 1000.0
        realized_vol = opp.signal.features.realized_vol_long if opp.signal else 0.0
        kelly_fraction = opp.capped_fraction
        try:
            self._db.execute(
                """
                INSERT INTO trades (
                    order_id, ticker, title, side,
                    model_prob, market_prob, edge,
                    size_usdc, fill_price, status,
                    placed_at, filled_at,
                    spot_price_at_signal, signal_latency_ms,
                    realized_vol, kelly_fraction,
                    environment
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order.order_id,
                    opp.market.ticker,
                    opp.market.title[:200],
                    opp.side.value,
                    opp.model_prob,
                    opp.market_prob,
                    opp.edge,
                    order.size_usdc,
                    order.fill_price,
                    order.status.value,
                    order.placed_at.isoformat(),
                    order.filled_at.isoformat() if order.filled_at else None,
                    spot_price,
                    signal_latency_ms,
                    realized_vol,
                    kelly_fraction,
                    self._env.label,
                ),
            )
            self._db.commit()
        except sqlite3.Error as exc:
            logger.error("DB write error: %s", exc)

    def _init_db(self) -> sqlite3.Connection:
        # Ensure data directory exists and table is initialized
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = db_connect(str(DB_PATH), check_same_thread=False)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                ticker TEXT,
                title TEXT,
                side TEXT,
                model_prob REAL,
                market_prob REAL,
                edge REAL,
                size_usdc REAL,
                fill_price REAL,
                status TEXT,
                placed_at TEXT,
                filled_at TEXT,
                resolved_at TEXT,
                resolution TEXT,
                pnl_usdc REAL,
                spot_price_at_signal REAL,
                signal_latency_ms REAL,
                realized_vol REAL,
                kelly_fraction REAL,
                environment TEXT
            )
        """)
        # Migrate existing DBs: add new audit columns if missing
        existing_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        for col in ("spot_price_at_signal", "signal_latency_ms", "realized_vol", "kelly_fraction"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col} REAL")
        if "environment" not in existing_cols:
            conn.execute("ALTER TABLE trades ADD COLUMN environment TEXT")
        conn.commit()
        return conn
