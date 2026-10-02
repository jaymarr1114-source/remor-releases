"""STUDENT-ENFORCE-1: single enforcement core serves both paths.

- read_enforcement_state returns RUNNING with no dir.
- is_running / check_running behave correctly.
- make_enforcement_reader honors override.
- No duplicated logic: chat_api and router import from the core.
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/student-enforce-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

from swarm_engine.services.enforcement_core import (
    read_enforcement_state, is_running, check_running,
    make_enforcement_reader, RUNNING)

check("e1_no_dir_running", read_enforcement_state(None) == RUNNING)
check("e1_is_running_true", is_running(RUNNING))
check("e1_is_running_false", not is_running("SUSPENDED_SAFETY"))
check("e1_check_none_when_running", check_running(RUNNING) is None)
r = check_running("SUSPENDED_SAFETY")
check("e1_check_reason_when_blocked", r is not None and "SUSPENDED_SAFETY" in r, (r or "")[:60])

# Override takes precedence.
ov = make_enforcement_reader(None, override=lambda: "CUSTOM")
check("e1_override_honored", ov() == "CUSTOM")
dflt = make_enforcement_reader(None)
check("e1_default_running", dflt() == RUNNING)

# Both consumers import from the core (no duplication).
import ast
for path, names in [
    (os.path.join(WT, "runtime/services/chat_api.py"), ["make_enforcement_reader"]),
    (os.path.join(WT, "distill1/router.py"), ["check_running"]),
]:
    with open(path) as f:
        src = f.read()
    for n in names:
        check(f"e1_uses_core_{os.path.basename(path)}_{n}", n in src, n)

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
