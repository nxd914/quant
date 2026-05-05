"""Tests for PortfolioAgent micro-fill threshold and reconcile cadence."""

from __future__ import annotations

import asyncio
import pytest

from strategies.crypto.agents.portfolio_agent import PortfolioAgent
from strategies.crypto.agents.risk_agent import RiskAgent
from strategies.crypto.core.config import Config


class _FakeWS:
    def __init__(self, fills: list[dict]) -> None:
        self.fill_events: asyncio.Queue = asyncio.Queue()
        for f in fills:
            self.fill_events.put_nowait(f)


class _FakeClient:
    """Stub KalshiClient — only the methods PortfolioAgent._poll_cycle touches."""

    def __init__(self, positions: list[dict] | None = None) -> None:
        self._positions = positions or []
        self.calls = {"get_positions": 0, "get_settlements": 0}

    async def get_balance(self) -> float:
        return 1000.0

    async def get_positions(self) -> list[dict]:
        self.calls["get_positions"] += 1
        return self._positions

    async def get_settlements(self, **kwargs) -> list[dict]:
        self.calls["get_settlements"] += 1
        return []


def _make_risk(seeded: bool = True) -> RiskAgent:
    risk = RiskAgent(asyncio.Queue(), asyncio.Queue(), bankroll_usdc=500.0)
    if seeded:
        risk.mark_seeded()
    return risk


def _ws_fill(ticker: str, count: int, yes_price_cents: int, side: str = "yes") -> dict:
    # count_fp is fixed-point (contracts * 100) per Kalshi WS payloads.
    return {
        "market_ticker": ticker,
        "count_fp": count * 100,
        "yes_price": yes_price_cents,
        "side": side,
    }


@pytest.mark.asyncio
async def test_micro_fill_skipped_does_not_lock_expiry_slot():
    """Sub-dollar WS fills must not reserve a per-expiry slot.

    Reproduces the 2026-05-05 incident where a 1-2¢ partial fill on
    KXETH15M-26MAY050645-45 locked the slot for ~3 minutes until the
    next reconcile cycle, blocking dozens of real trade attempts.
    """
    risk = _make_risk()
    ws = _FakeWS([_ws_fill("KXETH15M-26MAY050645-45", count=1, yes_price_cents=1)])
    client = _FakeClient(positions=[])
    cfg = Config(min_fill_register_usd=1.0)
    agent = PortfolioAgent(risk, ws, client, config=cfg)  # type: ignore[arg-type]

    await agent._poll_cycle()

    # Slot must NOT be reserved — cost was 1¢, below the $1 floor.
    assert "KXETH15M-26MAY050645-45" not in risk._open_positions
    assert risk._positions_by_expiry.get("26MAY050645", set()) == set()


@pytest.mark.asyncio
async def test_real_fill_above_threshold_is_registered():
    """Fills at or above the threshold register normally.

    Kalshi must also confirm the position via get_positions(), otherwise
    the now-tight reconcile sweep would drop it as a phantom.
    """
    risk = _make_risk()
    # 100 contracts × $0.50 = $50 fill — well above the $1 floor.
    ws = _FakeWS([_ws_fill("KXETH-26MAY0508-B2380", count=100, yes_price_cents=50)])
    client = _FakeClient(positions=[
        {"ticker": "KXETH-26MAY0508-B2380", "yes_count": 100, "no_count": 0,
         "market_exposure": 5000},  # cents
    ])
    cfg = Config(min_fill_register_usd=1.0)
    agent = PortfolioAgent(risk, ws, client, config=cfg)  # type: ignore[arg-type]

    await agent._poll_cycle()

    assert "KXETH-26MAY0508-B2380" in risk._open_positions
    assert "KXETH-26MAY0508-B2380" in risk._positions_by_expiry.get("26MAY0508", set())


@pytest.mark.asyncio
async def test_reconcile_runs_every_cycle():
    """Defence-in-depth: reconcile cadence dropped from 5 cycles to 1.

    Previously RECONCILE_EVERY_N_CYCLES=5 meant phantoms could persist
    up to 5 minutes. With cadence=1 every 60s poll triggers a sweep.
    """
    from strategies.crypto.agents.portfolio_agent import RECONCILE_EVERY_N_CYCLES
    assert RECONCILE_EVERY_N_CYCLES == 1
