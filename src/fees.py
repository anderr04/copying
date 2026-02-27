"""
fees.py  –  Polymarket dynamic taker-fee model.

Polymarket charges a dynamic taker fee on binary-outcome markets.
The fee peaks near p = 0.50 (maximum uncertainty / liquidity) and
decays towards the extremes (p → 0 or p → 1).

Observed fee schedule (from real Polymarket trades)
----------------------------------------------------
    Price  $0.50       → 1.56 %    (0.0156)
    Price  $0.30/$0.70 → ~0.37 %   (0.0037)
    Price  $0.10/$0.90 → ~0.16 %   (0.0016)
    Price  $0.05/$0.95 → ~0.08 %   (0.0008)

Interpolation method
--------------------
We define  d = |price − 0.5|  (distance from maximum-fee point) and
fit a **cubic polynomial** through the four observed data points:

    fee(d) = 0.0156 − 0.1097·d + 0.3151·d² − 0.3210·d³

Derivation
~~~~~~~~~~
The system of 4 equations / 4 unknowns (a₀, a₁, a₂, a₃) is:

    d = 0.00  →  fee = 0.0156   →  a₀ = 0.0156
    d = 0.20  →  fee = 0.0037   →  0.20·a₁ + 0.04·a₂ + 0.008·a₃ = −0.0119
    d = 0.40  →  fee = 0.0016   →  0.40·a₁ + 0.16·a₂ + 0.064·a₃ = −0.0140
    d = 0.45  →  fee = 0.0008   →  0.45·a₁ + 0.2025·a₂ + 0.091·a₃ = −0.0148

Solving gives:  a₁ ≈ −0.1097,  a₂ ≈ +0.3151,  a₃ ≈ −0.3210.

The polynomial is symmetric around p = 0.50 by construction (we use
|p − 0.5|) and is clamped to a floor of 0.01 % to avoid numerical
artefacts at the tails.
"""

from __future__ import annotations


# ── Polynomial coefficients (exact fit to Polymarket schedule) ───────
_A0: float =  0.0156
_A1: float = -0.1097
_A2: float =  0.3151
_A3: float = -0.3210

_FEE_FLOOR: float = 0.0001   # 0.01 % – safety floor


def calculate_dynamic_fee(price: float) -> float:
    """
    Return the effective taker-fee rate for a Polymarket binary contract.

    Parameters
    ----------
    price : float
        The execution price of the contract (0 < price < 1).

    Returns
    -------
    float
        Fee rate as a fraction of trade value (e.g. 0.0156 ≡ 1.56 %).

    Examples
    --------
    >>> round(calculate_dynamic_fee(0.50), 4)
    0.0156
    >>> round(calculate_dynamic_fee(0.30), 4)
    0.0037
    >>> round(calculate_dynamic_fee(0.10), 4)
    0.0016
    >>> round(calculate_dynamic_fee(0.05), 4)
    0.0008
    """
    price = max(0.01, min(price, 0.99))     # clamp to safe range
    d = abs(price - 0.5)
    fee = _A0 + _A1 * d + _A2 * d ** 2 + _A3 * d ** 3
    return max(fee, _FEE_FLOOR)
