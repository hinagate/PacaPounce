"""Regression tests for the right-tail economics of call credit spreads."""
import math
import sys
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.monitor import ev_at_credit  # noqa: E402
from veto import skew  # noqa: E402
from veto.gates import expected_value  # noqa: E402
from veto.intent import parse  # noqa: E402


SMILE = {90.0: 0.20, 95.0: 0.20, 100.0: 0.20, 105.0: 0.20, 110.0: 0.20}


def test_call_smile_recovers_iv_with_call_payoff():
    spot, strike, T, sigma = 100.0, 105.0, 5 / 252, 0.20
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * T) / (
        sigma * math.sqrt(T)
    )
    d2 = d1 - sigma * math.sqrt(T)
    call_price = spot * NormalDist().cdf(d1) - strike * NormalDist().cdf(d2)
    smile = skew.build_smile(
        spot, T, [{"strike": strike, "mid": call_price}], is_put=False
    )
    assert abs(smile[strike] - sigma) < 1e-6


def test_positive_drift_moves_put_and_call_tail_risk_in_opposite_directions():
    T = 5 / 252
    put_zero = skew.expected_loss(100, 95, 90, T, SMILE, drift=0.0, is_put=True)
    put_up = skew.expected_loss(100, 95, 90, T, SMILE, drift=0.10, is_put=True)
    call_zero = skew.expected_loss(100, 105, 110, T, SMILE, drift=0.0, is_put=False)
    call_up = skew.expected_loss(100, 105, 110, T, SMILE, drift=0.10, is_put=False)

    assert put_up < put_zero
    assert call_up > call_zero


def test_call_ev_integrates_upper_tail_not_put_downside_tail():
    inputs = dict(
        credit=0.35,
        width=5.0,
        short_delta=0.15,
        long_delta=0.08,
        spot=100.0,
        short_strike=105.0,
        long_strike=110.0,
        dte=5,
        realized_vol=0.20,
        smile=SMILE,
    )
    call = expected_value(**inputs, is_put=False)
    wrong_tail = expected_value(**inputs, is_put=True)

    assert call["expected_loss_real_usd"] < wrong_tail["expected_loss_real_usd"]
    assert call["ev_gross_usd"] > wrong_tail["ev_gross_usd"]


def test_open_order_chase_uses_same_call_tail_model():
    ev = ev_at_credit(
        0.35, 100.0, 105.0, 110.0, 5, SMILE, 1.0, 0.0,
        is_put=False,
    )
    expected = expected_value(
        0.35, 5.0, 0.15, 0.08,
        spot=100.0,
        short_strike=105.0,
        long_strike=110.0,
        dte=5,
        realized_vol=0.20,
        smile=SMILE,
        is_put=False,
    )
    assert abs(ev - expected["ev_gross_usd"]) < 0.02


def test_unimplemented_iron_condor_is_not_advertised_or_accepted():
    raw = {
        "underlying": "SPY",
        "strategy": "iron_condor",
        "dte_range": [2, 7],
        "short_delta_target": 0.15,
        "spread_width": 5,
        "max_loss_usd": 500,
    }
    intent, error = parse(raw)
    assert intent is None
    assert "not permitted" in error
