"""Tests for deterministic pricing, signal generation, and fee-adjusted Kelly."""

import math
import pytest
from datetime import datetime, timezone

from core.kelly import (
    KALSHI_TAKER_FEE_RATE,
    kalshi_taker_fee_per_contract,
    compute_kelly,
    capped_kelly,
)
from strategies.crypto.core.models import FeatureVector
from strategies.crypto.core.pricing import BRACKET_CALIBRATION, bracket_prob, features_to_signal, spot_to_implied_prob, up_down_15m_prob


def test_spot_to_implied_prob_deep_in_the_money():
    prob = spot_to_implied_prob(
        current_price=80000.0,
        strike=70000.0,
        time_to_expiry_hours=24.0,
        realized_vol=0.5,
    )
    assert prob > 0.5


def test_spot_to_implied_prob_deep_out_of_the_money():
    prob = spot_to_implied_prob(
        current_price=60000.0,
        strike=70000.0,
        time_to_expiry_hours=1.0,
        realized_vol=0.3,
    )
    assert prob < 0.5


def test_spot_to_implied_prob_at_expiry():
    above = spot_to_implied_prob(80000.0, 70000.0, 0.0, 0.5)
    below = spot_to_implied_prob(60000.0, 70000.0, 0.0, 0.5)
    assert above == 1.0
    assert below == 0.0


def test_spot_to_implied_prob_zero_vol():
    above = spot_to_implied_prob(80000.0, 70000.0, 24.0, 0.0)
    below = spot_to_implied_prob(60000.0, 70000.0, 24.0, 0.0)
    assert above == 1.0
    assert below == 0.0


def test_spot_to_implied_prob_drift_zero_is_backward_compatible():
    """drift=0.0 (default) must produce the same result as omitting drift."""
    args = (80000.0, 70000.0, 24.0, 0.5)
    assert spot_to_implied_prob(*args) == spot_to_implied_prob(*args, drift=0.0)


def test_spot_to_implied_prob_positive_drift_raises_otm_yes_prob():
    """Positive drift (price expected to rise) should raise P(spot > strike) for OTM strike."""
    base = spot_to_implied_prob(60000.0, 70000.0, 24.0, 0.5, drift=0.0)
    drifted = spot_to_implied_prob(60000.0, 70000.0, 24.0, 0.5, drift=2.0)
    assert drifted > base


def test_bracket_prob_drift_zero_is_backward_compatible():
    args = (75000.0, 74500.0, 75500.0, 1.0, 0.80)
    assert bracket_prob(*args) == bracket_prob(*args, drift=0.0)


def test_bracket_prob_spot_inside_range():
    # Spot at 75000, bracket [74500, 75500], 1 hour, 80% vol
    # $1000-wide range centered on spot; high prob since range > 1-hour σ
    prob = bracket_prob(75000.0, 74500.0, 75500.0, 1.0, 0.80)
    assert 0.05 < prob < 0.80  # meaningful probability inside range


def test_bracket_prob_spot_far_outside_range():
    # Spot at 80000, bracket [74500, 75500] — spot far above range
    prob = bracket_prob(80000.0, 74500.0, 75500.0, 1.0, 0.30)
    assert prob < 0.05  # very unlikely to fall back into range


def test_bracket_prob_applies_calibration_discount():
    """bracket_prob should apply BRACKET_CALIBRATION haircut to raw N(d2) difference."""
    # Compute raw prob manually: P(above floor) - P(above cap)
    raw_above_floor = spot_to_implied_prob(75000.0, 74500.0, 1.0, 0.80)
    raw_above_cap = spot_to_implied_prob(75000.0, 75500.0, 1.0, 0.80)
    raw_diff = raw_above_floor - raw_above_cap

    calibrated = bracket_prob(75000.0, 74500.0, 75500.0, 1.0, 0.80)
    assert calibrated == pytest.approx(raw_diff * BRACKET_CALIBRATION, rel=1e-6)
    assert calibrated < raw_diff  # calibration always reduces


def test_bracket_prob_invalid_inputs():
    # floor >= cap → 0
    assert bracket_prob(75000.0, 75500.0, 74500.0, 1.0, 0.5) == 0.0
    # zero price → 0
    assert bracket_prob(0.0, 74500.0, 75500.0, 1.0, 0.5) == 0.0


# ---------------------------------------------------------------------------
# up_down_15m_prob
# ---------------------------------------------------------------------------

def test_up_down_15m_prob_near_half_without_drift():
    """At drift=0, directional probability should be close to 0.5."""
    prob = up_down_15m_prob(spot=3000.0, t_hours=0.25, vol=0.60)
    assert 0.45 < prob < 0.55


def test_up_down_15m_prob_symmetry():
    """up_down_15m_prob(spot, spot) is symmetric around 0.50 at drift=0."""
    prob_up = up_down_15m_prob(spot=3000.0, t_hours=0.25, vol=0.60, drift=0.0)
    assert abs(prob_up - 0.5) < 0.05


def test_up_down_15m_prob_positive_drift_exceeds_half():
    """Large positive drift should push probability above 0.5."""
    prob = up_down_15m_prob(spot=3000.0, t_hours=0.25, vol=0.60, drift=5.0)
    assert prob > 0.5


