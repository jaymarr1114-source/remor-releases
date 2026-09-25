"""General example-driven body synthesis for project continuation.

Policy substrate for PROJECT_CONTINUATION_POLICY >= 45.

Searches a small, project-agnostic operator space against mined I/O examples.
No hardcoded project names, preferred symbols, or task-specific winners.
Import paths for reuse are derived from the live project inventory.
"""
from __future__ import annotations

import ast
import os
import re
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple


# --- Primitive unary transforms (generic; not project-specific) ---------------

def _prims() -> List[Tuple[str, str, Callable[[Any], Any]]]:
    """(label, expr_using_x, fn). expr uses free name `x`."""
    return [
        ("id", "x", lambda x: x),
        ("strip", "x.strip()", lambda x: x.strip()),
        ("lower", "x.lower()", lambda x: x.lower()),
        ("upper", "x.upper()", lambda x: x.upper()),
        ("reverse", "x[::-1]", lambda x: x[::-1]),
        ("split", "x.split()", lambda x: x.split()),
        ("strip_split", "x.strip().split()", lambda x: x.strip().split()),
        ("join_split", "''.join(x.split())", lambda x: "".join(x.split())),
        ("sorted_chars", "''.join(sorted(x))", lambda x: "".join(sorted(x))),
        ("sorted_tokens", "sorted(x.split())", lambda x: sorted(x.split())),
        ("len", "len(x)", lambda x: len(x)),
        ("list", "list(x)", lambda x: list(x)),
        ("set_list", "list(dict.fromkeys(x.split()))",
         lambda x: list(dict.fromkeys(x.split()))),
    ]


def _compose_expr(exprs: List[str]) -> str:
    """Compose exprs where each uses `x`; nest rightward."""
    out = "x"
    for e in exprs:
        out = e.replace("x", f"({out})") if out != "x" else e
        # When nesting, replace free x carefully: use sequential subst
    # Rebuild properly:
    out = exprs[0]
    for e in exprs[1:]:
        # substitute x in e with (out)
        out = re.sub(r"\bx\b", f"({out})", e)
    return out


def _compose_fn(fns: List[Callable]) -> Callable:
    def wrapped(v, _fns=tuple(fns)):
        for f in _fns:
            v = f(v)
        return v
    return wrapped


