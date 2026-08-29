"""MCP market-regime feature tests: compact inputs, no model or broker calls."""
from datetime import date

import run as app
from veto import config, llm, regime


def test_completed_history_excludes_incomplete_broker_day():
    payload = {"bars": {"SPY": [
        {"t": "2026-08-25T04:00:00Z", "c": 100},
        {"t": "2026-08-26T04:00:00Z", "c": 101},
        {"t": "2026-08-27T04:00:00Z", "c": 150},
    ]}}

    assert regime._completed_closes(payload, "SPY", date(2026, 8, 27)) == [100, 101]


def test_atm_selection_uses_nearest_three_day_expiry_and_both_sides(monkeypatch):
    monkeypatch.setattr(config, "DTE_MIN", 1)
    monkeypatch.setattr(config, "DTE_MAX", 7)
    rows = []
    for expiry in ("2026-08-28", "2026-08-31", "2026-09-03"):
        for option_type, suffix in (("call", "C"), ("put", "P")):
            for strike in (99, 100, 101):
                rows.append({
                    "symbol": f"TEST-{expiry}-{suffix}-{strike}",
                    "expiration_date": expiry,
                    "type": option_type,
                    "strike_price": str(strike),
                    "tradable": True,
                })

    symbols, expiry, dte = regime._select_atm_contracts(
        rows, 100.2, date(2026, 8, 27)
    )

    assert expiry == "2026-08-31"
    assert dte == 4
    assert symbols == [
        "TEST-2026-08-31-C-100",
        "TEST-2026-08-31-P-100",
    ]


def test_feature_format_is_compact_and_does_not_claim_unavailable_data():
    line = regime.format_feature("SPY", {
        "spot": 771.175,
        "return_1d": 0.0052,
        "return_5d": 0.0114,
        "rv20": 0.1043,
        "atm_iv": 0.0918,
        "atm_dte": 4,
        "iv_rv": 0.88,
        "iv_asof": "2026-08-27T19:59:59Z",
    })

    assert line == (
        "SPY: spot 771.17 | 1D +0.52% | 5D +1.14% | RV20 10.43% | "
        "ATM IV(4D) 9.18% | IV/RV 0.88 | IV quote 2026-08-27T19:59:59Z"
    )
    assert "VIX" not in line
    assert "rank" not in line.lower()


def test_market_brief_passes_mcp_regime_to_model(monkeypatch):
    monkeypatch.setattr(
        app.mcp_client,
        "call_many",
        lambda calls: calls,
    )
    monkeypatch.setattr(
        app.mcp_client,
        "run",
        lambda _calls: [
            {"quotes": {
                "SPY": {"bp": 771.1, "ap": 771.2},
                "QQQ": {"bp": 721.0, "ap": 721.1},
            }},
            {"equity": "100000"},
            {"timestamp": "2026-08-27T15:59:00-04:00", "is_open": True},
        ],
    )
    monkeypatch.setattr(
        app.regime,
        "snapshot",
        lambda spots, broker_date: {
            symbol: {
                "spot": spot,
                "return_1d": 0.005,
                "rv20": 0.10,
                "atm_iv": 0.12,
                "atm_dte": 4,
                "iv_rv": 1.2,
            }
            for symbol, spot in spots.items()
        },
    )

    brief = app.market_brief()

    assert "SPY: spot 771.15 | 1D +0.50%" in brief
    assert "QQQ: spot 721.05 | 1D +0.50%" in brief
    assert "ATM IV(4D) 12.00% | IV/RV 1.20" in brief
    assert "Regime source: Alpaca MCP" in brief
    assert "VIX" not in brief


def test_system_prompt_forbids_unsupported_market_claims():
    prompt = llm.system_prompt()

    assert "Base the thesis only on fields in the supplied MCP market brief" in prompt
    assert "Do not invent support levels, moving averages, news, VIX, IV rank" in prompt
    assert "Missing data means unavailable, not zero" in prompt
