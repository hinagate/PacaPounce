"""Sizing objectives remain deterministic and broker bounded."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto.sizing import buying_power_contracts, outcome_distribution  # noqa: E402


def test_full_buying_power_uses_largest_collateralized_integer_quantity():
    sizing = buying_power_contracts(30_582.28, 470.0, utilization=1.0)
    assert sizing["contracts"] == 65
    assert sizing["total_risk"] == 30_550.0
    assert sizing["total_risk"] <= sizing["options_buying_power"]
    assert sizing["options_buying_power"] - sizing["total_risk"] < 470.0


def test_full_buying_power_respects_broker_payload_ceiling():
    sizing = buying_power_contracts(1_000_000.0, 100.0, max_n=50)
    assert sizing["contracts"] == 50


def test_full_buying_power_fails_closed_when_one_contract_is_unaffordable():
    assert buying_power_contracts(469.99, 470.0)["contracts"] == 0


def test_equity_drift_helps_put_credit_and_hurts_call_credit_distribution():
    common = dict(
        n_contracts=1, spot=100.0, credit=0.35, sigma=0.20, T=20 / 252,
    )
    put_zero = outcome_distribution(
        short_k=95.0, long_k=90.0, is_put=True, drift=0.0, **common
    )
    put_up = outcome_distribution(
        short_k=95.0, long_k=90.0, is_put=True, drift=0.08, **common
    )
    call_zero = outcome_distribution(
        short_k=105.0, long_k=110.0, is_put=False, drift=0.0, **common
    )
    call_up = outcome_distribution(
        short_k=105.0, long_k=110.0, is_put=False, drift=0.08, **common
    )

    assert put_up["mean"] > put_zero["mean"]
    assert call_up["mean"] < call_zero["mean"]
