"""Generic discrete table lookup primitive — relation tables are data."""
from typing import Any, Dict, Optional


def _key_candidates(key: Any) -> list:
    """Generate equivalent lookup keys without collapsing distinct string ids.

    Handles JSON/plan hydration where numeric keys become strings ("1") while
    runtime args remain ints (1). Legitimate non-numeric strings are unchanged.
    """
    out = [key]
    if isinstance(key, str):
        out.append(key.lower())
        # numeric string ↔ number
        try:
            if key.isdigit() or (key.startswith("-") and key[1:].isdigit()):
                out.append(int(key))
            else:
                f = float(key)
                if f.is_integer():
                    out.append(int(f))
                out.append(f)
        except (ValueError, TypeError):
            pass
    elif isinstance(key, bool):
        pass  # do not coerce bool to int
    elif isinstance(key, int):
        out.append(str(key))
    elif isinstance(key, float) and key.is_integer():
        out.append(int(key))
        out.append(str(int(key)))
    # dedupe preserving order
    seen = set()
    uniq = []
    for k in out:
        marker = (type(k).__name__, k)
        if marker not in seen:
            seen.add(marker)
            uniq.append(k)
    return uniq


def discrete_lookup(table: Dict[Any, Any], key: Any) -> Optional[Any]:
    if not isinstance(table, dict):
        return None
    for k in _key_candidates(key):
        if k in table:
            return table[k]
    # also try candidate keys against stringified table keys
    for tk, tv in table.items():
        for k in _key_candidates(key):
            if tk == k:
                return tv
            if isinstance(tk, str) and isinstance(k, (int, float)) and not isinstance(k, bool):
                try:
                    if float(tk) == float(k):
                        return tv
                except (ValueError, TypeError):
                    pass
            if isinstance(k, str) and isinstance(tk, (int, float)) and not isinstance(tk, bool):
                try:
                    if float(k) == float(tk):
                        return tv
                except (ValueError, TypeError):
                    pass
    return None
