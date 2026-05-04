"""
Risk Agent

Sits between the scanner (opportunity detection) and execution.
Applies position limits, drawdown circuit breakers, and Kelly sizing
before forwarding approved opportunities to the execution agent.

Controls (all configurable):
  MAX_CONCURRENT_POSITIONS  — never hold more than N open positions
  MAX_DAILY_LOSS_PCT        — circuit breaker: halt if daily loss exceeds this % of bankroll
  MAX_SINGLE_EXPOSURE_PCT   — max % of bankroll in any single market
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..core.config import Config, DEFAULT_CONFIG
from core.kelly import position_size
from ..core.models import Side, TradeOpportunity

logger = logging.getLogger(__name__)


class RiskAgent:
    """
    Stateful risk gate. Approves or rejects trade opportunities.
    Tracks open positions and daily P&L.
    """

    def __init__(
        self,
        opportunity_queue: asyncio.Queue[TradeOpportunity],
        approved_queue: asyncio.Queue[tuple[TradeOpportunity, float]],
        bankroll_usdc: float,
        config: Config = DEFAULT_CONFIG,
    ) -> None:
        self._opportunities = opportunity_queue
        self._approved = approved_queue
        self._bankroll = bankroll_usdc
        self._cfg = config

        self._open_positions: dict[str, float] = {}   # ticker -> max-loss exposure (USDC)
        self._positions_by_symbol: dict[str, set[str]] = {}  # symbol -> set of tickers
        self._positions_by_expiry: dict[str, set[str]] = {}  # expiry_key -> set of tickers
        self._daily_pnl: float = 0.0
        self._last_reset_date: date = datetime.now(tz=timezone.utc).date()
        self._last_fill_time: Optional[datetime] = None
        self._halted: bool = False
        # Consecutive-loss circuit breaker: tracks outcomes of last N resolved fills.
        # If the tail of this deque is all losses, trading pauses until streak_halt_until.
        self._recent_outcomes: deque[bool] = deque(maxlen=config.consecutive_loss_pause_fills + 2)
        self._streak_halted: bool = False
        self._streak_halt_until: Optional[datetime] = None

    async def run(self) -> None:
        while True:
            opp = await self._opportunities.get()
            self._maybe_reset_daily()

            result = self._evaluate(opp)
            if result is not None:
                await self._approved.put(result)

    def restore_position(self, ticker: str, size: float) -> None:
        """Re-register an open position from persistent storage after a process restart."""
        self._open_positions[ticker] = size
        symbol = _ticker_to_symbol(ticker)
        self._positions_by_symbol.setdefault(symbol, set()).add(ticker)
        expiry = _expiry_key(ticker)
        self._positions_by_expiry.setdefault(expiry, set()).add(ticker)

    def restore_daily_pnl(self, amount: float) -> None:
        """Restore today's realized P&L from persistent storage after a process restart."""
        self._daily_pnl = amount

    def set_bankroll(self, usdc: float) -> None:
        """Update bankroll from live account balance. No-op if value is unchanged."""
        if usdc <= 0 or abs(usdc - self._bankroll) < 0.01:
            return
        prev = self._bankroll
        self._bankroll = usdc
        logger.info("RiskAgent: bankroll updated to $%.2f (was $%.2f)", usdc, prev)

    def record_fill(self, ticker: str, pnl: float) -> None:
        """Call when a position resolves. Updates daily P&L and removes from open set."""
        self._open_positions.pop(ticker, None)
        symbol = _ticker_to_symbol(ticker)
        if symbol in self._positions_by_symbol:
            self._positions_by_symbol[symbol].discard(ticker)
            if not self._positions_by_symbol[symbol]:
                del self._positions_by_symbol[symbol]
        expiry = _expiry_key(ticker)
        self._positions_by_expiry.get(expiry, set()).discard(ticker)
        self._daily_pnl += pnl

        # Daily loss circuit breaker
        if self._daily_pnl <= -(self._bankroll * self._cfg.max_daily_loss_pct):
            self._halted = True
            logger.warning(
                "CIRCUIT BREAKER: daily loss %.2f USDC exceeds limit. Trading halted.",
                abs(self._daily_pnl),
            )

        # Consecutive-loss circuit breaker: pause 24h if last N fills are all losses.
        # Catches edge decay that isn't yet large enough for the percentage-based halt.
        self._recent_outcomes.append(pnl > 0)
        n = self._cfg.consecutive_loss_pause_fills
        if len(self._recent_outcomes) >= n:
            tail = list(self._recent_outcomes)[-n:]
            if not any(tail):
                self._streak_halted = True
                self._streak_halt_until = datetime.now(tz=timezone.utc) + timedelta(hours=24)
                logger.warning(
                    "STREAK HALT: last %d fills all losses (latest P&L: $%+.2f). "
                    "Pausing until %s UTC.",
                    n, pnl,
                    self._streak_halt_until.strftime("%Y-%m-%d %H:%M"),
                )

    def _evaluate(
        self, opp: TradeOpportunity
    ) -> Optional[tuple[TradeOpportunity, float]]:
        if self._halted:
            logger.debug("Halted — rejecting opportunity on %s", opp.market.ticker)
            return None

        # Consecutive-loss streak halt — auto-clears after 24h
        if self._streak_halted:
            now = datetime.now(tz=timezone.utc)
            if self._streak_halt_until is not None and now >= self._streak_halt_until:
                self._streak_halted = False
                self._streak_halt_until = None
                self._recent_outcomes.clear()
                logger.info("Streak halt cleared. Resuming trading.")
            else:
                logger.debug("Streak-halted — rejecting opportunity on %s", opp.market.ticker)
                return None

        if len(self._open_positions) >= self._cfg.max_concurrent_positions:
            logger.debug("Max concurrent positions reached")
            return None

        if opp.market.spread_pct < self._cfg.min_spread_pct:
            logger.info(
                "RISK REJECT spread_too_tight: %s | spread=%.2f%% < %.0f%% floor | edge=%.3f",
                opp.market.ticker, opp.market.spread_pct * 100,
                self._cfg.min_spread_pct * 100, opp.edge,
            )
            return None

        if opp.market.ticker in self._open_positions:
            logger.debug("Already have position in %s", opp.market.ticker)
            return None

        # Burst protection — don't fill all slots from a single signal burst
        now = datetime.now(tz=timezone.utc)
        if self._last_fill_time is not None:
            seconds_since = (now - self._last_fill_time).total_seconds()
            if seconds_since < self._cfg.min_seconds_between_fills:
                logger.info(
                    "RISK REJECT cooldown: %s | %.0fs < %ds",
                    opp.market.ticker, seconds_since, self._cfg.min_seconds_between_fills,
                )
                return None

        # Signal freshness gate — stale signals aren't edge, they're noise
        signal_age = (now - opp.signal.timestamp).total_seconds()
        if signal_age > self._cfg.max_signal_age_seconds:
            logger.debug(
                "RISK REJECT stale_signal: %s | age=%.1fs > %.0fs",
                opp.market.ticker, signal_age, self._cfg.max_signal_age_seconds,
            )
            return None

        # Per-symbol concentration limit
        symbol = _ticker_to_symbol(opp.market.ticker)
        symbol_positions = self._positions_by_symbol.get(symbol, set())
        if len(symbol_positions) >= self._cfg.max_positions_per_symbol:
            logger.info(
                "RISK REJECT symbol_concentration: %s | %s has %d open",
                opp.market.ticker, symbol, len(symbol_positions),
            )
            return None

        # Per-expiry concentration limit — adjacent range bets on the same expiry hour
        # share the same settlement risk. Take only the highest-edge bracket per hour.
        expiry = _expiry_key(opp.market.ticker)
        if len(self._positions_by_expiry.get(expiry, set())) >= self._cfg.max_positions_per_expiry:
            logger.info(
                "RISK REJECT expiry_concentration: %s | expiry=%s already has %d position(s)",
                opp.market.ticker, expiry,
                len(self._positions_by_expiry.get(expiry, set())),
            )
            return None

        market_price = opp.market.yes_ask if opp.side == Side.YES else opp.market.no_ask

        # Breakeven gate — reject if edge doesn't clear fee + slippage at this price.
        # Fee is parabolic: peaks at P=0.5. Tick/slippage rounding costs dominate at
        # extreme prices. Static min_edge doesn't account for price-dependent dynamics.
        fee_cost = self._cfg.kalshi_taker_fee_rate * market_price * (1.0 - market_price)
        breakeven = fee_cost + self._cfg.estimated_slippage
        if opp.edge < breakeven:
            logger.info(
                "RISK REJECT below_breakeven: %s | edge=%.4f < breakeven=%.4f "
                "(fee=%.4f slip=%.4f) at P=%.3f",
                opp.market.ticker, opp.edge, breakeven,
                fee_cost, self._cfg.estimated_slippage, market_price,
            )
            return None

        # NO fill price band
        if opp.side == Side.NO and market_price < self._cfg.min_no_fill_price:
            logger.info(
                "RISK REJECT no_price_too_low: %s | no_ask=%.3f < %.2f floor",
                opp.market.ticker, market_price, self._cfg.min_no_fill_price,
            )
            return None
        if opp.side == Side.NO and market_price > self._cfg.max_no_fill_price:
            logger.info(
                "RISK REJECT no_price_too_high: %s | no_ask=%.3f > %.2f cap",
                opp.market.ticker, market_price, self._cfg.max_no_fill_price,
            )
            return None

        size = position_size(
            model_prob=opp.model_prob,
            market_price=market_price,
            bankroll_usdc=self._bankroll,
        )

        # Apply hard caps
        max_by_exposure = self._bankroll * self._cfg.max_single_exposure_pct
        size = min(size, max_by_exposure)

        # Scale NO position size proportionally to fill price.
        # At NO price=0.50 → max $5k instead of $10k. This makes
        # dollar-at-risk proportional to the payout ratio.
        if opp.side == Side.NO:
            max_no_size = max_by_exposure * market_price
            size = min(size, max_no_size)

        if size < 1.0:
            logger.info(
                "RISK REJECT size_too_small: %s | size=%.4f USDC | model_prob=%.3f ask=%.3f edge_vs_ask=%.3f",
                opp.market.ticker, size, opp.model_prob, market_price,
                abs(opp.model_prob - market_price),
            )
            return None

        # Proactive exposure gate — reject if total pending worst-case loss across
        # open positions + this trade would push us past the daily-loss circuit
        # breaker, even while _daily_pnl is still unrealized. Max loss per binary
        # position is the full stake (contract can resolve to 0).
        pending_exposure = sum(self._open_positions.values())
        worst_case_daily = self._daily_pnl - pending_exposure - size
        daily_loss_floor = -(self._bankroll * self._cfg.max_daily_loss_pct)
        if worst_case_daily < daily_loss_floor:
            logger.info(
                "RISK REJECT pending_exposure_cap: %s | pending=%.0f size=%.0f daily_pnl=%.0f "
                "worst_case=%.0f floor=%.0f",
                opp.market.ticker, pending_exposure, size, self._daily_pnl,
                worst_case_daily, daily_loss_floor,
            )
            return None

        self._open_positions[opp.market.ticker] = size
        self._last_fill_time = datetime.now(tz=timezone.utc)
        symbol = _ticker_to_symbol(opp.market.ticker)
        self._positions_by_symbol.setdefault(symbol, set()).add(opp.market.ticker)
        expiry = _expiry_key(opp.market.ticker)
        self._positions_by_expiry.setdefault(expiry, set()).add(opp.market.ticker)
        logger.info(
            "Approved: %s | edge=%.3f | size=%.2f USDC | side=%s",
            opp.market.title[:60],
            opp.edge,
            size,
            opp.side.value,
        )
        return (opp, size)

    def _maybe_reset_daily(self) -> None:
        today = datetime.now(tz=timezone.utc).date()
        if today != self._last_reset_date:
            self._daily_pnl = 0.0
            self._halted = False
            self._last_reset_date = today
            logger.info("Daily P&L reset")


def _ticker_to_symbol(ticker: str) -> str:
    """Extract crypto symbol from Kalshi ticker prefix (KXBTC -> BTC, KXETH -> ETH)."""
    upper = ticker.upper()
    # Check 15M series before shorter prefix (KXBTC15M starts with KXBTC)
    if upper.startswith("KXBTC15M"):
        return "BTC"
    if upper.startswith("KXETH15M"):
        return "ETH"
    if upper.startswith("KXBTC"):
        return "BTC"
    if upper.startswith("KXETH"):
        return "ETH"
    if upper.startswith("KXSOL"):
        return "SOL"
    if upper.startswith("KXXRP"):
        return "XRP"
    return ticker.split("-")[0]


def _expiry_key(ticker: str) -> str:
    """Extract expiry segment from Kalshi ticker: 'KXETH-26MAY0213-B2300' → '26MAY0213'."""
    parts = ticker.split("-")
    return parts[1] if len(parts) >= 2 else ticker
