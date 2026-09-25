"""Isolated subprocess code runner for the Tester protocol.

run_code(code, entrypoint, cases) executes the code in a fresh `python3`
subprocess (30s timeout, temp dir, JSON over stdio). The entrypoint is
called as entrypoint(**case_args) for each case.

Returns RunReport(ok, value, error):
- ok=True, value=[{"ok": bool, "value": <JSON>, "error": str}, ...]
- ok=False, value=[], error=<driver-visible reason>

The report object exposes .ok/.value/.error attributes because the real
Tester implementation (swarm_engine.verification.independent) reads those
attributes; it also supports tuple unpacking for convenience.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

TIMEOUT_S = 30


@dataclass
class RunReport:
    ok: bool
    value: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def __iter__(self):
        return iter((self.ok, self.value, self.error))

    def __len__(self):
        return 3

    def __getitem__(self, index):
        return (self.ok, self.value, self.error)[index]


_SHIM = textwrap.dedent('''
    import json, sys, traceback

    def _main():
        with open(sys.argv[1], "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        code_path = payload["code_path"]
        entrypoint = payload["entrypoint"]
        cases = payload["cases"]
        namespace = {"__name__": "candidate_module"}
        with open(code_path, "r", encoding="utf-8") as fh:
            code_text = fh.read()
        try:
            compile(code_text, code_path, "exec")
        except SyntaxError as exc:
            print(json.dumps({"fatal": "syntax: %s" % exc}))
            return
        try:
            exec(compile(code_text, code_path, "exec"), namespace)
        except Exception:
            print(json.dumps({"fatal": "import: %s" % traceback.format_exc(limit=3)}))
            return
        fn = namespace.get(entrypoint)
        if not callable(fn):
            print(json.dumps({"fatal": "no callable entrypoint %r" % entrypoint}))
            return
        results = []
        for case_args in cases:
            try:
                value = fn(**case_args)
            except Exception as exc:
                results.append({"ok": False, "value": None,
                                "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError) as exc:
                results.append({"ok": False, "value": None,
                                "error": "non-JSON-serializable return: %s" % exc})
                continue
            results.append({"ok": True, "value": value, "error": ""})
        print(json.dumps({"results": results}))

    _main()
''')


def _case_args(case: Any) -> Dict[str, Any]:
    if hasattr(case, "args"):
        return dict(case.args)
    if isinstance(case, dict):
        return dict(case)
    raise TypeError(f"case must be a Case or dict, got {type(case).__name__}")


def run_code(code: str, entrypoint: str,
             cases: Sequence[Any]) -> RunReport:
    """Run code under isolation. Matches the Tester protocol."""
    arg_list = [_case_args(c) for c in cases]
    with tempfile.TemporaryDirectory(prefix="ao_run_") as tmp:
        code_path = os.path.join(tmp, "candidate_module.py")
        shim_path = os.path.join(tmp, "shim.py")
        payload_path = os.path.join(tmp, "payload.json")
        with open(code_path, "w", encoding="utf-8") as fh:
            fh.write(code)
        with open(shim_path, "w", encoding="utf-8") as fh:
            fh.write(_SHIM)
        with open(payload_path, "w", encoding="utf-8") as fh:
            json.dump({"code_path": code_path, "entrypoint": entrypoint,
                       "cases": arg_list}, fh)
        try:
            proc = subprocess.run(
                [sys.executable, shim_path, payload_path],
                capture_output=True, text=True, timeout=TIMEOUT_S,
                cwd=tmp)
        except subprocess.TimeoutExpired:
            return RunReport(False, [], "subprocess timed out "
                             f"after {TIMEOUT_S}s (killed)")
        except Exception as exc:
            return RunReport(False, [],
                             f"could not launch subprocess: {exc}")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-500:]
        return RunReport(False, [],
                         f"subprocess exited {proc.returncode}: {tail}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return RunReport(False, [],
                         f"shim produced non-JSON stdout: {exc}")
    if "fatal" in out:
        return RunReport(False, [], out["fatal"])
    results = out.get("results")
    if not isinstance(results, list) or len(results) != len(arg_list):
        return RunReport(False, [],
                         "shim returned malformed results")
    return RunReport(True, results, "")