def test_up_down_15m_prob_negative_drift_below_half():
    """Large negative drift should push probability below 0.5."""
    prob = up_down_15m_prob(spot=3000.0, t_hours=0.25, vol=0.60, drift=-5.0)
    assert prob < 0.5


def test_up_down_15m_prob_invalid_inputs_return_half():
    assert up_down_15m_prob(0.0, 0.25, 0.60) == 0.5
    assert up_down_15m_prob(3000.0, 0.0, 0.60) == 0.5
    assert up_down_15m_prob(3000.0, 0.25, 0.0) == 0.5


def test_features_to_signal_no_signal_when_quiet():
    fv = FeatureVector(
        symbol="BTCUSDT",
        timestamp=datetime.now(tz=timezone.utc),
        spot_price=0.0,
        short_return=0.0001,
        realized_vol=0.3,
        jump_detected=False,
        momentum_z=0.5,
    )
    assert features_to_signal(fv) is None


def test_features_to_signal_fires_on_high_z():
    fv = FeatureVector(
        symbol="BTCUSDT",
        timestamp=datetime.now(tz=timezone.utc),
        spot_price=0.0,
        short_return=0.005,
        realized_vol=0.5,
        jump_detected=False,
        momentum_z=3.0,
    )
    signal = features_to_signal(fv)
    assert signal is not None
    assert signal.signal_type.value == "MOMENTUM_UP"
    assert signal.confidence >= 0.55


def test_features_to_signal_fires_on_jump():
    fv = FeatureVector(
        symbol="BTCUSDT",
        timestamp=datetime.now(tz=timezone.utc),
        spot_price=0.0,
        short_return=-0.01,
        realized_vol=0.5,
        jump_detected=True,
        momentum_z=1.0,
    )
    signal = features_to_signal(fv)
    assert signal is not None
    assert signal.signal_type.value == "MOMENTUM_DOWN"


def test_features_to_signal_confidence_scales_with_z():
    fv_low = FeatureVector(
        symbol="BTCUSDT",
        timestamp=datetime.now(tz=timezone.utc),
        spot_price=0.0,
        short_return=0.003,
        realized_vol=0.5,
        jump_detected=False,
        momentum_z=2.1,
    )
    fv_high = FeatureVector(
        symbol="BTCUSDT",
        timestamp=datetime.now(tz=timezone.utc),
        spot_price=0.0,
        short_return=0.003,
        realized_vol=0.5,
        jump_detected=False,
        momentum_z=5.0,
    )
    sig_low = features_to_signal(fv_low)
    sig_high = features_to_signal(fv_high)
    assert sig_low is not None
    assert sig_high is not None
    assert sig_high.confidence > sig_low.confidence


# ---------------------------------------------------------------------------
# Kalshi fee model
# ---------------------------------------------------------------------------

def test_kalshi_fee_at_p50():
    """Fee at P=0.50 should be 0.07 * 0.5 * 0.5 = 0.0175 per contract."""
    fee = kalshi_taker_fee_per_contract(0.50)
    assert fee == KALSHI_TAKER_FEE_RATE * 0.50 * 0.50
    assert abs(fee - 0.0175) < 1e-10


def test_kalshi_fee_at_p10():
    """Fee at P=0.10 should be 0.07 * 0.1 * 0.9 = 0.0063."""
    fee = kalshi_taker_fee_per_contract(0.10)
    assert abs(fee - 0.0063) < 1e-10


def test_kalshi_fee_at_extremes():
    """Fee at P=0 and P=1 should be 0."""
    assert kalshi_taker_fee_per_contract(0.0) == 0.0
    assert kalshi_taker_fee_per_contract(1.0) == 0.0


def test_kalshi_fee_symmetric():
    """Fee(P) ≈ Fee(1-P) by the P*(1-P) formula."""
    assert kalshi_taker_fee_per_contract(0.30) == pytest.approx(kalshi_taker_fee_per_contract(0.70))


# ---------------------------------------------------------------------------
# Fee-adjusted Kelly
# ---------------------------------------------------------------------------

def test_kelly_fee_adjusted_smaller_than_raw():
    """Fee-adjusted Kelly should always produce smaller fractions than raw."""
    model_prob = 0.60
    market_price = 0.50
    # Compute what raw Kelly would be (without fees)
    b_raw = (1.0 / market_price) - 1.0
    q = 1.0 - model_prob
    raw_kelly = (model_prob * b_raw - q) / b_raw

    fee_adjusted_kelly = compute_kelly(model_prob, market_price)
    assert fee_adjusted_kelly < raw_kelly
    assert fee_adjusted_kelly > 0  # still positive — edge is big enough


def test_kelly_fee_adjusted_zero_when_edge_too_small():
    """Tiny edge should be wiped out by fees → Kelly <= 0."""
    # model_prob barely above market: 0.52 vs 0.50. Fee ≈ 0.0175.
    # Effective price ≈ 0.5175, so payout odds drop.
    f = compute_kelly(0.52, 0.50)
    # With fees, the edge is eaten away significantly
    assert f < 0.10  # very small Kelly, may be near zero


def test_capped_kelly_respects_cap_with_fees():
    """Even with high model edge, Kelly cap at 0.25 should hold."""
    f = capped_kelly(0.90, 0.50)
    assert f <= 0.25
