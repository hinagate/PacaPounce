"""Poe LLM client (OpenAI-compatible) - proposes trade INTENT, never orders."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import config

SCHEMA = """You are an options strategist. You propose trade INTENT only.
You never choose specific contracts, strikes, or symbols - deterministic code
resolves your intent against the real option chain.

Respond with ONE JSON object and nothing else:
{
  "underlying": "SPY" | "QQQ",
  "direction": "bullish" | "bearish" | "neutral",
  "strategy": "put_credit_spread" | "call_credit_spread",
  "dte_range": [min_days, max_days],
  "short_delta_target": 0.10-0.35,
  "spread_width": 1-10,
  "max_loss_usd": <= 500,
  "thesis": "one sentence, grounded in the data given",
  "invalidation": "one sentence: what would prove this wrong"
}
Use call_credit_spread only for bearish or neutral intent. Use put_credit_spread
only for bullish or neutral intent."""


def system_prompt() -> str:
    """Schema plus the real limits.

    The model is told the constraints rather than left to guess them. A pass
    rate measured against a model that was set up to fail measures the prompt,
    not the model.
    """
    return SCHEMA + (
        "\n\nHARD CONSTRAINTS (a proposal outside these is discarded unpriced):"
        f"\n  underlying     one of: {', '.join(config.ALLOWLIST)}"
        f"\n  dte_range      both values within [{config.DTE_MIN}, {config.DTE_MAX}]"
        f"\n  max_loss_usd   at most {config.MAX_LOSS_USD:.0f}"
        f"\n  short_delta_target  within [0.05, 0.45]"
        "\n  strategy       defined-risk only; naked short options are never accepted"
        "\n\nGROUNDING: Base the thesis only on fields in the supplied MCP market brief."
        "\nDo not invent support levels, moving averages, news, VIX, IV rank, or trends"
        "\nthat are not explicitly present. Missing data means unavailable, not zero."
    )


def _post(payload: dict, timeout: int = 120) -> dict:
    req = urllib.request.Request(
        f"{config.POE_BASE}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {config.POE_KEY}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a reply, tolerating code fences."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return None


def propose(market_brief: str, feedback: str | None = None,
            temperature: float | None = None) -> tuple[dict | None, str]:
    """Ask the model for one trade intent. Returns (intent, raw_reply)."""
    user = market_brief
    if feedback:
        user += (f"\n\nYour previous proposal was REJECTED:\n{feedback}\n"
                 "Propose a revision that addresses this specific reason.")

    payload = {
        "model": config.POE_MODEL,
        "messages": [{"role": "system", "content": system_prompt()},
                     {"role": "user", "content": user}],
        # Reasoning tokens are billed against this budget. gemini-3.7-flash
        # spends 20-200 of them thinking; too small a cap returns empty content.
        "max_tokens": config.LLM_MAX_TOKENS,
    }
    if temperature is not None:
        payload["temperature"] = temperature

    try:
        data = _post(payload)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    return _extract_json(raw), raw


OPTION_MR_REVIEW_SCHEMA = """You are the event-risk reviewer for a PAPER long-call
options trade. Deterministic Python has already computed the underlying signal, exact
OCC contract, quote, quantity, premium cap, and exit rules. You may not change those
numbers, introduce another symbol, or propose a stock order. Review only the supplied
Alpaca MCP news headlines/summaries. Do not claim that an earnings calendar was checked:
the supplied source is a news feed, not an earnings calendar, and you have no verified
search tool in this API call.

VETO only when the supplied news contains a concrete, material near-term event or adverse
fact that makes a 2-3 session long mean-reversion hold unusually gap-prone. Otherwise
APPROVE. Respond with one JSON object and nothing else:
{
  "decision": "approve" | "veto",
  "thesis": "one sentence grounded in the numeric signal and supplied news",
  "event_risk": "specific supplied risk, or none observed in supplied news",
  "invalidation": "hard stop or failure condition stated in the candidate"
}
"""


def review_option_mr_candidate(candidate_brief: str) -> tuple[dict | None, str]:
    """One bounded AI review; calculations and order authority remain in Python."""
    payload = {
        "model": config.POE_MODEL,
        "messages": [
            {"role": "system", "content": OPTION_MR_REVIEW_SCHEMA},
            {"role": "user", "content": candidate_brief},
        ],
        "max_tokens": min(config.LLM_MAX_TOKENS, 2000),
    }
    try:
        data = _post(payload)
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    parsed = _extract_json(raw)
    if parsed is not None:
        parsed["decision"] = str(parsed.get("decision") or "").lower()
        if parsed["decision"] not in {"approve", "veto"}:
            parsed = None
    return parsed, raw
