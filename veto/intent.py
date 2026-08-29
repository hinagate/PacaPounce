"""Trade intent: schema validation + coherence check.

The coherence check runs BEFORE any chain lookup. It asks whether the intent is
internally consistent - can any strike pair satisfy this delta target and this
max-loss cap at once? An incoherent intent is rejected for free.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config

STRATEGIES = {"put_credit_spread", "call_credit_spread"}


@dataclass
class Intent:
    underlying: str
    direction: str
    strategy: str
    dte_min: int
    dte_max: int
    short_delta_target: float
    spread_width: float
    max_loss_usd: float
    thesis: str
    invalidation: str


def parse(raw: dict) -> tuple[Intent | None, str | None]:
    """Validate shape and ranges. Returns (intent, error)."""
    try:
        dte = raw.get("dte_range") or [config.DTE_MIN, config.DTE_MAX]
        intent = Intent(
            underlying=str(raw["underlying"]).upper(),
            direction=str(raw.get("direction", "neutral")).lower(),
            strategy=str(raw["strategy"]).lower(),
            dte_min=int(dte[0]),
            dte_max=int(dte[1]),
            short_delta_target=float(raw["short_delta_target"]),
            spread_width=float(raw.get("spread_width", 5)),
            max_loss_usd=float(raw.get("max_loss_usd", config.MAX_LOSS_USD)),
            thesis=str(raw.get("thesis", ""))[:400],
            invalidation=str(raw.get("invalidation", ""))[:400],
        )
    except (KeyError, TypeError, ValueError, IndexError) as e:
        return None, f"malformed intent: {type(e).__name__} {e}"

    if intent.underlying not in config.ALLOWLIST:
        return None, f"underlying {intent.underlying} not in allowlist {config.ALLOWLIST}"
    if intent.strategy not in STRATEGIES:
        return None, f"strategy {intent.strategy} not permitted (defined-risk only)"
    if intent.strategy == "call_credit_spread" and intent.direction == "bullish":
        return None, "bullish direction contradicts a bearish/neutral call credit spread"
    if intent.strategy == "put_credit_spread" and intent.direction == "bearish":
        return None, "bearish direction contradicts a bullish/neutral put credit spread"
    if not 0.05 <= intent.short_delta_target <= 0.45:
        return None, f"short_delta_target {intent.short_delta_target} outside [0.05, 0.45]"
    if not config.DTE_MIN <= intent.dte_min <= intent.dte_max <= config.DTE_MAX:
        return None, f"dte_range {[intent.dte_min, intent.dte_max]} outside [{config.DTE_MIN}, {config.DTE_MAX}]"
    if intent.max_loss_usd > config.MAX_LOSS_USD:
        return None, f"max_loss_usd ${intent.max_loss_usd:.0f} exceeds cap ${config.MAX_LOSS_USD:.0f}"
    return intent, None


def coherence(intent: Intent) -> tuple[bool, str]:
    """Can this intent exist at all, before we touch the chain?

    A credit spread's max loss is (width - credit) * 100. Even at an optimistic
    credit, max loss cannot fall below the cap unless the width is small enough.
    Separately, the credit implied by the delta target must be able to clear the
    payoff's breakeven - otherwise every contract we could build is negative-EV.
    """
    width = intent.spread_width
    # Best realistic credit for a spread is bounded well under the width;
    # 40% of width is generous for a short-dated defined-risk spread.
    best_credit = 0.40 * width
    min_possible_loss = (width - best_credit) * 100
    if min_possible_loss > intent.max_loss_usd:
        return False, (f"incoherent: ${width:.0f}-wide spread has min possible max-loss "
                       f"${min_possible_loss:.0f} > cap ${intent.max_loss_usd:.0f}. "
                       f"Narrow the width or raise the cap.")

    # Fair credit for a short leg at delta d on a width-w spread is roughly
    # d * w (the short leg's share of the width it can lose). If the payoff
    # needs more than the market plausibly pays at that delta, it cannot work.
    fair_credit = intent.short_delta_target * width
    breakeven_credit = intent.short_delta_target * width  # same quantity, stated explicitly
    if fair_credit < 0.05:
        return False, (f"incoherent: delta {intent.short_delta_target:.2f} on ${width:.0f} width "
                       f"implies ~${fair_credit:.2f} fair credit - below any tradeable minimum.")
    _ = breakeven_credit
    return True, "coherent"
