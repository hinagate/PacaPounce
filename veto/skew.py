"""Skew-aware expected loss for put and call credit spreads.

A single flat volatility disagrees with the market about the shape of the left
tail, and it disagrees in the direction that flatters a premium seller. Priced
that way, a chain scan reliably "discovers" that far-OTM max-width spreads are
the best trades available - which is not a discovery, it is the model's own
error ranked first.

The volatility smile is the market's statement about both tails. This module
takes that statement seriously: the tail SHAPE comes from the market's
per-strike implied vols, and only the LEVEL is adjusted down to realised vol to
capture the variance risk premium.

The integration rests on an exact identity, so no distributional assumption
enters beyond the per-strike vols themselves:

    put:  E[min(max(Ks - S_T, 0), w)] = integral Kl..Ks P(S_T < x) dx
    call: E[min(max(S_T - Ks, 0), w)] = integral Ks..Kl P(S_T > x) dx

The relevant tail probability is read off the chain strike by strike, which is
where the smile lives.
"""
from __future__ import annotations

import math

SQRT2 = math.sqrt(2.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / SQRT2))


def implied_vol(spot: float, strike: float, T: float, price: float,
                is_put: bool = True) -> float | None:
    """Back the strike's own implied vol out of its observed mid price."""
    if spot <= 0 or strike <= 0 or T <= 0 or price <= 0:
        return None

    def theo(sig: float) -> float:
        if sig <= 0:
            return max(strike - spot, 0.0) if is_put else max(spot - strike, 0.0)
        d1 = (math.log(spot / strike) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
        d2 = d1 - sig * math.sqrt(T)
        if is_put:
            return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)

    lo, hi = 1e-4, 5.0
    if theo(hi) < price or theo(lo) > price:
        return None                      # outside arbitrage bounds: stale quote
    for _ in range(70):
        mid = (lo + hi) / 2
        if theo(mid) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def build_smile(spot: float, T: float, legs: list[dict],
                is_put: bool = True) -> dict[float, float]:
    """Map strike -> implied vol, from real quoted mids. This is the skew."""
    smile: dict[float, float] = {}
    for leg in legs:
        iv = leg.get("iv") or 0.0
        if iv <= 0:
            iv = implied_vol(spot, leg["strike"], T, leg["mid"], is_put=is_put) or 0.0
        if iv > 0:
            smile[leg["strike"]] = iv
    return smile


def _interp(smile: dict[float, float], strike: float) -> float:
    """Linear interpolation in strike, flat extrapolation past the wings.

    Flat rather than linear extrapolation on purpose: extending a steep skew
    beyond the quoted strikes invents tail vol the market never quoted.
    """
    if not smile:
        return 0.0
    ks = sorted(smile)
    if strike <= ks[0]:
        return smile[ks[0]]
    if strike >= ks[-1]:
        return smile[ks[-1]]
    for i in range(1, len(ks)):
        if strike <= ks[i]:
            k0, k1 = ks[i - 1], ks[i]
            w = (strike - k0) / (k1 - k0) if k1 > k0 else 0.0
            return smile[k0] * (1 - w) + smile[k1] * w
    return smile[ks[-1]]


def atm_vol(smile: dict[float, float], spot: float) -> float:
    """Implied vol at the money - the level the VRP ratio is measured against."""
    if not smile:
        return 0.0
    return smile[min(smile, key=lambda k: abs(k - spot))]


def prob_below(spot: float, strike: float, T: float, sigma: float,
               drift: float = 0.0) -> float:
    """P(S_T < strike) under the REAL-WORLD measure.

    drift is the expected annual log return of the underlying. Zero drift is not
    a neutral choice - it is an active bet against the equity risk premium, and
    it is what made at-the-money call spreads look like free money earlier. The
    ERP is the thing this strategy is paid for, so it belongs in the model
    explicitly where its sensitivity can be tested.
    """
    if sigma <= 0 or T <= 0:
        return 1.0 if strike > spot else 0.0
    d2 = (math.log(spot / strike) + (drift - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(-d2)


def expected_loss(spot: float, short_k: float, long_k: float, T: float,
                  smile: dict[float, float], vrp_ratio: float = 1.0,
                  steps: int = 400, drift: float = 0.0,
                  is_put: bool = True) -> float:
    """Expected loss per share, using the market's skew and a scaled vol level.

    vrp_ratio = realised_vol / atm_implied_vol.
      < 1  the market charges more vol than the underlying has been delivering
           - the premium a seller is paid for.
      = 1  no premium; expected loss equals what the chain implies and the trade
           is a coin flip minus friction.
    """
    lo, hi = min(short_k, long_k), max(short_k, long_k)
    if hi <= lo or T <= 0:
        intrinsic = (
            max(short_k - spot, 0.0)
            if is_put else max(spot - short_k, 0.0)
        )
        return min(intrinsic, hi - lo)
    dx = (hi - lo) / steps
    total = 0.0
    for i in range(steps):
        x = lo + (i + 0.5) * dx
        sigma = _interp(smile, x) * vrp_ratio
        p_below = prob_below(spot, x, T, sigma, drift)
        tail_probability = p_below if is_put else 1.0 - p_below
        total += tail_probability * dx
    return total


def diagnose(spot: float, smile: dict[float, float]) -> dict:
    """Summarise the smile so the skew is visible rather than implicit."""
    if not smile:
        return {}
    atm = atm_vol(smile, spot)
    wing_k = min(smile)
    return {
        "atm_iv": round(atm, 4),
        "wing_strike": wing_k,
        "wing_iv": round(smile[wing_k], 4),
        "skew_points": round((smile[wing_k] - atm) * 100, 2),
        "strikes": len(smile),
    }


def chain_is_sane(legs: list[dict], is_put: bool = True,
                  max_violation_rate: float = 0.02) -> tuple[bool, str]:
    """Reject a chain that violates no-arbitrage before pricing anything off it.

    Put value must be non-decreasing in strike (calls non-increasing). A chain
    that breaks this is stale or crossed, and every EV computed from it is
    fiction.

    This is not hypothetical: on 2026-08-25 around 10:30 ET the indicative feed
    quoted a $10-wide spread at $0.41 credit while a $5-wide spread on adjacent
    strikes paid $0.71. The gate scored 0/189 candidates tradeable. Forty minutes
    later the same code on a clean chain scored 84/84. Nothing changed but the
    data, and nothing in the system noticed.
    """
    rows = sorted(((l["strike"], l["mid"]) for l in legs if l.get("mid", 0) > 0))
    if len(rows) < 5:
        return False, f"only {len(rows)} usable strikes"
    violations = 0
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1][1], rows[i][1]
        if (is_put and cur < prev - 1e-9) or ((not is_put) and cur > prev + 1e-9):
            violations += 1
    rate = violations / (len(rows) - 1)
    if rate > max_violation_rate:
        return False, (f"{violations}/{len(rows) - 1} monotonicity violations "
                       f"({rate:.1%}) - chain is stale or crossed")
    return True, f"clean ({violations}/{len(rows) - 1} violations)"
