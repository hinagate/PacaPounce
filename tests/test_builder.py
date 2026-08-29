"""Contract construction uses the exact price that execution will submit."""
from datetime import date
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto import builder, config  # noqa: E402
from veto.intent import Intent  # noqa: E402


def test_full_buying_power_sizes_from_rounded_executable_credit(monkeypatch):
    """A half-cent midpoint must not create one unaffordable extra contract."""
    short_symbol = "SPY260829P00100000"
    long_symbol = "SPY260829P00096000"
    chain = {
        "snapshots": {
            short_symbol: {
                "latestQuote": {"bp": 1.63, "ap": 1.64},
                "greeks": {"delta": 0.20},
                "impliedVolatility": 0.20,
            },
            long_symbol: {
                "latestQuote": {"bp": 1.00, "ap": 1.00},
                "greeks": {"delta": 0.10},
                "impliedVolatility": 0.22,
            },
        }
    }
    monkeypatch.setattr(builder.mcp_client, "call", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(builder.mcp_client, "run", lambda _call: chain)
    monkeypatch.setattr(builder, "realized_vol", lambda _symbol: 0.20)
    monkeypatch.setattr(builder, "vol_profile", lambda _symbol: {"ewma": 0.20})
    monkeypatch.setattr(builder.skew, "chain_is_sane", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(builder.skew, "build_smile", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(config, "FULL_BUYING_POWER", True)

    intent = Intent(
        underlying="SPY",
        direction="bullish",
        strategy="put_credit_spread",
        dte_min=2,
        dte_max=2,
        short_delta_target=0.20,
        spread_width=4.0,
        max_loss_usd=400.0,
        thesis="test",
        invalidation="test",
    )
    spread, error = builder.build(
        intent,
        spot=102.0,
        today=date(2026, 8, 27),
        sizing_context={
            "equity": 100_000.0,
            "options_buying_power": 100_000.0,
            "options_bp_utilization": 1.0,
        },
    )

    assert error == ""
    assert spread is not None
    assert spread["credit"] == 0.63
    assert spread["kelly"]["max_loss_per_contract"] == 337.0
    assert spread["qty"] == 296
    assert spread["kelly"]["total_risk"] == 99_752.0
    assert spread["kelly"]["total_risk"] <= 100_000.0