def _chain_candidates(max_depth: int = 3
                      ) -> List[Tuple[str, str, Callable]]:
    prims = _prims()
    out: List[Tuple[str, str, Callable]] = []
    # depth 1
    for lab, expr, fn in prims:
        out.append((lab, f"return {expr}", fn))
    # depth 2-3 (skip id-leading to reduce waste; allow reverse/strip/lower combos)
    useful = [p for p in prims if p[0] != "id"]
    from itertools import product
    for depth in range(2, max_depth + 1):
        for combo in product(useful, repeat=depth):
            labels = [c[0] for c in combo]
            # prune obvious no-ops / duplicates
            if len(set(labels)) < len(labels) and "reverse" not in labels:
                continue
            if labels.count("split") + labels.count("strip_split") > 1:
                continue
            exprs = [c[1] for c in combo]
            fns = [c[2] for c in combo]
            expr = _compose_expr(exprs)
            lab = "+".join(labels)
            out.append((lab, f"return {expr}", _compose_fn(fns)))
    # Counter wraps over string→tokens chains
    token_chains = [
        ("split", "x.split()", lambda x: x.split()),
        ("strip_split", "x.strip().split()", lambda x: x.strip().split()),
        ("lower_split", "x.lower().split()", lambda x: x.lower().split()),
        ("strip_lower_split", "x.strip().lower().split()",
         lambda x: x.strip().lower().split()),
        ("reverse_strip_split", "x.strip()[::-1].split()",
         lambda x: x.strip()[::-1].split()),
        ("strip_reverse_split", "x.strip()[::-1].split()",
         lambda x: x.strip()[::-1].split()),
        ("join_split_as_tokens", "list(''.join(x.split()))",
         lambda x: list("".join(x.split()))),
    ]
    for lab, expr, fn in token_chains:
        out.append((
            f"counter:{lab}",
            f"from collections import Counter\nreturn dict(Counter({expr}))",
            (lambda t, f=fn: dict(Counter(f(t)))),
        ))
    # Frequency / report field helpers (generic; not symbol-specific)
    def _top(toks):
        if not toks:
            return None
        ct = dict(Counter(toks))
        mx = max(ct.values())
        return min(k for k, v in ct.items() if v == mx)

    field_helpers = [
        ("n_tokens_split", "len(x.split())", lambda x: len(x.split())),
        ("n_tokens_strip_split", "len(x.strip().split())",
         lambda x: len(x.strip().split())),
        ("n_unique_split", "len(list(dict.fromkeys(x.split())))",
         lambda x: len(list(dict.fromkeys(x.split())))),
        ("n_unique_strip_split", "len(list(dict.fromkeys(x.strip().split())))",
         lambda x: len(list(dict.fromkeys(x.strip().split())))),
        ("n_tokens_strip_lower_split", "len(x.strip().lower().split())",
         lambda x: len(x.strip().lower().split())),
        ("n_unique_strip_lower_split",
         "len(list(dict.fromkeys(x.strip().lower().split())))",
         lambda x: len(list(dict.fromkeys(x.strip().lower().split())))),
        ("top_token_split",
         "(lambda ct: (min((k for k, v in ct.items() if v == max(ct.values())), "
         "default=None) if ct else None))(dict(Counter(x.split())))",
         lambda x: _top(x.split())),
        ("top_token_strip_split",
         "(lambda ct: (min((k for k, v in ct.items() if v == max(ct.values())), "
         "default=None) if ct else None))(dict(Counter(x.strip().split())))",
         lambda x: _top(x.strip().split())),
        ("top_token_strip_lower_split",
         "(lambda ct: (min((k for k, v in ct.items() if v == max(ct.values())), "
         "default=None) if ct else None))"
         "(dict(Counter(x.strip().lower().split())))",
         lambda x: _top(x.strip().lower().split())),
        ("cleaned_strip_lower", "x.strip().lower()",
         lambda x: x.strip().lower()),
        ("cleaned_strip_reverse", "x.strip()[::-1]",
         lambda x: x.strip()[::-1]),
        ("cleaned_lower_strip", "x.lower().strip()",
         lambda x: x.lower().strip()),
    ]
    for lab, expr, fn in field_helpers:
        body = f"return {expr}"
        if "Counter" in expr:
            body = "from collections import Counter\n" + body
        out.append((lab, body, fn))
    return out


def discover_top_packages(root: str) -> List[str]:
    pkgs = []
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if (os.path.isdir(p) and not name.startswith(".")
                and name not in ("tests", "__pycache__", "work")
                and os.path.isfile(os.path.join(p, "__init__.py"))):
            pkgs.append(name)
    return pkgs


def module_import_path(root: str, rel_path: str) -> Optional[str]:
    """textkit/normalize.py -> textkit.normalize"""
    if not rel_path.endswith(".py"):
        return None
    parts = rel_path.replace("\\", "/").split("/")
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1]) or None
    parts[-1] = parts[-1][:-3]
    return ".".join(parts)


def locate_import_for_symbol(root: str, symbol: str, inventory: List[str]
                             ) -> Optional[Tuple[str, str]]:
    """Return (import_module, symbol) for a defined function."""
    for rel in inventory:
        if not rel.endswith(".py") or rel.startswith("tests/"):
            continue
        full = os.path.join(root, rel)
        try:
            tree = ast.parse(open(full, encoding="utf-8").read())
        except Exception:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == symbol:
                mod = module_import_path(root, rel)
                if mod:
                    return mod, symbol
    return None


def _match_expect(got: Any, expect: Any) -> bool:
    if isinstance(expect, dict) and isinstance(got, dict):
        return all(got.get(k) == v for k, v in expect.items())
    return got == expect


