from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from veto import mcp_client


class FakeSession:
    def __init__(self, pages_by_symbol):
        self.pages_by_symbol = pages_by_symbol
        self.calls = []

    async def call_tool(self, tool, kwargs):
        self.calls.append((tool, dict(kwargs)))
        symbol = kwargs["symbols"]
        pages = self.pages_by_symbol[symbol]
        index = 0 if not kwargs.get("page_token") else int(kwargs["page_token"])
        return SimpleNamespace(structuredContent=pages[index], content=[])


class FakeTimeWindowSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool, kwargs):
        self.calls.append((tool, dict(kwargs)))
        start = mcp_client._time_boundary(kwargs["start"])
        end = mcp_client._time_boundary(kwargs["end"])
        duration_days = (end - start).total_seconds() / 86400
        if duration_days > 7:
            payload = {"bars": {kwargs["symbols"]: [{"t": kwargs["start"]}]},
                       "next_page_token": "hidden-token"}
        else:
            payload = {"bars": {kwargs["symbols"]: [{"t": kwargs["start"]}]},
                       "next_page_token": None}
        return SimpleNamespace(structuredContent=payload, content=[])


def fake_session_context(fake):
    @asynccontextmanager
    async def _context():
        yield fake

    return _context


def test_call_all_pages_follows_token_until_eof_and_merges_symbol_rows(monkeypatch):
    fake = FakeSession({"AAPL": [
        {"bars": {"AAPL": [{"t": "1"}]}, "next_page_token": "1"},
        {"bars": {"AAPL": [{"t": "2"}]}, "next_page_token": None},
    ]})
    monkeypatch.setattr(mcp_client, "session", fake_session_context(fake))
    monkeypatch.setattr(mcp_client, "_log_call", lambda _tool: None)

    result = mcp_client.run(mcp_client.call_all_pages(
        "get_option_snapshot", symbols="AAPL"
    ))

    assert result["bars"]["AAPL"] == [{"t": "1"}, {"t": "2"}]
    assert result["next_page_token"] is None
    assert [kwargs.get("page_token") for _, kwargs in fake.calls] == [None, "1"]


def test_call_many_all_pages_keeps_independent_token_chains(monkeypatch):
    fake = FakeSession({
        "AAPL": [
            {"bars": {"AAPL": [1]}, "next_page_token": "1"},
            {"bars": {"AAPL": [2]}, "next_page_token": None},
        ],
        "MSFT": [{"bars": {"MSFT": [3]}, "next_page_token": None}],
    })
    monkeypatch.setattr(mcp_client, "session", fake_session_context(fake))
    monkeypatch.setattr(mcp_client, "_log_call", lambda _tool: None)

    results = mcp_client.run(mcp_client.call_many_all_pages([
        ("get_option_snapshot", {"symbols": "AAPL"}),
        ("get_option_snapshot", {"symbols": "MSFT"}),
    ]))

    assert results[0]["bars"]["AAPL"] == [1, 2]
    assert results[1]["bars"]["MSFT"] == [3]
    assert len(fake.calls) == 3


def test_call_all_pages_rejects_repeated_token_loop(monkeypatch):
    fake = FakeSession({"AAPL": [
        {"bars": {"AAPL": [1]}, "next_page_token": "1"},
        {"bars": {"AAPL": [2]}, "next_page_token": "1"},
    ]})
    monkeypatch.setattr(mcp_client, "session", fake_session_context(fake))
    monkeypatch.setattr(mcp_client, "_log_call", lambda _tool: None)

    with pytest.raises(RuntimeError, match="repeated next_page_token"):
        mcp_client.run(mcp_client.call_all_pages(
            "get_option_snapshot", symbols="AAPL"
        ))


def test_time_windows_bisect_until_each_mcp_response_reaches_eof(monkeypatch):
    fake = FakeTimeWindowSession()
    monkeypatch.setattr(mcp_client, "session", fake_session_context(fake))
    monkeypatch.setattr(mcp_client, "_log_call", lambda _tool: None)

    result = mcp_client.run(mcp_client.call_time_windows(
        "get_stock_bars", symbols="AAPL", timeframe="15Min",
        start="2026-01-01", end="2026-01-29", window_days=14,
    ))

    rows = result["bars"]["AAPL"]
    assert result["next_page_token"] is None
    assert len(rows) == 4
    assert len(fake.calls) == 6  # two rejected 14-day windows + four 7-day windows
    assert all("page_token" not in kwargs for _, kwargs in fake.calls)


def test_token_following_refuses_tools_whose_schema_omits_page_token():
    # MCP 2.3.0 returns next_page_token for get_stock_bars but will not accept
    # it back. Sending it anyway is rejected by the server, so the mistake must
    # surface here rather than as a silently truncated first page.
    with pytest.raises(ValueError, match="does not accept page_token"):
        mcp_client.run(mcp_client.call_all_pages("get_stock_bars", symbols="AAPL"))
    with pytest.raises(ValueError, match="call_many_time_windows"):
        mcp_client.run(mcp_client.call_many_all_pages([
            ("get_option_snapshot", {"symbols": "AAPL"}),
            ("get_stock_bars", {"symbols": "AAPL"}),
        ]))


def test_bar_window_days_keeps_a_window_inside_one_alpaca_page():
    budget = mcp_client.MAX_ROWS_PER_PAGE * mcp_client.WINDOW_ROW_BUDGET
    for symbols, per_session in ((30, 1.0), (30, 26.0), (1, 1.0), (2, 1.0)):
        days = mcp_client.bar_window_days(symbols, per_session)
        rows = days * symbols * per_session * mcp_client.SESSIONS_PER_CALENDAR_DAY
        assert rows <= budget + 1e-6
        assert days >= 1.0


def test_time_windows_honour_a_per_call_window_width(monkeypatch):
    fake = FakeTimeWindowSession()
    monkeypatch.setattr(mcp_client, "session", fake_session_context(fake))
    monkeypatch.setattr(mcp_client, "_log_call", lambda _tool: None)

    # One batch, two different widths: the daily series splits into 7-day
    # windows while the intraday series is already inside one window.
    daily, intraday = mcp_client.run(mcp_client.call_many_time_windows(
        [
            ("get_stock_bars", {"symbols": "AAPL", "start": "2026-01-01",
                                "end": "2026-01-29"}, 7),
            ("get_stock_bars", {"symbols": "MSFT", "start": "2026-01-01",
                                "end": "2026-01-03"}, 5),
        ],
        window_days=90,
    ))

    assert len(daily["bars"]["AAPL"]) == 4
    assert len(intraday["bars"]["MSFT"]) == 1
    assert all("page_token" not in kwargs for _, kwargs in fake.calls)


def test_bars_call_sizes_its_window_from_its_own_symbol_list():
    tool, kwargs, window = mcp_client.bars_call(
        "get_stock_bars", {"symbols": "AAPL,MSFT,NVDA"},
        symbols="AAPL,MSFT,NVDA", bars_per_session=26.0,
    )
    assert tool == "get_stock_bars"
    assert kwargs == {"symbols": "AAPL,MSFT,NVDA"}
    assert window == mcp_client.bar_window_days(3, 26.0)
