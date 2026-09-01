"""Keep the test suite away from the live account, in both directions.

Two separate hazards, both of which have actually fired:

* **Writes.** The runtime paths are derived from ``ALPACA_ACCOUNT_ID`` once, at
  import time. A test that redirects the account id alone keeps writing to the
  real account's files: it changes who the code thinks it is without changing
  where it writes. That is how a ``TEST-ACCOUNT`` decision reached the live
  audit log the judge dashboard renders.

* **Orders.** ``test_session`` drives the real ``autonomous_loop`` with
  fabricated snapshots. The loop calls ``mean_reversion.maybe_enter`` before it
  checks anything else, so as soon as a fabricated timestamp landed inside a
  configured decision window, the suite placed **real broker orders** on the
  judged account - five of them, on 2026-09-01, with client ids stamped
  ``20260826`` from the fake snapshot. Adding an 11:00 decision window is what
  turned a previously harmless fixture into a live trading path, which is
  exactly the kind of coupling no one can be expected to remember.

So the network is closed by default rather than mocked case by case. A test that
wants MCP behaviour must say so by patching it, which is explicit and local; a
test that forgets gets a loud failure instead of a filled order.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto import config, mcp_client  # noqa: E402

# Everything the agent writes at runtime, keyed by the config attribute holding
# it. A new runtime path added to config.py should be added here too.
RUNTIME_PATHS = {
    "VERDICT_LOG": "verdicts.jsonl",
    "SESSION_LOG": "session_log.jsonl",
    "RISK_STATE_FILE": "risk_state.json",
    "MCP_CALL_LOG": "mcp_calls.json",
    "OPTION_MR_STATE_FILE": "option_mr_state.json",
    "OPTION_MR_LOG": "option_mr_decisions.jsonl",
}

# Every public helper routes through mcp_client.session() to reach Alpaca, so
# that one function is the whole surface. Blocking it rather than each wrapper
# leaves the wrappers themselves testable: tests/test_mcp_client.py exercises the
# real pagination logic against a fake session, which is exactly the seam here.
MCP_CHOKEPOINT = "session"


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path, monkeypatch):
    """Point every runtime write at this test's own directory."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(config, "RUNTIME_DATA_DIR", runtime, raising=False)
    for attribute, filename in RUNTIME_PATHS.items():
        monkeypatch.setattr(config, attribute, runtime / filename, raising=False)
    return runtime


@pytest.fixture(autouse=True)
def block_live_broker(monkeypatch):
    """Fail loudly instead of reaching Alpaca.

    Patched on the ``veto.mcp_client`` module itself, so it covers every module
    that imported it. A test that legitimately needs one of these re-patches it
    afterwards and wins, because its own monkeypatch is applied later.
    """
    def _blocked(*_args, **_kwargs):
        raise AssertionError(
            "the test suite tried to open a live Alpaca MCP session. Tests must "
            "not touch the judged account: patch mcp_client.call (or whatever "
            "function makes the call), or patch mcp_client.session with a fake."
        )

    monkeypatch.setattr(mcp_client, MCP_CHOKEPOINT, _blocked)


def test_no_runtime_path_escapes_the_isolation():
    """Fails if config gains a runtime path this fixture does not redirect."""
    live = Path(config.ROOT) / "data" / "accounts"
    escaped = sorted(
        name for name in dir(config)
        if name.isupper()
        and isinstance(getattr(config, name), Path)
        and live in getattr(config, name).parents
    )
    assert not escaped, (
        f"{escaped} still resolve inside the live account directory; add them to "
        "RUNTIME_PATHS so tests cannot write to the judged audit trail"
    )


def test_every_broker_helper_still_routes_through_the_blocked_chokepoint():
    """Fails if mcp_client grows a way to reach Alpaca without session()."""
    import inspect

    source = inspect.getsource(mcp_client)
    offenders = []
    for name in dir(mcp_client):
        if name.startswith("_") or name == MCP_CHOKEPOINT:
            continue
        function = getattr(mcp_client, name)
        if not inspect.iscoroutinefunction(function):
            continue
        body = inspect.getsource(function)
        if "session()" not in body and "call_many" not in body and "call(" not in body:
            offenders.append(name)
    assert not offenders, (
        f"{offenders} reach the broker without going through "
        f"mcp_client.{MCP_CHOKEPOINT}(), so blocking it no longer protects tests"
    )
    assert "stdio_client" in source, "the chokepoint moved; revisit this guard"


def test_the_live_broker_block_actually_fires():
    with pytest.raises(AssertionError, match="not touch the judged account"):
        mcp_client.run(mcp_client.call("get_clock"))
