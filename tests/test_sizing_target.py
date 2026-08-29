"""Annual-objective sizing tests."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto.sizing import daily_return_target, target_contract_cap  # noqa: E402


def test_geometric_daily_rate_compounds_to_eight_percent():
    daily = daily_return_target(0.08, 252)
    assert abs((1 + daily) ** 252 - 1 - 0.08) < 1e-12


def test_live_trade_ev_caps_target_at_one_contract():
    cap = target_contract_cap(30561.78, 9.68, 0.08, 252)
    assert cap["contracts"] == 1
    assert abs(cap["daily_target_usd"] - 9.34) < 0.02


def test_non_positive_ev_cannot_be_sized_for_target():
    assert target_contract_cap(30561.78, 0.0)["contracts"] == 0
    assert target_contract_cap(30561.78, -1.0)["contracts"] == 0
