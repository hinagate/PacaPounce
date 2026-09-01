"""Profit protection shared by every lane that holds a position.

Being wrong about direction is a probability outcome, and a stop bounds it.
Being *right* and ending flat is not a probability outcome - it is a missing
control. Any lane that can hold a winning position needs the same answer to it,
so the mechanism lives here rather than in each strategy.

What is genuinely shared is one sequence:

    executable P&L  ->  high-water mark  ->  volatility-adjusted trail
                    ->  confirmed breach  ->  close

What is *not* shared is the policy: a credit spread and a long call have
different P&L dynamics, so they need different thresholds, a different
denominator to measure capture against, and their own storage. Those are passed
in. Trying to share them too would produce a function with ten arguments and no
abstraction.

Two invariants hold for every caller:

* A breach requires ``pnl > 0``, so a ratchet close is always taken in profit.
  Losses are the stop's job, never this one's.
* The trailing floor is clamped at zero and the giveback is validated below 1,
  so a position that has been right can never be trailed back into a loss.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import pstdev


@dataclass(frozen=True)
class Policy:
    """Per-lane ratchet thresholds.

    ``volatility_mode`` selects what "volatile" means for this lane. A credit
    spread's P&L grinds, so the size of its *changes* is the informative signal;
    a long call's P&L swings with delta, so the dispersion of its *levels* is.
    """

    arm_pct: float
    giveback_pct: float
    high_vol_giveback_pct: float
    high_vol_pct: float
    confirmations: int
    history_limit: int = 60
    volatility_mode: str = "changes"

    def __post_init__(self) -> None:
        if self.arm_pct <= 0:
            raise ValueError("ratchet arm_pct must be positive")
        for name in ("giveback_pct", "high_vol_giveback_pct"):
            value = getattr(self, name)
            if not 0 < value < 1:
                raise ValueError(
                    f"ratchet {name} must be in (0, 1); at 1 or more a protected "
                    "position could be trailed back into a loss"
                )
        if self.high_vol_giveback_pct > self.giveback_pct:
            raise ValueError(
                "the high-volatility giveback must tighten the trail, not loosen it"
            )
        if self.confirmations < 1:
            raise ValueError("ratchet confirmations must be at least one")
        if self.history_limit < 3:
            raise ValueError("ratchet history_limit must retain at least three marks")
        if self.volatility_mode not in {"changes", "levels"}:
            raise ValueError("ratchet volatility_mode must be 'changes' or 'levels'")


def _dispersion(history: list[float], mode: str) -> float:
    if mode == "changes":
        changes = [b - a for a, b in zip(history, history[1:])]
        return pstdev(changes) if len(changes) >= 2 else 0.0
    return pstdev(history) if len(history) >= 2 else 0.0


def update(*, pnl: float | None, denominator: float, history: list[float],
           breach_count: int, policy: Policy, quote_ready: bool = True,
           high_water: float = 0.0, arm_threshold: float | None = None) -> dict:
    """Advance one position's ratchet by a single executable mark.

    ``pnl`` is measured at the price the position could actually be closed at -
    the live bid, never a midpoint - so the floor is a number that can be
    realised rather than a mark that cannot be sold at. ``denominator`` is what
    capture is measured against: opening credit for a credit spread, premium
    paid for a long option.

    ``high_water`` is the caller's stored peak. It is carried rather than
    recomputed because history is truncated to ``history_limit``: after a
    restart a lane may recover a full peak alongside only a recent tail, and
    rebuilding the peak from that tail would silently lower the floor.

    ``arm_threshold`` overrides ``policy.arm_pct * denominator`` when the lane
    can express "this has been right" in better units than a share of the
    capital committed. A long option's P&L moves by its leverage, which varies
    several-fold between contracts, so a fixed share of premium asks for a
    different underlying move from each one.
    """
    series = [float(value) for value in history]
    if pnl is not None:
        series.append(float(pnl))
    series = series[-policy.history_limit:]

    base = {
        "history": series,
        "armed": False,
        "high_water_pnl": round(max(float(high_water), *series, 0.0), 2)
        if series else round(max(float(high_water), 0.0), 2),
        "arm_threshold_pnl": 0.0,
        "trailing_floor_pnl": 0.0,
        "breach_count": 0,
        "giveback_pct": policy.giveback_pct,
        "volatility": 0.0,
        "volatility_ratio": 0.0,
        "high_volatility": False,
        "slope_nonpositive": False,
        "close": False,
        "reason": None,
    }
    if pnl is None or denominator <= 0:
        # Nothing usable to measure against; hold the existing confirmations
        # rather than silently resetting them.
        base["breach_count"] = max(int(breach_count), 0)
        return base

    scale = max(denominator, 0.01)
    peak = max(float(high_water), *series, 0.0)
    threshold = (
        policy.arm_pct * scale if arm_threshold is None else max(arm_threshold, 0.0)
    )
    armed = peak >= threshold

    dispersion = _dispersion(series, policy.volatility_mode)
    volatility_ratio = dispersion / scale
    high_volatility = volatility_ratio >= policy.high_vol_pct
    giveback = (
        policy.high_vol_giveback_pct if high_volatility else policy.giveback_pct
    )
    # Clamped at zero so no configuration can trail a winner into a loss.
    trailing_floor = max(peak * (1.0 - giveback), 0.0) if armed else 0.0

    # Do not close into a still-rising position: a dip that is already being
    # bought back is not a giveback.
    slope_nonpositive = len(series) >= 3 and series[-1] <= series[-3]
    below_floor = (
        armed
        and pnl > 0                 # a ratchet close is always taken in profit
        and pnl <= trailing_floor
        and slope_nonpositive
        and quote_ready
    )
    breaches = int(breach_count) + 1 if below_floor else 0
    close = breaches >= policy.confirmations

    base.update({
        "armed": armed,
        "high_water_pnl": round(peak, 2),
        "arm_threshold_pnl": round(threshold, 2),
        "trailing_floor_pnl": round(trailing_floor, 2),
        "breach_count": breaches,
        "capture_pct": round(pnl / scale, 6),
        "giveback_pct": giveback,
        "volatility": round(dispersion, 2),
        "volatility_ratio": round(volatility_ratio, 6),
        "high_volatility": high_volatility,
        "slope_nonpositive": slope_nonpositive,
        "close": close,
        "reason": "profit_ratchet" if close else None,
    })
    return base