def _via_candidates(
    available: Dict[str, Callable],
    root: str,
    inventory: List[str],
) -> List[Tuple[str, str, Callable]]:
    """Reuse completed callables via discovered import paths + light post-ops."""
    out: List[Tuple[str, str, Callable]] = []
    for name, fn in available.items():
        loc = locate_import_for_symbol(root, name, inventory)
        if not loc:
            continue
        mod, sym = loc
        imp = f"from {mod} import {sym}"
        out.append((f"via:{name}", f"{imp}\nreturn {sym}(x)",
                    (lambda t, f=fn: f(t))))
        # light post-ops
        def _top(toks):
            if not toks:
                return None
            ct = dict(Counter(toks))
            mx = max(ct.values())
            return min(k for k, v in ct.items() if v == mx)

        posts = [
            ("split", "return {call}.split()", lambda t, f=fn: f(t).split()),
            ("strip", "return {call}.strip()", lambda t, f=fn: f(t).strip()),
            ("lower", "return {call}.lower()", lambda t, f=fn: f(t).lower()),
            ("reverse", "return {call}[::-1]", lambda t, f=fn: f(t)[::-1]),
            ("len_split", "return len({call}.split())",
             lambda t, f=fn: len(f(t).split())),
            ("nunique_split",
             "return len(list(dict.fromkeys({call}.split())))",
             lambda t, f=fn: len(list(dict.fromkeys(f(t).split())))),
            ("top_split",
             "from collections import Counter\n"
             "return (lambda ct: (min((k for k, v in ct.items() if v == max(ct.values())), "
             "default=None) if ct else None))(dict(Counter({call}.split())))",
             lambda t, f=fn: _top(f(t).split())),
            ("counter_split",
             "from collections import Counter\nreturn dict(Counter({call}.split()))",
             lambda t, f=fn: dict(Counter(f(t).split()))),
            ("counter",
             "from collections import Counter\nreturn dict(Counter({call}))",
             lambda t, f=fn: dict(Counter(f(t)))),
        ]
        call = f"{sym}(x)"
        for plab, pbody, pfn in posts:
            body = pbody.format(call=call)
            if not body.startswith("from collections"):
                body = f"{imp}\n{body}"
            else:
                body = f"{imp}\n{body}"
            out.append((f"via:{name}+{plab}", body, pfn))
    return out


