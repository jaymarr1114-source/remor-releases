"""
swarm_engine/acquisition/algorithmic.py

Algorithmic (loop-based) candidate synthesis.

`strategies.SynthesizingSource` only ever produced single-expression
skeletons and two-stage compositions of them — `return a0[::-1]`,
`return sorted(a0)`. That is enough for transformations expressible as one
expression, and it is structurally incapable of prime factorization, a
primality test, iterative digit manipulation, or anything else that needs a
loop and accumulated state. `acquire_capability("prime_factorization", ...)`
failing was not a missing special case, it was this ceiling.

This module is a second, broader hypothesis source: a curated library of
parameterized algorithm skeletons that *do* contain loops and local state —
trial-division factorization, Euclid's algorithm, iterative Fibonacci, run-
length encoding, linear and binary search, and so on. It is still a library of
templates, not open-ended program synthesis, and that is stated rather than
implied. What makes it a genuine widening rather than a special case for the
observed failure is that every template is offered for every acquisition
alongside the expression skeletons, unconditionally — nothing here inspects
the goal name or description to decide which template to try. `prime` never
appears in a string-match anywhere in this module or its caller; the trial-
division template is simply one of ~20 candidates tried on every acquisition,
and it is independent verification against the caller's examples that decides
whether it happens to be the one that fits.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from swarm_engine.acquisition.pipeline import Candidate
from swarm_engine.acquisition.strategies import CapabilitySpec

# Each template takes the argument names it needs (by position: a0, a1, ...)
# and is only offered when the specification has at least that many inputs.
# `arity` documents the requirement rather than leaving it implicit in the
# template body, so a spec with the wrong shape is skipped rather than
# generating code that cannot possibly run.
_TEMPLATES: List[Tuple[str, int, str]] = [
    ("trial_division_factors", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 2:\n"
     "        return []\n"
     "    factors = []\n"
     "    d = 2\n"
     "    while d * d <= n:\n"
     "        while n % d == 0:\n"
     "            factors.append(d)\n"
     "            n //= d\n"
     "        d += 1\n"
     "    if n > 1:\n"
     "        factors.append(n)\n"
     "    return factors\n"),

    ("is_prime_trial", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 2:\n"
     "        return False\n"
     "    d = 2\n"
     "    while d * d <= n:\n"
     "        if n % d == 0:\n"
     "            return False\n"
     "        d += 1\n"
     "    return True\n"),

    ("count_divisors", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 1:\n"
     "        return 0\n"
     "    count = 0\n"
     "    d = 1\n"
     "    while d <= n:\n"
     "        if n % d == 0:\n"
     "            count += 1\n"
     "        d += 1\n"
     "    return count\n"),

    ("digit_sum", 1,
     "def capability({args}):\n"
     "    n = abs(int({a0}))\n"
     "    total = 0\n"
     "    while n > 0:\n"
     "        total += n % 10\n"
     "        n //= 10\n"
     "    return total\n"),

    ("digit_reverse", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    sign = -1 if n < 0 else 1\n"
     "    n = abs(n)\n"
     "    result = 0\n"
     "    while n > 0:\n"
     "        result = result * 10 + n % 10\n"
     "        n //= 10\n"
     "    return sign * result\n"),

    ("is_palindrome_number", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 0:\n"
     "        return False\n"
     "    original, reversed_n, rest = n, 0, n\n"
     "    while rest > 0:\n"
     "        reversed_n = reversed_n * 10 + rest % 10\n"
     "        rest //= 10\n"
     "    return original == reversed_n\n"),

    ("collatz_length", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 1:\n"
     "        raise ValueError('undefined for n < 1')\n"
     "    steps = 0\n"
     "    while n != 1:\n"
     "        n = n // 2 if n % 2 == 0 else 3 * n + 1\n"
     "        steps += 1\n"
     "    return steps\n"),

    ("factorial_iterative", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 0:\n"
     "        raise ValueError('undefined for negative n')\n"
     "    result = 1\n"
     "    k = 2\n"
     "    while k <= n:\n"
     "        result *= k\n"
     "        k += 1\n"
     "    return result\n"),

    ("fibonacci_nth", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n < 0:\n"
     "        raise ValueError('undefined for negative n')\n"
     "    a, b = 0, 1\n"
     "    k = 0\n"
     "    while k < n:\n"
     "        a, b = b, a + b\n"
     "        k += 1\n"
     "    return a\n"),

    ("sum_of_digits_squared", 1,
     "def capability({args}):\n"
     "    n = abs(int({a0}))\n"
     "    total = 0\n"
     "    while n > 0:\n"
     "        total += (n % 10) ** 2\n"
     "        n //= 10\n"
     "    return total\n"),

    ("binary_representation", 1,
     "def capability({args}):\n"
     "    n = int({a0})\n"
     "    if n == 0:\n"
     "        return '0'\n"
     "    negative = n < 0\n"
     "    n = abs(n)\n"
     "    bits = ''\n"
     "    while n > 0:\n"
     "        bits = str(n % 2) + bits\n"
     "        n //= 2\n"
     "    return ('-' if negative else '') + bits\n"),

    ("gcd_euclid", 2,
     "def capability({args}):\n"
     "    a, b = int({a0}), int({a1})\n"
     "    while b:\n"
     "        a, b = b, a % b\n"
     "    return abs(a)\n"),

    ("lcm_two", 2,
     "def capability({args}):\n"
     "    a, b = int({a0}), int({a1})\n"
     "    orig_a, orig_b = a, b\n"
     "    while b:\n"
     "        a, b = b, a % b\n"
     "    gcd = abs(a) or 1\n"
     "    return abs(orig_a * orig_b) // gcd\n"),

    ("power_iterative", 2,
     "def capability({args}):\n"
     "    base, exponent = {a0}, int({a1})\n"
     "    if exponent < 0:\n"
     "        raise ValueError('undefined for negative exponent')\n"
     "    result = 1\n"
     "    k = 0\n"
     "    while k < exponent:\n"
     "        result *= base\n"
     "        k += 1\n"
     "    return result\n"),

    ("is_palindrome_text", 1,
     "def capability({args}):\n"
     "    s = {a0}\n"
     "    i, j = 0, len(s) - 1\n"
     "    while i < j:\n"
     "        if s[i] != s[j]:\n"
     "            return False\n"
     "        i += 1\n"
     "        j -= 1\n"
     "    return True\n"),

    ("run_length_encode", 1,
     "def capability({args}):\n"
     "    s = {a0}\n"
     "    if not s:\n"
     "        return ''\n"
     "    out = []\n"
     "    i = 0\n"
     "    while i < len(s):\n"
     "        j = i\n"
     "        while j < len(s) and s[j] == s[i]:\n"
     "            j += 1\n"
     "        out.append(s[i] + str(j - i))\n"
     "        i = j\n"
     "    return ''.join(out)\n"),

    ("count_vowels", 1,
     "def capability({args}):\n"
     "    s = {a0}\n"
     "    vowels = set('aeiouAEIOU')\n"
     "    count = 0\n"
     "    i = 0\n"
     "    while i < len(s):\n"
     "        if s[i] in vowels:\n"
     "            count += 1\n"
     "        i += 1\n"
     "    return count\n"),

    ("bubble_sort", 1,
     "def capability({args}):\n"
     "    values = list({a0})\n"
     "    n = len(values)\n"
     "    i = 0\n"
     "    while i < n:\n"
     "        j = 0\n"
     "        while j < n - i - 1:\n"
     "            if values[j] > values[j + 1]:\n"
     "                values[j], values[j + 1] = values[j + 1], values[j]\n"
     "            j += 1\n"
     "        i += 1\n"
     "    return values\n"),

    ("linear_search_first_duplicate", 1,
     "def capability({args}):\n"
     "    values = list({a0})\n"
     "    seen = []\n"
     "    i = 0\n"
     "    while i < len(values):\n"
     "        j = 0\n"
     "        while j < len(seen):\n"
     "            if seen[j] == values[i]:\n"
     "                return values[i]\n"
     "            j += 1\n"
     "        seen.append(values[i])\n"
     "        i += 1\n"
     "    return None\n"),

    ("running_sum", 1,
     "def capability({args}):\n"
     "    values = list({a0})\n"
     "    out = []\n"
     "    total = 0\n"
     "    i = 0\n"
     "    while i < len(values):\n"
     "        total += values[i]\n"
     "        out.append(total)\n"
     "        i += 1\n"
     "    return out\n"),

    ("is_sorted_ascending", 1,
     "def capability({args}):\n"
     "    values = list({a0})\n"
     "    i = 1\n"
     "    while i < len(values):\n"
     "        if values[i] < values[i - 1]:\n"
     "            return False\n"
     "        i += 1\n"
     "    return True\n"),

    ("remove_consecutive_duplicates", 1,
     "def capability({args}):\n"
     "    values = list({a0})\n"
     "    if not values:\n"
     "        return []\n"
     "    out = [values[0]]\n"
     "    i = 1\n"
     "    while i < len(values):\n"
     "        if values[i] != out[-1]:\n"
     "            out.append(values[i])\n"
     "        i += 1\n"
     "    return out\n"),
]


class AlgorithmicSynthesizer:
    """Generates loop-based candidates from a specification.

    A sibling to `SynthesizingSource`, offering a materially different class
    of hypothesis (control flow and accumulated state) rather than variations
    on the same expression-composition idea.
    """

    def generate(self, spec: CapabilitySpec) -> List[Candidate]:
        args = spec.input_names or ["value"]
        arg_list = ", ".join(args)
        candidates: List[Candidate] = []

        for name, arity, template in _TEMPLATES:
            if len(args) < arity:
                continue
            substitutions = {"args": arg_list}
            for index in range(arity):
                substitutions[f"a{index}"] = args[index]
            try:
                code = template.format(**substitutions)
            except (KeyError, IndexError):
                continue
            candidates.append(Candidate(
                name=f"algo_{spec.name}_{name}", source="synthesized:algorithmic",
                code=code, notes=f"algorithm {name}"))
        return candidates
