"""Alpaca MCP client - the agent's tool layer.

The agent talks to Alpaca through the official MCP server (stdio transport)
rather than hand-rolled REST. Chain lookups, Greek-bearing snapshots and
multi-leg order placement all go through MCP tools. There is no direct Alpaca
REST fallback in the runnable agent: incomplete MCP state fails closed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
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


def _merge_page(accumulator: Any, page: Any) -> Any:
    """Merge one MCP page without treating a non-empty page as completion."""
    if accumulator is None:
        if isinstance(page, dict):
            return {
                key: copy.deepcopy(value)
                for key, value in page.items()
                if key != "next_page_token"
            }
        return copy.deepcopy(page)
    if isinstance(accumulator, list) and isinstance(page, list):
        accumulator.extend(copy.deepcopy(page))
        return accumulator
    if isinstance(accumulator, dict) and isinstance(page, dict):
        for key, value in page.items():
            if key == "next_page_token":
                continue
            if key not in accumulator:
                accumulator[key] = copy.deepcopy(value)
            elif isinstance(accumulator[key], list) and isinstance(value, list):
                accumulator[key].extend(copy.deepcopy(value))
            elif isinstance(accumulator[key], dict) and isinstance(value, dict):
                accumulator[key] = _merge_page(accumulator[key], value)
            else:
                accumulator[key] = copy.deepcopy(value)
        return accumulator
    raise TypeError(
        f"MCP pagination changed payload type from "
        f"{type(accumulator).__name__} to {type(page).__name__}"
    )


# Alpaca MCP server 2.3.0 hand-writes these six historical tools in
# ``market_data_overrides.py`` with a fixed signature that omits ``page_token``.
# Their responses still carry ``next_page_token``, so a truncated page is
# visible but unfollowable: sending the token back is rejected by the tool
# schema. These must be completed with :func:`call_many_time_windows` instead.
# Every other paginated tool does expose ``page_token`` and is served by
# :func:`call_many_all_pages`.
NO_PAGE_TOKEN_TOOLS = frozenset({
    "get_stock_bars", "get_stock_quotes", "get_stock_trades",
    "get_crypto_bars", "get_crypto_quotes", "get_crypto_trades",
})

# One Alpaca historical response holds at most 10,000 rows. Window sizing aims
# at a fraction of that so a whole window is comfortably one page, and the
# bisection in :func:`call_many_time_windows` still catches any surprise.
MAX_ROWS_PER_PAGE = 10_000
WINDOW_ROW_BUDGET = 0.2
SESSIONS_PER_CALENDAR_DAY = 5 / 7


def bar_window_days(symbol_count: int, bars_per_session: float = 1.0) -> float:
    """Calendar-day window that keeps one bars response inside a single page.

    ``bars_per_session`` is the per-symbol row count for one trading day: 1 for
    ``1Day`` bars, ~26 for ``15Min`` bars over a regular session.
    """
    rows_per_day = (
        max(symbol_count, 1)
        * max(bars_per_session, 1e-6)
        * SESSIONS_PER_CALENDAR_DAY
    )
    return max(MAX_ROWS_PER_PAGE * WINDOW_ROW_BUDGET / rows_per_day, 1.0)


async def call_many_all_pages(
    calls: list[tuple[str, dict]], *, max_pages: int = 1000
) -> list[Any]:
    """Run paginated MCP calls concurrently and follow every token to EOF.

    Alpaca may return fewer rows than the requested ``limit``. A result is
    complete only when ``next_page_token`` is empty. Each logical request keeps
    its own token chain while sharing one MCP server session.
    """
    if max_pages < 1:
        raise ValueError("max_pages must be positive")
    unfollowable = sorted({tool for tool, _ in calls if tool in NO_PAGE_TOKEN_TOOLS})
    if unfollowable:
        # Fail loudly rather than pretending the token can be sent back: the
        # server would reject the extra argument, and a caller that swallowed
        # that error would silently keep page one.
        raise ValueError(
            f"{', '.join(unfollowable)} does not accept page_token; complete "
            f"these with call_many_time_windows instead"
        )
    states = [{
        "tool": tool,
        "kwargs": dict(kwargs),
        "result": None,
        "next_page_token": None,
        "seen_tokens": set(),
        "pages": 0,
        "done": False,
    } for tool, kwargs in calls]

    async def _one(s, state: dict) -> Any:
        kwargs = dict(state["kwargs"])
        token = state["next_page_token"]
        if token:
            kwargs["page_token"] = token
        try:
            payload = _unwrap(await s.call_tool(state["tool"], kwargs))
            _log_call(state["tool"])
            return payload
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    async with session() as s:
        while True:
            active = [state for state in states if not state["done"]]
            if not active:
                break
            pages = await asyncio.gather(*(_one(s, state) for state in active))
            for state, page in zip(active, pages):
                state["pages"] += 1
                if isinstance(page, dict) and page.get("error"):
                    state["result"] = page
                    state["done"] = True
                    continue
                if not isinstance(page, (dict, list)):
                    state["result"] = {
                        "error": f"{state['tool']} returned non-JSON page: {page}"
                    }
                    state["done"] = True
                    continue
                state["result"] = _merge_page(state["result"], page)
                token = page.get("next_page_token") if isinstance(page, dict) else None
                if not token:
                    state["done"] = True
                    continue
                if token in state["seen_tokens"]:
                    raise RuntimeError(
                        f"{state['tool']} repeated next_page_token before EOF"
                    )
                state["seen_tokens"].add(token)
                state["next_page_token"] = token
                if state["pages"] >= max_pages:
                    raise RuntimeError(
                        f"{state['tool']} exceeded {max_pages} MCP pages"
                    )

    results = []
    for state in states:
        result = state["result"]
        if isinstance(result, dict) and not result.get("error"):
            result["next_page_token"] = None
        results.append(result)
    return results


async def call_all_pages(tool: str, *, max_pages: int = 1000, **kwargs) -> Any:
    """Invoke one MCP tool and return all pages, not merely the first page."""
    return (await call_many_all_pages(
        [(tool, kwargs)], max_pages=max_pages
    ))[0]


def _time_boundary(value: Any) -> datetime:
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sort_time_series(value: Any) -> Any:
    """Keep merged MCP time-series rows chronological and duplicate-free."""
    if isinstance(value, dict):
        return {key: _sort_time_series(item) for key, item in value.items()}
    if isinstance(value, list) and value and all(isinstance(row, dict) for row in value):
        if all(row.get("t") is not None for row in value):
            unique = {}
            for row in value:
                identity = json.dumps(row, sort_keys=True, separators=(",", ":"))
                unique[identity] = row
            return sorted(unique.values(), key=lambda row: str(row["t"]))
    return value


async def call_many_time_windows(
    calls: list[tuple[str, dict]], *, window_days: float = 14,
    min_window_seconds: float = 60, concurrency: int = 24,
) -> list[Any]:
    """Fetch complete historical MCP data without relying on page_token.

    Alpaca MCP server 2.3.0 exposes ``next_page_token`` in historical responses
    but omits ``page_token`` from some tool input schemas. Long intervals are
    therefore split into non-overlapping time windows. If any window is still
    paginated, that window is bisected until its response reaches EOF.

    A call may carry its own width as an optional third element,
    ``(tool, kwargs, window_days)``: a batch mixing daily and intraday bars
    needs different windows to keep each response inside one page.
    """
    if window_days <= 0 or min_window_seconds <= 0 or concurrency < 1:
        raise ValueError("time-window pagination settings must be positive")
    states = []
    pending = []
    for index, entry in enumerate(calls):
        tool, kwargs = entry[0], entry[1]
        entry_days = entry[2] if len(entry) > 2 and entry[2] else window_days
        if entry_days <= 0:
            raise ValueError(f"{tool} window_days must be positive")
        if not kwargs.get("start") or not kwargs.get("end"):
            raise ValueError(f"{tool} requires start and end for time-window pagination")
        start = _time_boundary(kwargs["start"])
        end = _time_boundary(kwargs["end"])
        if end <= start:
            raise ValueError(f"{tool} end must be after start")
        states.append({"parts": [], "error": None})
        cursor = start
        width = timedelta(days=entry_days)
        while cursor < end:
            boundary = min(cursor + width, end)
            pending.append({
                "index": index, "tool": tool, "kwargs": dict(kwargs),
                "start": cursor, "end": boundary,
            })
            cursor = boundary

    async def _one(s, item: dict) -> Any:
        kwargs = dict(item["kwargs"])
        kwargs["start"] = _rfc3339(item["start"])
        # End is inclusive in Alpaca's specification. Subtract one microsecond
        # so adjacent windows cannot duplicate a boundary bar.
        kwargs["end"] = _rfc3339(item["end"] - timedelta(microseconds=1))
        try:
            payload = _unwrap(await s.call_tool(item["tool"], kwargs))
            _log_call(item["tool"])
            return payload
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    async with session() as s:
        while pending:
            batch, pending = pending[:concurrency], pending[concurrency:]
            payloads = await asyncio.gather(*(_one(s, item) for item in batch))
            for item, payload in zip(batch, payloads):
                state = states[item["index"]]
                if isinstance(payload, dict) and payload.get("error"):
                    state["error"] = payload
                    continue
                if not isinstance(payload, dict):
                    state["error"] = {
                        "error": f"{item['tool']} returned non-JSON window: {payload}"
                    }
                    continue
                if payload.get("next_page_token"):
                    duration = (item["end"] - item["start"]).total_seconds()
                    if duration <= min_window_seconds:
                        state["error"] = {
                            "error": (
                                f"{item['tool']} remains paginated at the "
                                f"{duration:g}s minimum window"
                            )
                        }
                        continue
                    midpoint = item["start"] + (item["end"] - item["start"]) / 2
                    pending.extend([
                        {**item, "end": midpoint},
                        {**item, "start": midpoint},
                    ])
                    continue
                clean = {key: value for key, value in payload.items()
                         if key != "next_page_token"}
                state["parts"].append((item["start"], clean))

    results = []
    for state in states:
        if state["error"]:
            results.append(state["error"])
            continue
        merged = None
        for _, part in sorted(state["parts"], key=lambda pair: pair[0]):
            merged = _merge_page(merged, part)
        if isinstance(merged, dict):
            merged = _sort_time_series(merged)
            merged["next_page_token"] = None
        results.append(merged)
    return results


async def call_time_windows(tool: str, **kwargs) -> Any:
    """Fetch one historical MCP request to EOF using adaptive time windows."""
    settings = {
        key: kwargs.pop(key)
        for key in ("window_days", "min_window_seconds", "concurrency")
        if key in kwargs
    }
    return (await call_many_time_windows([(tool, kwargs)], **settings))[0]


def bars_call(tool: str, kwargs: dict, *, symbols: str,
              bars_per_session: float = 1.0) -> tuple[str, dict, float]:
    """Build one time-window bars call sized from its own symbol count."""
    return (tool, kwargs, bar_window_days(
        len([part for part in str(symbols).split(",") if part.strip()]),
        bars_per_session,
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
