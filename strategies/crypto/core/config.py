"""
Canonical configuration for all tunable parameters.

Single source of truth for every threshold, limit, and constant in the
trading system. Agents import from here rather than defining magic numbers
inline. Pass a Config instance to override defaults without touching code.

All values match the empirically-calibrated production defaults.
To run with a custom config: Config(min_edge=0.06, max_concurrent_positions=3)
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # ── Kelly sizing ────────────────────────────────────────────────────────
    kelly_fraction_cap: float = 0.25
    # Conservative half-Kelly analog. Accounts for estimation error in model_prob.
    # Full Kelly requires perfect probability estimates; 0.25× is standard for
    # research-grade systems where edge is unverified at scale.

    min_edge: float = 0.035
    # 3.5% minimum edge before considering a trade.
    # Kalshi taker fee peaks at 1.75% (P=0.5). With bid-ask spread,
    # effective cost is ~3-4% on liquid markets. Floor ensures positive EV.
    # Lowered from 0.04 after paper confirms 14.73 Sharpe; unblocks final live-gate fills.

    min_kelly: float = 0.01
    # Minimum Kelly fraction. Below 1% of bankroll, transaction costs dominate.

    kalshi_taker_fee_rate: float = 0.07
    # Per-contract fee from Kalshi fee schedule (PDF).
    # Formula: fee = 0.07 × P × (1-P) per contract (parabolic, max at P=0.5).

    estimated_slippage: float = 0.005
    # Per-side slippage estimate (price units). 0.5¢ = half-tick avg rounding cost.
    # Used in RiskAgent's breakeven gate: edge must exceed fee + slippage or the
    # trade is rejected before sizing. Recalibrate from demo fills once N >= 50.

    # ── Risk limits ─────────────────────────────────────────────────────────
    max_concurrent_positions: int = 5
    # Portfolio-level concurrency. At $5k bankroll with 10% max per position,
    # 5 positions = 50% max deployed capital. Leaves margin for adverse moves.

    max_daily_loss_pct: float = 0.20
    # Circuit breaker: halt trading if daily realized P&L drops below -20% of bankroll.
    # Proactive gate also blocks new positions if pending worst-case loss would breach this.

    max_single_exposure_pct: float = 0.10
    # Max fraction of bankroll in any single position. 10% per position.

    min_spread_pct: float = 0.04
    # Minimum full bid-ask spread (as fraction of mid). Markets tighter than 4%
    # have insufficient edge after fees — Kalshi doesn't offer maker rebates.

    min_seconds_between_fills: int = 30
    # Burst protection. Prevents a single momentum signal from filling all 5 slots
    # in rapid succession before the market reprices.

    max_positions_per_symbol: int = 2
    # Per-symbol concentration limit (BTC / ETH separately).
    # Prevents correlated loss if one asset makes a large adverse move.

    max_positions_per_expiry: int = 1
    # Max positions sharing the same expiry hour. Adjacent-range NO bets on the same
    # expiry are correlated: if price lands in either bracket, one must lose. Default
    # of 1 ensures we take only the highest-edge bracket per hour.

    max_signal_age_seconds: float = 30.0
    # Freshness gate. The scanner has a 5s burst cooldown before evaluating a
    # signal, so 2s was unreachable. 30s keeps signals current (Kalshi prices
    # reprice on the order of minutes, not seconds) while discarding truly stale
    # signals from queue backup. Periodic-scan opportunities use synthetic
    # signals timestamped at evaluation time and always pass this gate.

    min_no_fill_price: float = 0.40
    # NO-side fill floor. At NO=0.39, you risk $0.39 to win $0.61 (1.56:1).
    # Below 0.40 the risk/reward deteriorates relative to YES-side alternatives.

    max_no_fill_price: float = 0.95
    # NO-side fill cap. At NO=0.95, you risk $0.95 to win $0.05 (19:1 against).
    # Requires 95%+ win rate to break even after fees. Catastrophic if wrong.

    # ── Consecutive-loss circuit breaker ────────────────────────────────────
    consecutive_loss_pause_fills: int = 3
    # If the last N consecutive fills are all losses, pause for 24 hours.
    # Distinct from the daily loss gate: catches streak-based edge decay
    # that isn't yet large enough to trigger the percentage-based halt.

    # ── Scanner ─────────────────────────────────────────────────────────────
    scan_interval_seconds: int = 120
    # Periodic re-pricing cadence. Primarily a rate-limit safety valve —
    # the signal-triggered path fires reactively (within 5s of a momentum event).

    scan_startup_delay_seconds: int = 15
    # Brief warmup before first scan so feature windows accumulate enough ticks.

    scan_concurrency: int = 8
    # Semaphore cap for parallel market evaluation.

    scan_limit: int = 50
    # Max markets to evaluate per periodic scan cycle.

    signal_candidate_limit: int = 120
    # Wider market fetch for signal-triggered scans (more candidates to match).

    min_time_to_close_minutes: int = 5
    # Skip markets with less than 5 minutes to settlement. Too little time for
    # meaningful convergence and order processing.

    max_hours_to_close: int = 4
    # Skip markets closing more than 4 hours out. Far-dated contracts can stay
    # mispriced indefinitely — latency arb requires near-expiry convergence pressure.

    signal_cooldown_seconds: int = 5
    # Rate limit on signal-triggered scan loop. Prevents CPU saturation from
    # high-frequency momentum events on volatile days.

    min_crypto_vol: float = 0.30
    # Vol floor for BTC/ETH. In low-vol regimes, the BS model becomes unreliable.
    # 30% annualized ≈ 0.016% per minute, consistent with historical BTC/ETH.

    max_bracket_yes_price: float = 0.30
    # Don't buy YES on bracket contracts above 30¢. At 30¢+, you're paying 30%+
    # for a narrow range bet — risk/reward inverts relative to outright YES bets.

    min_bracket_distance_pct: float = 0.005
    # Skip brackets where spot is within 0.5% of the bracket midpoint.
    # ATM brackets have non-linear sensitivity that the log-normal model mishandles.

    trading_start_hour_utc: int = 0
    trading_end_hour_utc: int = 24
    # Active trading window (UTC). Crypto markets trade 24/7 and Kalshi
    # publishes 15-minute Up/Down + hourly bracket contracts continuously,
    # so we scan around the clock. (Held over from a multi-strategy era when
    # the bot was idle outside US equity hours.)

    idle_scan_interval_seconds: int = 600
    # Scan cadence outside trading hours (10 min). Keeps the market cache warm
    # without burning unnecessary API quota overnight.

    # ── Feature computation ─────────────────────────────────────────────────
    short_return_window_seconds: float = 5.0
    # 5-second lookback for short return and jump detection.

    vol_window_seconds: float = 60.0
    # 60-second window for realized vol (signal detection and momentum z-score).

    vol_window_long_seconds: float = 900.0
    # 15-minute window for pricing vol. Longer lookback gives more stable vol
    # estimates for 1-4 hour contracts (avoids noise from brief vol spikes).

    min_ticks_for_features: int = 10
    # Minimum observations before emitting features. Prevents cold-start trading
    # with statistically meaningless vol estimates.

    jump_return_threshold: float = 0.002
    # 0.2% return in short window triggers jump detection.

    # ── Pricing ─────────────────────────────────────────────────────────────
    bracket_calibration: float = 0.55
    # Multiplicative haircut applied to bracket_prob output.
    # See docs/CALIBRATION.md for derivation. Short version: the log-normal model
    # overestimates narrow bracket probabilities by ~45% empirically. Tuned from
    # a single paper loss event (model=0.81, market=0.51 on ATM bracket).
    # Needs 50+ fills to validate statistically.

    min_time_to_expiry_hours: float = 1.0 / 60.0
    # 1-minute floor on time-to-expiry in BS formula. Prevents d2 singularity as
    # t→0. Scanner guards at 5min, but this floor catches the race condition between
    # scan and execution timing.

    # ── Signal detection ────────────────────────────────────────────────────
    momentum_z_threshold: float = 2.0
    # Z-score threshold to fire a momentum signal (2 sigma).

    min_confidence: float = 0.55
    # Minimum confidence to propagate a signal downstream.

    # ── Performance metrics ─────────────────────────────────────────────────
    assumed_fills_per_day: int = 4
    # Conservative baseline for Sharpe annualization in _running_sharpe().
    # One fill every ~6 hours during active trading hours. Update from live
    # data once fill cadence stabilizes — the Sharpe estimate is sensitive to
    # this assumption at low sample counts.

    # ── Execution ───────────────────────────────────────────────────────────
    execution_fill_grace_seconds: float = 30.0
    # Kalshi V2 limit orders fill asynchronously. After POST we poll
    # get_order(order_id) up to this many seconds; cancel if still unfilled.
    # Required: a synchronous fill_count==0 check would cancel every order
    # before it has a chance to match. 30s gives thin demo books a chance
    # for crossing flow to land — 8s was too short on Up/Down 15m markets.

    execution_fill_poll_interval_seconds: float = 1.0

    execution_cross_offset_max: float = 0.10
    execution_cross_offset_min: float = 0.01
    # Cross-the-spread bounds applied in ExecutionAgent. The limit price is
    # base_ask + clamp(edge*0.5, min, max). Raising max from 5¢→10¢ unblocks
    # thin 15m Up/Down books where 5¢ above the cached ask still didn't cross.

    min_fill_register_usd: float = 1.0
    # Minimum WS-fill cost (USD) to register as a real position with RiskAgent.
    # Sub-dollar partial fills routinely never settle as a real Kalshi position
    # (Kalshi's matching engine occasionally reports 1-2¢ partial-fills that
    # are immediately reversed). Registering them locks the per-expiry slot for
    # up to one reconcile cycle, blocking dozens of real trade attempts.
    # PortfolioAgent's reconcile loop still picks up real positions from
    # get_positions() within ~60s, so the worst-case effect of this filter is
    # a brief delay in registering a genuine micro-fill.

    # ── Live mode gate ──────────────────────────────────────────────────────
    min_fills_for_live: int = 100
    # Minimum resolved paper fills before live mode is permitted.

    min_sharpe_for_live: float = 1.0
    # Minimum rolling Sharpe ratio (over all fills, n >= min_fills_for_live)
    # before live mode is permitted.

    @classmethod
    def from_env(cls) -> Config:
        """
        Construct Config with environment variable overrides.
        All env vars are optional — missing vars use dataclass defaults.

        Example:
            KELLY_FRACTION_CAP=0.15 MIN_EDGE=0.05 python3 daemon.py
        """
        def _float(key: str, default: float) -> float:
            v = os.environ.get(key)
            return float(v) if v is not None else default

        def _int(key: str, default: int) -> int:
            v = os.environ.get(key)
            return int(v) if v is not None else default

        base = cls()
        return cls(
            kelly_fraction_cap=_float("KELLY_FRACTION_CAP", base.kelly_fraction_cap),
            min_edge=_float("MIN_EDGE", base.min_edge),
            min_kelly=_float("MIN_KELLY", base.min_kelly),
            kalshi_taker_fee_rate=_float("KALSHI_TAKER_FEE_RATE", base.kalshi_taker_fee_rate),
            estimated_slippage=_float("ESTIMATED_SLIPPAGE", base.estimated_slippage),
            max_concurrent_positions=_int("MAX_CONCURRENT_POSITIONS", base.max_concurrent_positions),
            max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", base.max_daily_loss_pct),
            max_single_exposure_pct=_float("MAX_SINGLE_EXPOSURE_PCT", base.max_single_exposure_pct),
            min_spread_pct=_float("MIN_SPREAD_PCT", base.min_spread_pct),
            min_seconds_between_fills=_int("MIN_SECONDS_BETWEEN_FILLS", base.min_seconds_between_fills),
            max_positions_per_symbol=_int("MAX_POSITIONS_PER_SYMBOL", base.max_positions_per_symbol),
            max_positions_per_expiry=_int("MAX_POSITIONS_PER_EXPIRY", base.max_positions_per_expiry),
            max_signal_age_seconds=_float("MAX_SIGNAL_AGE_SECONDS", base.max_signal_age_seconds),
            min_no_fill_price=_float("MIN_NO_FILL_PRICE", base.min_no_fill_price),
            max_no_fill_price=_float("MAX_NO_FILL_PRICE", base.max_no_fill_price),
            consecutive_loss_pause_fills=_int("CONSECUTIVE_LOSS_PAUSE_FILLS", base.consecutive_loss_pause_fills),
            scan_interval_seconds=_int("SCAN_INTERVAL_SECONDS", base.scan_interval_seconds),
            assumed_fills_per_day=_int("ASSUMED_FILLS_PER_DAY", base.assumed_fills_per_day),
            min_fills_for_live=_int("MIN_FILLS_FOR_LIVE", base.min_fills_for_live),
            min_sharpe_for_live=_float("MIN_SHARPE_FOR_LIVE", base.min_sharpe_for_live),
            execution_fill_grace_seconds=_float(
                "EXECUTION_FILL_GRACE_SECONDS", base.execution_fill_grace_seconds,
            ),
            execution_fill_poll_interval_seconds=_float(
                "EXECUTION_FILL_POLL_INTERVAL_SECONDS",
                base.execution_fill_poll_interval_seconds,
            ),
            execution_cross_offset_max=_float(
                "EXECUTION_CROSS_OFFSET_MAX", base.execution_cross_offset_max,
            ),
            execution_cross_offset_min=_float(
                "EXECUTION_CROSS_OFFSET_MIN", base.execution_cross_offset_min,
            ),
            min_fill_register_usd=_float(
                "MIN_FILL_REGISTER_USD", base.min_fill_register_usd,
            ),
        )

    def validate(self) -> None:
        """Assert parameter invariants. Call at startup."""
        assert 0 < self.kelly_fraction_cap <= 1.0, "Kelly cap must be in (0, 1]"
        assert 0 < self.min_edge < 0.50, "Min edge must be in (0, 0.5)"
        assert 0 < self.max_daily_loss_pct <= 1.0, "Daily loss pct must be in (0, 1]"
        assert 0 < self.max_single_exposure_pct <= 1.0, "Exposure pct must be in (0, 1]"
        assert self.max_concurrent_positions >= 1
        assert self.min_no_fill_price < self.max_no_fill_price
        assert self.min_fills_for_live >= 1
        assert self.min_sharpe_for_live > 0


# Default singleton — agents use this unless a custom Config is injected.
DEFAULT_CONFIG = Config()