def _dict_report_candidates(
    examples: List[Tuple[Any, Any]],
    available: Dict[str, Callable],
    root: str,
    inventory: List[str],
) -> List[Tuple[str, str, Callable]]:
    """When expects are dicts, synthesize a report body key-by-key."""
    if not examples or not all(isinstance(e, dict) for _, e in examples):
        return []
    keys = sorted(set().union(*(e.keys() for _, e in examples)))
    if len(keys) < 2:
        return []

    # Build a pool of (lab, expr_using_x, fn) for scalar/list/dict field values
    pool: List[Tuple[str, str, Callable]] = []
    for lab, body, fn in _chain_candidates(max_depth=2):
        # body is "return EXPR" or multi-line; extract expr
        lines = [ln for ln in body.split("\n") if ln.strip() and not ln.strip().startswith("from ")]
        if len(lines) == 1 and lines[0].startswith("return "):
            expr = lines[0][len("return "):]
            pool.append((lab, expr, fn))
    for lab, body, fn in _via_candidates(available, root, inventory):
        lines = [ln for ln in body.split("\n") if ln.strip() and not ln.strip().startswith("from ")]
        if len(lines) == 1 and lines[0].startswith("return "):
            expr = lines[0][len("return "):]
            # rewrite param x -> keep; via bodies use symbol(x)
            pool.append((lab, expr, fn))

    # Special field helpers using available
    def _top_of_counter(ct: dict):
        if not ct:
            return None
        mx = max(ct.values())
        return min(k for k, v in ct.items() if v == mx)

    for name, fn in available.items():
        loc = locate_import_for_symbol(root, name, inventory)
        if not loc:
            continue
        mod, sym = loc
        pool.append((f"avail:{name}", f"{sym}(x)", fn))
        pool.append((f"len_avail:{name}", f"len({sym}(x))",
                     (lambda t, f=fn: len(f(t)))))
        pool.append((f"len_keys_avail:{name}", f"len({sym}(x))",
                     (lambda t, f=fn: len(f(t)))))

    # Also Counter-of-available for top
    for name, fn in list(available.items()):
        loc = locate_import_for_symbol(root, name, inventory)
        if not loc:
            continue
        mod, sym = loc
        def _ct(t, f=fn):
            v = f(t)
            if isinstance(v, dict):
                return v
            return dict(Counter(v))
        pool.append((f"as_counter:{name}",
                     f"(dict(Counter({sym}(x))) if not isinstance({sym}(x), dict) else {sym}(x))",
                     _ct))
        pool.append((f"top_of:{name}",
                     f"(lambda ct: (min((k for k,v in ct.items() if v==max(ct.values())), default=None) if ct else None))(dict(Counter({sym}(x))) if not isinstance({sym}(x), dict) else {sym}(x))",
                     (lambda t, f=fn: _top_of_counter(
                         f(t) if isinstance(f(t), dict) else dict(Counter(f(t)))))))

    # Per-key winning expr
    key_choice: Dict[str, Tuple[str, str, Callable]] = {}
    for key in keys:
        survivors = []
        keyed = [(a, e) for a, e in examples if isinstance(e, dict) and key in e]
        if not keyed:
            return []
        for lab, expr, fn in pool:
            ok = True
            for arg, expect in keyed:
                try:
                    if fn(arg) != expect[key]:
                        ok = False
                        break
                except Exception:
                    ok = False
                    break
            if ok:
                survivors.append((lab, expr, fn))
        if not survivors:
            return []
        survivors.sort(key=lambda t: (len(t[1]), t[0]))
        key_choice[key] = survivors[0]

    # Build imports + body
    imports = set()
    for lab, expr, fn in key_choice.values():
        for name in available:
            loc = locate_import_for_symbol(root, name, inventory)
            if loc and (name in expr or loc[1] in expr):
                imports.add(f"from {loc[0]} import {loc[1]}")
        if "Counter" in expr:
            imports.add("from collections import Counter")

    # Need Counter import if any top_of uses it
    for lab, expr, _ in key_choice.values():
        if "Counter" in expr or "top_of" in lab or "as_counter" in lab:
            imports.add("from collections import Counter")

    dict_items = ", ".join(f'"{k}": {key_choice[k][1]}' for k in keys)
    body_lines = list(sorted(imports)) + [f"return {{{dict_items}}}"]
    body = "\n".join(body_lines)

    def _report_fn(t, _choices=dict(key_choice)):
        return {k: fn(t) for k, (_, _, fn) in _choices.items()}

    label = "dict_report:" + "+".join(f"{k}={key_choice[k][0]}" for k in keys)
    return [(label, body, _report_fn)]


