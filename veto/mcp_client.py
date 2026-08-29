"""Alpaca MCP client - the agent's tool layer.

The agent talks to Alpaca through the official MCP server (stdio transport)
rather than hand-rolled REST. Chain lookups, Greek-bearing snapshots and
multi-leg order placement all go through MCP tools. There is no direct Alpaca
REST fallback in the runnable agent: incomplete MCP state fails closed.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import config

def _server_cmd() -> str:
    """The package ships a console script only (no __main__), so resolve the
    executable that sits beside the running interpreter."""
    scripts = Path(sys.executable).parent
    for name in ("alpaca-mcp-server.exe", "alpaca-mcp-server"):
        cand = scripts / name
        if cand.exists():
            return str(cand)
    return "alpaca-mcp-server"  # fall back to PATH


SERVER_CMD = _server_cmd()
SERVER_ARGS = ["--transport", "stdio"]


def _env() -> dict[str, str]:
    """The MCP server reads Alpaca creds from the environment.
    ALPACA_PAPER_TRADE=true keeps it pinned to the paper account."""
    env = dict(os.environ)
    env.update({
        "ALPACA_API_KEY": config.ALPACA_KEY,
        "ALPACA_SECRET_KEY": config.ALPACA_SECRET,
        "ALPACA_PAPER_TRADE": "true",
        # A polling monitor starts short-lived stdio sessions. Suppress the
        # multi-line FastMCP banner and routine startup INFO on every session.
        "FASTMCP_SHOW_SERVER_BANNER": "false",
        "FASTMCP_LOG_LEVEL": "WARNING",
    })
    return env


@asynccontextmanager
async def session():
    params = StdioServerParameters(command=SERVER_CMD, args=SERVER_ARGS, env=_env())
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


async def list_tools() -> list[dict]:
    async with session() as s:
        res = await s.list_tools()
        return [{"name": t.name, "description": (t.description or "").split("\n")[0]}
                for t in res.tools]


async def call(tool: str, **kwargs) -> Any:
    """Invoke one MCP tool and return its parsed payload."""
    async with session() as s:
        res = await s.call_tool(tool, kwargs)
        _log_call(tool)
        return _unwrap(res)


async def call_many(calls: list[tuple[str, dict]]) -> list[Any]:
    """Run independent tools concurrently over one server session.

    The monitor used to pay both the MCP process startup cost and each network
    round trip serially.  A single session plus ``gather`` keeps a polling
    cycle comfortably inside its requested cadence while preserving the input
    order of results.
    """
    async def _one(s, tool: str, kwargs: dict) -> Any:
        try:
            result = _unwrap(await s.call_tool(tool, kwargs))
            _log_call(tool)
            return result
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    async with session() as s:
        return list(await asyncio.gather(
            *(_one(s, tool, kwargs) for tool, kwargs in calls)
        ))


def _log_call(tool: str) -> None:
    """Count MCP tool usage so the dashboard can report it factually."""
    path = config.MCP_CALL_LOG
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        counts = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        counts[tool] = counts.get(tool, 0) + 1
        path.write_text(json.dumps(counts, indent=2), encoding="utf-8")
    except Exception:
        pass  # telemetry must never break a trade


def tool_counts() -> dict:
    path = config.MCP_CALL_LOG
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _strip_envelope(payload: Any) -> Any:
    """Unwrap the server's security envelope.

    Responses arrive as {"_alpaca_mcp_security": {...}, "data": <payload>}. The
    envelope marks the contents as untrusted_tool_output - a prompt-injection
    guard. We surface only the data, and it is never fed back to the model as
    instructions: the LLM proposes, and API values are read by code alone.
    """
    if isinstance(payload, dict) and "_alpaca_mcp_security" in payload:
        payload = payload.get("data")
    # Second layer: list-returning tools wrap their rows as {"result": [...]}.
    # Unwrap only when it is the sole key, so a tool that legitimately returns a
    # field named "result" alongside others is left intact.
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


class UnknownTool(RuntimeError):
    """The server does not expose this tool - a typo, or a version drift."""


def _unwrap(res) -> Any:
    """MCP returns content blocks; prefer structured output, fall back to text.

    A bad tool name comes back as ordinary text ("Unknown tool: 'x'"), which then
    fails as an AttributeError somewhere far away. Raise at the source instead.
    """
    if getattr(res, "structuredContent", None):
        return _strip_envelope(res.structuredContent)
    parts = []
    for block in getattr(res, "content", []) or []:
        text = getattr(block, "text", None)
        if text is None:
            continue
        try:
            return _strip_envelope(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            parts.append(text)
    return "\n".join(parts) if parts else None


def run(coro):
    """Sync entry point for the agent loop."""
    return asyncio.run(coro)
