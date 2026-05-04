"""
Spot-to-probability mapping for prediction markets.

Converts CEX spot features into an implied probability estimate for
Kalshi binary contracts.

This is a deterministic mapping — no learned model — so the edge claim
is mathematically defensible under review.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .models import FeatureVector, Signal, SignalType

# Thresholds for signal generation
JUMP_RETURN_THRESHOLD = 0.002   # 0.2% return in short window → potential jump
MOMENTUM_Z_THRESHOLD = 2.0      # z-score threshold to fire signal
MIN_CONFIDENCE = 0.55           # minimum confidence to pass signal downstream
BRACKET_CALIBRATION = 0.55      # haircut for bracket prob: N(d2) overestimates narrow brackets due to discrete jumps + CF Benchmark averaging. Lowered from 0.70 after -$31k paper loss analysis (model=0.81 vs market=0.51 on ATM brackets).
MIN_TIME_TO_EXPIRY_HOURS = 1.0 / 60.0  # 1-minute floor; prevents d2 singularity as t→0 (scanner guards at 5min but race condition exists between scan and execution)


def spot_to_implied_prob(
    current_price: float,
    strike: float,
    time_to_expiry_hours: float,
    realized_vol: float,
    drift: float = 0.0,
) -> float:
    """
    Log-normal model probability that price > strike at expiry.

    Uses Black-Scholes N(d2) as a closed-form approximation.
    No risk-free rate (prediction market, not option).

    Args:
        current_price:        current spot price
        strike:               contract resolution threshold
        time_to_expiry_hours: hours until contract resolves
        realized_vol:         annualized realized volatility
        drift:                annualized drift (default 0.0 for backward
                              compatibility). Pass a short-window EWMA of
                              log-returns when |momentum_z| is large.

    Returns:
        Probability in (0, 1)
    """
    if time_to_expiry_hours <= 0 or realized_vol <= 0:
        return 1.0 if current_price > strike else 0.0

    t = max(time_to_expiry_hours, MIN_TIME_TO_EXPIRY_HOURS) / 8760.0  # convert to years; floor prevents d2 singularity
    log_moneyness = math.log(current_price / strike)
    d2 = (log_moneyness + (drift - 0.5 * realized_vol ** 2) * t) / (realized_vol * math.sqrt(t))
    return _standard_normal_cdf(d2)


def up_down_15m_prob(
    spot: float,
    t_hours: float,
    vol: float,
    drift: float = 0.0,
) -> float:
    """
    Probability that price is HIGHER at expiry than current spot.

    Used for Kalshi Up/Down directional markets (KXETH15M, KXBTC15M).
    At drift=0 returns ~0.50 for all horizons — edge only comes from drift
    (momentum signal). Wire drift from FeatureAgent.short_return once N≥50 fills.

    Equivalent to spot_to_implied_prob with strike = spot (ATM).
    """
    if spot <= 0 or t_hours <= 0 or vol <= 0:
        return 0.5
    return spot_to_implied_prob(spot, spot, t_hours, vol, drift=drift)


def bracket_prob(
    current_price: float,
    floor_strike: float,
    cap_strike: float,
    time_to_expiry_hours: float,
    realized_vol: float,
    drift: float = 0.0,
) -> float:
    """
    Log-normal probability that price lands inside [floor, cap] at expiry.

    P(floor < S_T < cap) = N(d2_floor) - N(d2_cap)
    where each term is computed via spot_to_implied_prob.

    Used for Kalshi bracket ("between") contracts where YES resolves
    when spot closes strictly between floor_strike and cap_strike.
    """
    if floor_strike >= cap_strike or current_price <= 0:
        return 0.0
    prob_above_floor = spot_to_implied_prob(
        current_price, floor_strike, time_to_expiry_hours, realized_vol, drift=drift
    )
    prob_above_cap = spot_to_implied_prob(
        current_price, cap_strike, time_to_expiry_hours, realized_vol, drift=drift
    )
    return max(0.0, (prob_above_floor - prob_above_cap) * BRACKET_CALIBRATION)


def _standard_normal_cdf(x: float) -> float:
    """Approximation of N(x) using math.erfc."""
    return 0.5 * math.erfc(-x / math.sqrt(2))


def features_to_signal(features: FeatureVector) -> Signal | None:
    """
    Deterministic decision rule: FeatureVector → Signal or None.

    Fires a signal if:
      - A jump is detected, OR
      - Momentum z-score exceeds threshold

    Confidence is a normalized function of z-score magnitude.
    No learned parameters — all thresholds are domain-reasoned constants.
    """
    if not features.jump_detected and abs(features.momentum_z) < MOMENTUM_Z_THRESHOLD:
        return None

    signal_type = (
        SignalType.MOMENTUM_UP
        if features.short_return > 0
        else SignalType.MOMENTUM_DOWN
    )

    # Confidence scaled by how much z-score exceeds threshold
    z_excess = max(0.0, abs(features.momentum_z) - MOMENTUM_Z_THRESHOLD)
    confidence = min(0.95, MIN_CONFIDENCE + 0.05 * z_excess)

    # Implied probability shift magnitude (rough heuristic, refined in backtest)
    implied_shift = min(0.15, abs(features.short_return) * 50)

    if confidence < MIN_CONFIDENCE:
        return None

    return Signal(
        signal_type=signal_type,
        symbol=features.symbol,
        timestamp=features.timestamp,
        features=features,
        implied_prob_shift=implied_shift,
        confidence=confidence,
    )
