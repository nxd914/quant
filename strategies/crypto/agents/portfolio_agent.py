"""
Portfolio Agent

Owns the daemon's view of account state by treating Kalshi as the
authoritative source of truth. Replaces ResolutionAgent: there is no
local "open positions" ledger, no resolution polling against market
status, and no timeout-as-loss heuristic.

Responsibilities
  - On startup: seed RiskAgent.bankroll from get_balance(),
    RiskAgent open-position set from get_positions(), and today's
    realised P&L from get_settlements().
  - Run loop:
      1. Drain WebsocketAgent.fill_events — register newly-filled
         tickers with RiskAgent.restore_position so the slot is held.
      2. Poll get_settlements(): for every settlement we haven't
         processed, call RiskAgent.record_fill(ticker, pnl) — this
         frees the slot, updates daily P&L, and feeds the streak
         circuit-breaker.
      3. Periodic full reconciliation against get_positions(): drop any
         in-memory positions Kalshi no longer reports.

The local SQLite ``trades`` table is still written by ExecutionAgent as
a decision audit log (signal context at order time, keyed by
``order_id``). This agent never reads from it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from core.kalshi_client import KalshiClient
from ..core.config import Config, DEFAULT_CONFIG
from .risk_agent import RiskAgent
from .websocket_agent import WebsocketAgent

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
# Reconcile against Kalshi get_positions() every cycle (60s). Was 5 cycles
# (5 min); a tighter cadence bounds any phantom-slot window — e.g. when a
# WS fill event registers a position that Kalshi never actually opened.
RECONCILE_EVERY_N_CYCLES = 1


class PortfolioAgent:
    def __init__(
        self,
        risk_agent: RiskAgent,
        websocket_agent: WebsocketAgent,
        kalshi_client: KalshiClient,
        poll_interval: int = POLL_INTERVAL_SECONDS,
        config: Config = DEFAULT_CONFIG,
    ) -> None:
        self._risk = risk_agent
        self._ws = websocket_agent
        self._client = kalshi_client
        self._poll_interval = poll_interval
        self._cfg = config
        self._known_tickers: set[str] = set()
        self._seen_settlement_keys: set[tuple[str, str]] = set()
        self._cycles_since_reconcile = 0

    async def run(self) -> None:
        await self._sync_initial()
        logger.info(
            "PortfolioAgent: started, polling every %ds (Kalshi-authoritative)",
            self._poll_interval,
        )
        while True:
            try:
                await self._poll_cycle()
            except Exception as exc:
                logger.warning("PortfolioAgent cycle error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    async def _sync_initial(self) -> None:
        try:
            balance = await self._client.get_balance()
            self._risk.set_bankroll(balance)
            logger.info("PortfolioAgent: bankroll synced from Kalshi = $%.2f", balance)
        except Exception as exc:
            logger.warning("PortfolioAgent: get_balance() failed: %s", exc)

        try:
            positions = await self._client.get_positions()
        except Exception as exc:
            logger.warning("PortfolioAgent: get_positions() failed: %s", exc)
            positions = []

        held = 0
        for p in positions:
            ticker = p.get("ticker") or p.get("market_ticker") or ""
            if not ticker:
                continue
            # Kalshi /portfolio/positions returns historical rows even after
            # full close (yes_count=0, no_count=0) — they still carry
            # non-zero `total_traded`. Only restore positions that are
            # actually held right now.
            if int(p.get("yes_count") or 0) == 0 and int(p.get("no_count") or 0) == 0:
                continue
            cost = _position_cost_usd(p)
            if cost <= 0:
                continue
            self._risk.restore_position(ticker, cost)
            self._known_tickers.add(ticker)
            held += 1
        if held:
            logger.info("PortfolioAgent: synced %d open positions from Kalshi", held)

        # Mark every historic settlement Kalshi knows about as "seen" before
        # the run loop starts. This prevents the first poll cycle from
        # re-processing them as fresh fills (which would falsely fire the
        # streak halt and re-add their P&L to the daily counter).
        try:
            historic = await self._client.get_settlements(limit=200)
        except Exception as exc:
            logger.warning("PortfolioAgent: get_settlements() failed at startup: %s", exc)
            historic = []
        for s in historic:
            self._seen_settlement_keys.add(_settlement_key(s))

        # Rehydrate today's realised P&L only — circuit breaker scope.
        midnight_ts = int(datetime.now(tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp())
        daily_pnl = 0.0
        for s in historic:
            ts_int = _settled_time_unix(s)
            if ts_int is None or ts_int < midnight_ts:
                continue
            daily_pnl += _settlement_pnl(s)
        if daily_pnl != 0.0:
            self._risk.restore_daily_pnl(daily_pnl)
            logger.info(
                "PortfolioAgent: rehydrated daily P&L = $%+.2f", daily_pnl,
            )

        # Open the gate — RiskAgent now knows our true open-position set, so
        # an opportunity on a held expiry will hit the per-expiry cap instead
        # of being approved against an empty in-memory state.
        self._risk.mark_seeded()

    async def _poll_cycle(self) -> None:
        # 1. Drain WS fill events — register any new filled tickers.
        drained = 0
        while True:
            try:
                fill = self._ws.fill_events.get_nowait()
            except asyncio.QueueEmpty:
                break
            drained += 1
            ticker = fill.get("market_ticker") or fill.get("ticker") or ""
            if not ticker or ticker in self._known_tickers:
                continue
            cost = _ws_fill_cost(fill)
            if cost <= 0:
                continue
            if cost < self._cfg.min_fill_register_usd:
                # Sub-dollar partial fills are usually never confirmed by
                # get_positions(); reserving a per-expiry slot for them
                # locks out real trades for up to one reconcile cycle.
                # Reconcile picks up real positions from Kalshi within ~60s.
                logger.info(
                    "PortfolioAgent: WS fill skipped (micro) %s | cost=$%.2f < $%.2f floor",
                    ticker, cost, self._cfg.min_fill_register_usd,
                )
                continue
            self._risk.restore_position(ticker, cost)
            self._known_tickers.add(ticker)
            logger.info(
                "PortfolioAgent: WS fill registered %s | cost=$%.2f", ticker, cost,
            )
        if drained:
            logger.debug("PortfolioAgent: drained %d WS fill events", drained)

        # 2. Process new settlements.
        try:
            settlements = await self._client.get_settlements()
        except Exception as exc:
            logger.warning("PortfolioAgent: get_settlements() failed: %s", exc)
            settlements = []
        for s in settlements:
            key = _settlement_key(s)
            if key in self._seen_settlement_keys:
                continue
            self._seen_settlement_keys.add(key)
            ticker = s.get("ticker") or s.get("market_ticker") or ""
            if not ticker:
                continue
            pnl = _settlement_pnl(s)
            self._risk.record_fill(ticker, pnl)
            self._known_tickers.discard(ticker)
            logger.info(
                "PortfolioAgent: Kalshi settlement %s | result=%s | P&L $%+.2f",
                ticker, s.get("market_result", "?"), pnl,
            )

        # 3. Periodic full reconciliation against Kalshi's view of positions.
        self._cycles_since_reconcile += 1
        if self._cycles_since_reconcile >= RECONCILE_EVERY_N_CYCLES:
            self._cycles_since_reconcile = 0
            await self._reconcile_positions()

    async def _reconcile_positions(self) -> None:
        try:
            positions = await self._client.get_positions()
        except Exception as exc:
            logger.warning("PortfolioAgent: reconcile get_positions() failed: %s", exc)
            return
        kalshi_tickers = {
            (p.get("ticker") or p.get("market_ticker") or "")
            for p in positions
            if (p.get("ticker") or p.get("market_ticker"))
            and (int(p.get("yes_count") or 0) != 0
                 or int(p.get("no_count") or 0) != 0)
        }
        # Drop any locally-tracked ticker Kalshi no longer reports.
        ghosts = self._known_tickers - kalshi_tickers
        for ticker in ghosts:
            logger.info(
                "PortfolioAgent: reconcile dropped phantom %s (no longer in Kalshi positions)",
                ticker,
            )
            # Use record_fill with pnl=0 to clear the slot without faking P&L.
            self._risk.record_fill(ticker, 0.0)
            self._known_tickers.discard(ticker)
        # Add any Kalshi position we somehow missed (e.g. WS event dropped).
        new = kalshi_tickers - self._known_tickers
        for ticker in new:
            p = next(
                (x for x in positions if (x.get("ticker") or x.get("market_ticker")) == ticker),
                None,
            )
            if p is None:
                continue
            cost = _position_cost_usd(p)
            if cost <= 0:
                continue
            self._risk.restore_position(ticker, cost)
            self._known_tickers.add(ticker)
            logger.info("PortfolioAgent: reconcile picked up %s | cost=$%.2f", ticker, cost)


def _position_cost_usd(position: dict) -> float:
    """Estimate USD cost basis of a Kalshi position row.

    Kalshi returns ``market_exposure`` and ``total_traded`` in cents.
    Prefer ``market_exposure`` (current at-risk capital); fall back to
    absolute traded value if exposure is zero.
    """
    for field in ("market_exposure", "total_traded"):
        v = position.get(field)
        if v is None:
            continue
        try:
            cents = abs(float(v))
            if cents > 0:
                return cents / 100.0
        except (TypeError, ValueError):
            pass
    return 0.0


def _ws_fill_cost(fill: dict) -> float:
    """Compute USD cost basis from a WS fill event payload."""
    try:
        count = float(fill.get("count_fp") or fill.get("count") or 0)
        # count_fp is fixed-point (count * 100)
        if count >= 1.0 and "count_fp" in fill:
            count = count / 100.0
        price = float(fill.get("yes_price_dollars") or 0.0)
        if price <= 0:
            cents = float(fill.get("yes_price") or 0.0)
            price = cents / 100.0 if cents > 0 else 0.0
        side = (fill.get("side") or "").lower()
        if side in ("no", "ask"):
            price = max(0.0, 1.0 - price)
        return max(0.0, count * price)
    except (TypeError, ValueError):
        return 0.0


def _settlement_key(settlement: dict) -> tuple[str, str]:
    return (
        settlement.get("ticker") or settlement.get("market_ticker") or "",
        str(settlement.get("settled_time") or settlement.get("settle_time") or ""),
    )


def _settled_time_unix(settlement: dict) -> Optional[int]:
    """Parse Kalshi's ISO settled_time into a unix epoch int, or None."""
    raw = settlement.get("settled_time") or settlement.get("settle_time")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    try:
        return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


def _settlement_pnl(settlement: dict) -> float:
    """Net P&L for a settled market in USD.

    Kalshi's settlement row fields:
      - ``revenue``: total payout in cents (0 on a losing bet)
      - ``yes_total_cost_dollars`` / ``no_total_cost_dollars``: cost basis
        (one is always "0.000000"; the other is what we paid in)
      - ``fee_cost``: settlement fee in dollars

    P&L = revenue/100 - cost_basis - fee_cost.
    """
    try:
        revenue = float(settlement.get("revenue") or 0.0) / 100.0
        yes_cost = float(settlement.get("yes_total_cost_dollars") or 0.0)
        no_cost = float(settlement.get("no_total_cost_dollars") or 0.0)
        fee = float(settlement.get("fee_cost") or 0.0)
        return revenue - yes_cost - no_cost - fee
    except (TypeError, ValueError):
        return 0.0