def synthesize_body(
    examples: List[Tuple[Any, Any]],
    available: Optional[Dict[str, Callable]] = None,
    root: str = "",
    inventory: Optional[List[str]] = None,
    param_name: str = "text",
) -> Dict[str, Any]:
    """Search general candidates; return selection dict compatible with continuation."""
    available = available or {}
    inventory = inventory or []
    if not examples:
        return {"selected": None, "n_candidates": 0, "survivors": [], "rejected": []}

    cands: List[Tuple[str, str, Callable]] = []
    cands.extend(_chain_candidates(max_depth=3))
    if root and inventory:
        cands.extend(_via_candidates(available, root, inventory))
    cands.extend(_dict_report_candidates(examples, available, root, inventory))

    # Rewrite bodies to use param_name instead of free `x` where needed
    def _reparam(body: str) -> str:
        # Don't touch import lines' dots; only replace free x as the param
        lines = []
        for ln in body.split("\n"):
            if ln.strip().startswith("from ") or ln.strip().startswith("import "):
                lines.append(ln)
            else:
                lines.append(re.sub(r"\bx\b", param_name, ln))
        return "\n".join(lines)

    survivors, rejected = [], []
    seen_labels = set()
    for lab, body, fn in cands:
        if lab in seen_labels:
            continue
        seen_labels.add(lab)
        ok = True
        for arg, expect in examples:
            try:
                got = fn(arg)
                if not _match_expect(got, expect):
                    ok = False
                    break
            except Exception:
                ok = False
                break
        if ok:
            survivors.append((lab, _reparam(body)))
        else:
            rejected.append(lab)

    if not survivors:
        return {"selected": None, "n_candidates": len(seen_labels),
                "survivors": [], "rejected": rejected[:40]}
    # Prefer shorter bodies, then shorter labels (Occam)
    survivors.sort(key=lambda x: (len(x[1]), len(x[0]), x[0]))
    lab, body = survivors[0]
    return {
        "selected": lab,
        "body": body,
        "n_candidates": len(seen_labels),
        "survivors": [s[0] for s in survivors[:20]],
        "rejected": rejected[:40],
        "param_name": param_name,
    }


def render_function(symbol: str, body: str, param_name: str = "text") -> str:
    imports, code = [], []
    for ln in body.split("\n"):
        st = ln.strip()
        if st.startswith("from ") or st.startswith("import "):
            imports.append(st)
        else:
            code.append(st)
    lines = ['"""Repaired/implemented by project continuation body synthesis."""', ""]
    for imp in dict.fromkeys(imports):
        lines.append(imp)
    if imports:
        lines.append("")
    lines.append(f"def {symbol}({param_name}):")
    if not code:
        lines.append("    pass")
    else:
        for c in code:
            lines.append("    " + c if c else "")
    lines.append("")
    return "\n".join(lines)


def mine_examples_for_symbol(root: str, symbol: str) -> List[Tuple[Any, Any]]:
    """Mine assert symbol(arg)==expect and dict-field expects for any symbol."""
    examples: List[Tuple[Any, Any]] = []
    tests = os.path.join(root, "tests")
    if not os.path.isdir(tests):
        return examples
    for dp, dns, fns in os.walk(tests):
        dns[:] = [d for d in dns if d != "heldout"]
        for fn in fns:
            if not (fn.startswith("test_eng") and fn.endswith(".py")):
                continue
            src = open(os.path.join(dp, fn), encoding="utf-8").read()
            for m in re.finditer(
                    rf"assert\s+{re.escape(symbol)}\((.+?)\)\s*==\s*(.+)$",
                    src, re.M):
                try:
                    examples.append((ast.literal_eval(m.group(1).strip()),
                                     ast.literal_eval(m.group(2).strip())))
                except Exception:
                    pass
            # dict-field form: s = symbol(...); assert s["k"] == ...
            for part in re.split(r"\ndef\s+", src):
                m = re.search(rf"{re.escape(symbol)}\((['\"].*?['\"]|\d+)\)", part)
                if not m:
                    continue
                try:
                    arg = ast.literal_eval(m.group(1))
                except Exception:
                    continue
                expect: Dict[str, Any] = {}
                for am in re.finditer(
                        r"""(?:s|result|out|got)\[['\"](\w+)['\"]\]\s*==\s*(.+)$""",
                        part, re.M):
                    try:
                        expect[am.group(1)] = ast.literal_eval(am.group(2).strip())
                    except Exception:
                        pass
                if len(expect) >= 2:
                    examples.append((arg, expect))
    seen, out = set(), []
    for a, e in examples:
        k = (repr(a), repr(e))
        if k not in seen:
            seen.add(k)
            out.append((a, e))
    return out


__all__ = [
    "synthesize_body",
    "render_function",
    "mine_examples_for_symbol",
    "discover_top_packages",
    "module_import_path",
    "locate_import_for_symbol",
]
