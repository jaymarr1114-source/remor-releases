"""Authoritative execution-safety policy for exponentiation (R16).

Single home for the magnitude cap, the _UnsafeMagnitude signal, and the
pre-materialization safety check.  Imported by:

- ``swarm_engine.primitives.families_pure``: the native ``power``
  primitive enforces the check before ``**``.  This is the authoritative
  enforcement point -- it covers every execution path that can reach the
  primitive (fast plan interpreter, composer batch/single execution,
  scalar fallbacks, pairwise probes, blind enumeration, production
  evaluation of admitted capabilities).
- ``swarm_engine.cognition.synthesis``: the search probes use
  ``_pow_safe_scalar`` as a cheap early-rejection filter so unsafe
  candidates are skipped without raising.

The invariant: no production-reachable execution path may attempt
``base ** exponent`` whose result would provably exceed ``_POW_MAG_CAP``.
Such values would OOM (or churn unboundedly) before any post-hoc
magnitude filter could reject them, so they are rejected *before* the
arithmetic is attempted.  Callers treat ``_UnsafeMagnitude`` like a
failed application (the candidate is skipped); it is an ``Exception``
subclass so existing generic handlers convert it to a skip.
"""
from __future__ import annotations

import math

# The single magnitude constant.  Do not duplicate it elsewhere; import
# this name.
_POW_MAG_CAP = 10 ** 18


class _UnsafeMagnitude(Exception):
    """Raised when evaluating a candidate would compute a value that
    provably exceeds the magnitude cap (e.g. power(2, 1e18)).

    The computation is skipped BEFORE it runs: such values would OOM
    before the post-hoc magnitude filter could reject them. Callers
    treat this like a failed application (the candidate is skipped).
    """


def _pow_safe_scalar(_base, _exp, _cap=_POW_MAG_CAP):
    """True iff power(_base, _exp) is safe to compute without exceeding _cap.

    Estimates digits via logarithms without computing the power, so
    pathological inputs (1e18**1e18) are rejected before they can OOM.
    Non-numeric inputs return True (the call itself may still fail, which
    callers already handle as a skip).
    """
    if not isinstance(_base, (int, float)) or not isinstance(_exp, (int, float)):
        return True
    if isinstance(_base, bool) or isinstance(_exp, bool):
        return True
    try:
        _ab = abs(_base)
        _ae = abs(_exp)
        if _ae == 0 or _ab <= 1:
            return True
        # digits(base**exp) ~= exp * log10(base)
        if _ae * math.log10(_ab) > math.log10(_cap):
            return False
    except (ValueError, OverflowError, ZeroDivisionError):
        return False
    return True


def _ensure_pow_safe(_base, _exp):
    """Authoritative enforcement: raise _UnsafeMagnitude unless
    power(_base, _exp) is safe to materialize.

    This is the single check every execution path funnels through (via
    the native ``power`` primitive).  It must stay pure and cheap: it is
    called on every power evaluation in the system.
    """
    if not _pow_safe_scalar(_base, _exp):
        raise _UnsafeMagnitude(
            f"power({_base!r}, {_exp!r}) would exceed magnitude cap {_POW_MAG_CAP}"
        )
