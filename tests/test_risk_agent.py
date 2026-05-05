"""
Tests for quant/agents/risk_agent.py

Covers:
- Spread floor enforcement (MIN_SPREAD_PCT)
- Concurrent position limit (MAX_CONCURRENT_POSITIONS)
- Circuit breaker trigger on daily loss
- record_fill: position slot cleanup and P&L accumulation
"""

import asyncio
import pytest
from datetime import datetime, timedelta, timezone

from strategies.crypto.agents.risk_agent import RiskAgent
from strategies.crypto.core.config import DEFAULT_CONFIG
from strategies.crypto.core.models import (
    FeatureVector,
    KalshiMarket,
    Side,
    Signal,
    SignalType,
    TradeOpportunity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opp(ticker: str, spread: float = DEFAULT_CONFIG.min_spread_pct + 0.01, edge: float = 0.1) -> TradeOpportunity:
    mid = 0.50
    half = spread / 2
    market = KalshiMarket(
        ticker=ticker,
        title="Test market",
        event_ticker="",
        yes_bid=mid - half,
        yes_ask=mid + half,
        no_bid=mid - half,
        no_ask=mid + half,
        implied_prob=mid,
        spread_pct=spread,
        volume_24h=5000,
        liquidity=2000,
        close_time="",
        timestamp=datetime.now(tz=timezone.utc),
    )
    fv = FeatureVector("SYM", datetime.now(tz=timezone.utc), 0.0, 0.0, 0.0, False, 0.0)
    sig = Signal(SignalType.MOMENTUM_UP, "SYM", datetime.now(tz=timezone.utc), fv, 0.1, 0.9, ())
    return TradeOpportunity(
        signal=sig,
        market=market,
        side=Side.YES,
        model_prob=mid + edge,
        market_prob=mid,
        edge=edge,
        kelly_fraction=0.1,
        capped_fraction=0.1,
    )


def _make_agent() -> RiskAgent:
    agent = RiskAgent(asyncio.Queue(), asyncio.Queue(), bankroll_usdc=500.0)
    agent.mark_seeded()
    return agent


# ---------------------------------------------------------------------------
# Spread floor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spread_floor_rejects_tight_spread():
    """Spreads below MIN_SPREAD_PCT must always be rejected (Kalshi fee protection)."""
    agent = _make_agent()
    bad = _make_opp("TIGHT", spread=DEFAULT_CONFIG.min_spread_pct - 0.005)
    assert agent._evaluate(bad) is None


@pytest.mark.asyncio
async def test_spread_floor_approves_sufficient_spread():
    """Spreads at or above DEFAULT_CONFIG.min_spread_pct should pass the gate."""
    agent = _make_agent()
    good = _make_opp("WIDE", spread=DEFAULT_CONFIG.min_spread_pct + 0.01)
    result = agent._evaluate(good)
    assert result is not None
    assert result[0].market.ticker == "WIDE"


# ---------------------------------------------------------------------------
# Concurrent position limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_position_limit():
    """After MAX_CONCURRENT_POSITIONS slots filled, further opps are rejected by the count gate.

    Populate slots directly with minimal exposure so the proactive exposure gate
    isn't what trips — this test isolates the count limit.
    """
    agent = _make_agent()
    for i in range(DEFAULT_CONFIG.max_concurrent_positions):
        agent._open_positions[f"MKT{i}"] = 1.0  # minimal exposure, isolates count behavior

    assert len(agent._open_positions) == DEFAULT_CONFIG.max_concurrent_positions

    extra = _make_opp("EXTRA")
    assert agent._evaluate(extra) is None


@pytest.mark.asyncio
async def test_duplicate_ticker_rejected():
    """Cannot open a second position in the same market."""
    agent = _make_agent()
    opp = _make_opp("DUP")
    assert agent._evaluate(opp) is not None
    assert agent._evaluate(opp) is None  # same ticker, second attempt


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_halts_on_loss():
    """A single loss exceeding bankroll * MAX_DAILY_LOSS_PCT must trigger the halt flag."""
    agent = _make_agent()
    daily_loss_limit = 500.0 * DEFAULT_CONFIG.max_daily_loss_pct  # $100 at $500 bankroll
    agent.record_fill("X", -(daily_loss_limit + 1.0))
    assert agent._halted is True


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_new_trades():
    """Once halted, no new opportunities get approved."""
    agent = _make_agent()
    daily_loss_limit = 500.0 * DEFAULT_CONFIG.max_daily_loss_pct  # $100 at $500 bankroll
    agent.record_fill("X", -(daily_loss_limit + 1.0))

    opp = _make_opp("NEW")
    assert agent._evaluate(opp) is None


@pytest.mark.asyncio
async def test_circuit_breaker_not_triggered_on_profit():
    """Profitable fills should never trigger the circuit breaker."""
    agent = _make_agent()
    agent.record_fill("X", +50.0)
    assert agent._halted is False


# ---------------------------------------------------------------------------
# record_fill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_fill_removes_position():
    """Resolved position must be removed from _open_positions."""
    agent = _make_agent()
    opp = _make_opp("FILL_ME")
    agent._evaluate(opp)

    assert "FILL_ME" in agent._open_positions
    agent.record_fill("FILL_ME", 5.0)
    assert "FILL_ME" not in agent._open_positions


@pytest.mark.asyncio
async def test_record_fill_accumulates_pnl():
    """Multiple fills should accumulate into _daily_pnl."""
    agent = _make_agent()
    agent.record_fill("T1", 10.0)
    agent.record_fill("T2", -3.0)
    assert agent._daily_pnl == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_record_fill_reopens_slot_for_new_trade():
    """After a fill frees a slot, a new trade in that ticker should be approvable."""
    agent = _make_agent()
    # Fill up all slots with minimal exposure (isolates slot-recycling from exposure gate)
    for i in range(DEFAULT_CONFIG.max_concurrent_positions):
        agent._open_positions[f"S{i}"] = 1.0

    agent.record_fill("S0", 0.0)
    assert len(agent._open_positions) == DEFAULT_CONFIG.max_concurrent_positions - 1

    new_opp = _make_opp("SNEW")
    assert agent._evaluate(new_opp) is not None


# ---------------------------------------------------------------------------
# Burst protection (cooldown between fills)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cooldown_rejects_rapid_fills():
    """A second fill within MIN_SECONDS_BETWEEN_FILLS should be rejected."""
    agent = _make_agent()
    assert agent._evaluate(_make_opp("FIRST")) is not None
    # Immediately try another — should be rejected by cooldown
    assert agent._evaluate(_make_opp("SECOND")) is None


@pytest.mark.asyncio
async def test_cooldown_allows_after_interval():
    """After the cooldown expires, a new fill should be approved."""
    agent = _make_agent()
    assert agent._evaluate(_make_opp("FIRST")) is not None
    # Simulate time passing beyond cooldown
    agent._last_fill_time = datetime.now(tz=timezone.utc) - timedelta(
        seconds=DEFAULT_CONFIG.min_seconds_between_fills + 1
    )
    assert agent._evaluate(_make_opp("SECOND")) is not None


# ---------------------------------------------------------------------------
# Breakeven gate
# ---------------------------------------------------------------------------

def _make_opp_at_price(ticker: str, price: float, edge: float) -> TradeOpportunity:
    """Build an opportunity where yes_ask == price and opp.edge == edge."""
    spread = DEFAULT_CONFIG.min_spread_pct + 0.01
    half = spread / 2
    market = KalshiMarket(
        ticker=ticker,
        title="Breakeven test",
        event_ticker="",
        yes_bid=price - half,
        yes_ask=price,
        no_bid=price - half,
        no_ask=price,
        implied_prob=price - half,
        spread_pct=spread,
        volume_24h=5000,
        liquidity=2000,
        close_time="",
        timestamp=datetime.now(tz=timezone.utc),
    )
    fv = FeatureVector("SYM", datetime.now(tz=timezone.utc), 0.0, 0.0, 0.0, False, 0.0)
    sig = Signal(SignalType.MOMENTUM_UP, "SYM", datetime.now(tz=timezone.utc), fv, 0.1, 0.9, ())
    return TradeOpportunity(
        signal=sig,
        market=market,
        side=Side.YES,
        model_prob=price + edge,
        market_prob=price,
        edge=edge,
        kelly_fraction=0.1,
        capped_fraction=0.1,
    )


@pytest.mark.asyncio
async def test_breakeven_gate_rejects_sub_breakeven_edge():
    """Edge below fee+slippage at ATM must be rejected."""
    from strategies.crypto.core.config import Config
    cfg = Config(estimated_slippage=0.005)
    agent = RiskAgent(asyncio.Queue(), asyncio.Queue(), bankroll_usdc=500.0, config=cfg)
    agent.mark_seeded()
    # At P=0.5: fee = 0.07*0.5*0.5 = 0.0175; breakeven = 0.0175 + 0.005 = 0.0225
    # edge=0.015 < 0.0225 → reject
    opp = _make_opp_at_price("BELOW", price=0.50, edge=0.015)
    assert agent._evaluate(opp) is None


@pytest.mark.asyncio
async def test_breakeven_gate_approves_sufficient_edge():
    """Edge clearly above fee+slippage must pass the breakeven gate."""
    from strategies.crypto.core.config import Config
    cfg = Config(estimated_slippage=0.005)
    agent = RiskAgent(asyncio.Queue(), asyncio.Queue(), bankroll_usdc=500.0, config=cfg)
    agent.mark_seeded()
    # At P=0.5: breakeven = 0.0225; edge=0.10 passes
    opp = _make_opp_at_price("ABOVE", price=0.50, edge=0.10)
    assert agent._evaluate(opp) is not None


@pytest.mark.asyncio
async def test_breakeven_gate_lower_at_extreme_price():
    """Fee is smaller at extreme prices (P=0.1), so breakeven is lower than at ATM.

    edge=0.02 is below the ATM breakeven (0.0225) but above the P=0.1 breakeven
    (0.0113), demonstrating the gate is price-aware and not a static floor.
    """
    from strategies.crypto.core.config import Config
    cfg = Config(estimated_slippage=0.005)
    agent = RiskAgent(asyncio.Queue(), asyncio.Queue(), bankroll_usdc=500.0, config=cfg)
    agent.mark_seeded()
    # At P=0.1: fee = 0.07*0.1*0.9 = 0.0063; breakeven = 0.0063+0.005 = 0.0113
    # edge=0.02 > 0.0113 → passes breakeven; also clears MIN_KELLY at this price
    opp = _make_opp_at_price("EXTREME", price=0.10, edge=0.02)
    assert agent._evaluate(opp) is not None


# ---------------------------------------------------------------------------
# Per-symbol concentration limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_symbol_concentration_limit():
    """Cannot exceed MAX_POSITIONS_PER_SYMBOL open positions per crypto symbol."""
    agent = _make_agent()
    # Pre-load ETH positions at the symbol cap with minimal exposure (isolates symbol gate)
    for i in range(DEFAULT_CONFIG.max_positions_per_symbol):
        ticker = f"KXETH-SERIES-{i}"
        agent._open_positions[ticker] = 1.0
        agent._positions_by_symbol.setdefault("ETH", set()).add(ticker)

    # Next ETH position should be rejected by symbol gate
    extra = _make_opp("KXETH-EXTRA")
    assert agent._evaluate(extra) is None

    # A BTC position should still be allowed
    btc = _make_opp("KXBTC-SERIES-0")
    assert agent._evaluate(btc) is not None


# ---------------------------------------------------------------------------
# Signal freshness gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_signal_rejected():
    """Signals older than MAX_SIGNAL_AGE_SECONDS should be rejected."""
    agent = _make_agent()
    # Create opportunity with a signal timestamped 10 seconds ago
    opp = _make_opp("KXBTC-FRESH")
    old_timestamp = datetime.now(tz=timezone.utc) - timedelta(seconds=DEFAULT_CONFIG.max_signal_age_seconds + 5)
    stale_signal = Signal(
        signal_type=opp.signal.signal_type,
        symbol=opp.signal.symbol,
        timestamp=old_timestamp,
        features=opp.signal.features,
        implied_prob_shift=opp.signal.implied_prob_shift,
        confidence=opp.signal.confidence,
    )
    stale_opp = TradeOpportunity(
        signal=stale_signal,
        market=opp.market,
        side=opp.side,
        model_prob=opp.model_prob,
        market_prob=opp.market_prob,
        edge=opp.edge,
        kelly_fraction=opp.kelly_fraction,
        capped_fraction=opp.capped_fraction,
    )
    assert agent._evaluate(stale_opp) is None


@pytest.mark.asyncio
async def test_fresh_signal_accepted():
    """Signals within MAX_SIGNAL_AGE_SECONDS should pass the freshness gate."""
    agent = _make_agent()
    # Default _make_opp uses datetime.now() — should be fresh enough
    opp = _make_opp("KXBTC-FRESH")
    assert agent._evaluate(opp) is not None


# ---------------------------------------------------------------------------
# Proactive exposure circuit breaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proactive_exposure_cap_rejects_before_settlement():
    """New trade rejected when pending worst-case exposure + new trade breaches the daily-loss gate.

    Prevents all 5 slots from filling with worst-case exposure that would breach
    MAX_DAILY_LOSS_PCT * bankroll before any position settles.
    """
    agent = _make_agent()  # bankroll=$500, daily-loss floor = -$100
    # Pre-load two open positions with combined exposure of $80
    agent._open_positions["EXISTING_1"] = 40.0
    agent._open_positions["EXISTING_2"] = 40.0
    assert agent._daily_pnl == 0.0

    # New opportunity produces size ≈ $50 (MAX_SINGLE_EXPOSURE_PCT cap on $500 bankroll).
    # Worst case = 0 - 80 - 50 = -130 < -100 floor → must reject.
    opp = _make_opp("NEW")
    assert agent._evaluate(opp) is None
    assert "NEW" not in agent._open_positions


@pytest.mark.asyncio
async def test_proactive_exposure_cap_allows_when_room_remains():
    """Trade approved when open exposure + new trade stays inside the daily-loss gate."""
    agent = _make_agent()  # bankroll=$500, floor=-$100
    agent._open_positions["EXISTING"] = 10.0

    # Worst case = 0 - 10 - 50 = -60 > -100 → approve.
    opp = _make_opp("NEW")
    result = agent._evaluate(opp)
    assert result is not None
    assert "NEW" in agent._open_positions
    assert agent._open_positions["NEW"] > 0  # exposure tracked


@pytest.mark.asyncio
async def test_record_fill_releases_pending_exposure():
    """After a fill resolves, its exposure is removed from the pending pool and a new trade fits."""
    agent = _make_agent()  # bankroll=$500, floor=-$100
    # Two positions at $45 each = $90 pending → a new $50 trade would worst-case to -$140, reject.
    agent._open_positions["A"] = 45.0
    agent._open_positions["B"] = 45.0
    blocked = _make_opp("BLOCKED")
    assert agent._evaluate(blocked) is None

    # Resolve one position flat; pending now $45. New trade worst-case -$95 > -$100 → approve.
    agent._last_fill_time = None  # reset cooldown (this test is about exposure accounting)
    agent.record_fill("A", 0.0)
    assert "A" not in agent._open_positions
    assert sum(agent._open_positions.values()) == pytest.approx(45.0)

    opp = _make_opp("OK")
    assert agent._evaluate(opp) is not None


# ---------------------------------------------------------------------------
# NO price band (floor + cap)
# ---------------------------------------------------------------------------

def _make_no_opp(ticker: str, no_ask: float) -> TradeOpportunity:
    """Build a NO-side opportunity with a specific no_ask price."""
    spread = DEFAULT_CONFIG.min_spread_pct + 0.01
    mid = 0.50
    half = spread / 2
    market = KalshiMarket(
        ticker=ticker,
        title="Test NO market",
        event_ticker="",
        yes_bid=mid - half,
        yes_ask=mid + half,
        no_bid=no_ask - 0.01,
        no_ask=no_ask,
        implied_prob=mid,
        spread_pct=spread,
        volume_24h=5000,
        liquidity=2000,
        close_time="",
        timestamp=datetime.now(tz=timezone.utc),
    )
    fv = FeatureVector("SYM", datetime.now(tz=timezone.utc), 0.0, 0.0, 0.0, False, 0.0)
    sig = Signal(SignalType.MOMENTUM_DOWN, "SYM", datetime.now(tz=timezone.utc), fv, 0.1, 0.9, ())
    return TradeOpportunity(
        signal=sig,
        market=market,
        side=Side.NO,
        model_prob=0.9,
        market_prob=mid,
        edge=0.4,
        kelly_fraction=0.1,
        capped_fraction=0.1,
    )


@pytest.mark.asyncio
async def test_no_price_floor_rejects_cheap_no():
    """NO bets below MIN_NO_FILL_PRICE are rejected (bad payout ratio)."""
    agent = _make_agent()
    opp = _make_no_opp("KXBTC-CHEAP", no_ask=DEFAULT_CONFIG.min_no_fill_price - 0.01)
    assert agent._evaluate(opp) is None


@pytest.mark.asyncio
async def test_no_price_cap_rejects_expensive_no():
    """NO bets above MAX_NO_FILL_PRICE are rejected (inverted risk/reward — risking $7k+ to win $3k or less)."""
    agent = _make_agent()
    opp = _make_no_opp("KXBTC-PRICEY", no_ask=DEFAULT_CONFIG.max_no_fill_price + 0.01)
    assert agent._evaluate(opp) is None


@pytest.mark.asyncio
async def test_no_price_within_band_passes():
    """NO bets inside [min_no_fill_price, max_no_fill_price] should pass the price gate."""
    agent = _make_agent()
    mid_price = (DEFAULT_CONFIG.min_no_fill_price + DEFAULT_CONFIG.max_no_fill_price) / 2
    opp = _make_no_opp("KXBTC-OK", no_ask=mid_price)
    assert agent._evaluate(opp) is not None


# ---------------------------------------------------------------------------
# Per-expiry concentration limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expiry_concentration_rejects_second_same_expiry():
    """Second position with the same expiry key must be rejected (default limit=1)."""
    agent = _make_agent()
    # Two tickers with same expiry segment "26MAY0213" but different brackets
    opp1 = _make_opp("KXETH-26MAY0213-B2300")
    opp2 = _make_opp("KXETH-26MAY0213-B2320")
    assert agent._evaluate(opp1) is not None
    # Cooldown would also block opp2, so advance fill time to isolate the expiry gate
    agent._last_fill_time = None
    assert agent._evaluate(opp2) is None


@pytest.mark.asyncio
async def test_expiry_concentration_allows_different_expiry():
    """Positions at different expiry hours should both be approved."""
    agent = _make_agent()
    opp1 = _make_opp("KXETH-26MAY0213-B2300")
    opp2 = _make_opp("KXETH-26MAY0214-B2300")
    assert agent._evaluate(opp1) is not None
    agent._last_fill_time = None
    assert agent._evaluate(opp2) is not None


@pytest.mark.asyncio
async def test_expiry_concentration_slot_freed_after_resolve():
    """After a position resolves, the expiry slot opens for a new bracket bet."""
    agent = _make_agent()
    opp1 = _make_opp("KXETH-26MAY0213-B2300")
    assert agent._evaluate(opp1) is not None
    agent.record_fill("KXETH-26MAY0213-B2300", 10.0)
    agent._last_fill_time = None
    opp2 = _make_opp("KXETH-26MAY0213-B2320")
    assert agent._evaluate(opp2) is not None


# ---------------------------------------------------------------------------
# Startup-seed gate (closes the cross-restart per-expiry duplication race)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_seed_approvals_are_rejected():
    """Until PortfolioAgent calls mark_seeded(), no opportunity may be approved.

    Closes the cross-restart race where the scanner approved a second bracket
    on an expiry the bot already held a position on, because the in-memory
    _positions_by_expiry hadn't been re-seeded from Kalshi yet.
    """
    agent = RiskAgent(asyncio.Queue(), asyncio.Queue(), bankroll_usdc=500.0)
    opp = _make_opp("KXETH-26MAY0213-B2300")
    assert agent._evaluate(opp) is None
    agent.mark_seeded()
    assert agent._evaluate(opp) is not None
