"""Keep every test out of the judged account's runtime directory.

The runtime paths are derived from ``ALPACA_ACCOUNT_ID`` once, at import time.
A test that redirects the account id alone therefore keeps writing to the real
account's files: it changes who the code thinks it is without changing where it
writes. That is how a ``TEST-ACCOUNT`` decision ended up in the live audit log
that the judge dashboard renders.

Redirecting the paths themselves, for every test, makes that impossible rather
than merely discouraged. Tests that already point these at their own ``tmp_path``
still work - they simply override an already-safe default.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from veto import config  # noqa: E402

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


@pytest.fixture(autouse=True)
def isolate_runtime_paths(tmp_path, monkeypatch):
    """Point every runtime write at this test's own directory."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(config, "RUNTIME_DATA_DIR", runtime, raising=False)
    for attribute, filename in RUNTIME_PATHS.items():
        monkeypatch.setattr(config, attribute, runtime / filename, raising=False)
    return runtime


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
