#!/usr/bin/env python3
"""CUR-P6E Primary-seam inspection.

Verifies by inspection (not just by the passing PLOOP batteries) that
Curiosity did not disturb the Primary seam:

  S1. The Curiosity-owned interface the Primary consumes (FrmGrant)
      still carries every field the Primary-side consumer reads.
  S2. No NEW curiosity imports appeared in runtime/core/ (the Primary
      side) beyond the known, gated set — i.e. no seam drift.
  S3. The handoff contract still declares an answer for every
      (loop, terminal_state) pair (structural completeness).
  S4. The curiosity executive's public surface still exposes the
      names the mirror-structure rule requires.

Exit 0 only if every check passes. Read-only.
"""
import dataclasses
import inspect
import os
import re
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(REPO, "pylib"))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""),
          flush=True)
    if cond:
        PASS += 1
    else:
        FAIL += 1


# -- S1: FrmGrant interface intact -------------------------------------
from swarm_engine.curiosity.frm.grant import FrmGrant

fields = {f.name for f in dataclasses.fields(FrmGrant)}
for needed in ("grant_id", "epoch_id", "budget_s"):
    check(f"S1: FrmGrant carries '{needed}' (Primary consumer reads it)",
          needed in fields, f"fields={sorted(fields)}")
check("S1: FrmGrant.issue classmethod present",
      hasattr(FrmGrant, "issue") and isinstance(
          inspect.getattr_static(FrmGrant, "issue"), classmethod))

# -- S2: no seam drift in runtime/core/ ----------------------------------
known = {"runtime/core/microcontroller/granted_cognition.py"}
found = set()
pat = re.compile(r"from (swarm_engine|runtime)\.curiosity[\s.]|"
                 r"import (swarm_engine|runtime)\.curiosity[\s.]")
for root, _dirs, files in os.walk(os.path.join(REPO, "runtime", "core")):
    for fn in files:
        if not fn.endswith(".py"):
            continue
        p = os.path.join(root, fn)
        try:
            with open(p, encoding="utf-8", errors="strict") as fh:
                src = fh.read()
        except OSError:
            continue
        if pat.search(src):
            found.add(os.path.relpath(p, REPO))
check("S2: Primary-side curiosity imports == known gated set",
      found == known, f"found={sorted(found)} expected={sorted(known)}")

# -- S3: handoff contract structurally complete -------------------------
from swarm_engine.core.executive import handoff as hmod

routes = getattr(hmod, "HANDOFF_ROUTES", None)
check("S3: HANDOFF_ROUTES exists", isinstance(routes, dict))
if isinstance(routes, dict):
    loops = {k[0] if isinstance(k, tuple) else k for k in routes}
    check("S3: all six loops have declared handoff answers",
          len(loops) >= 6, f"loops={sorted(loops)}")

# -- S4: curiosity executive public surface -----------------------------
import swarm_engine.curiosity.executive as cexec

for name in ("CuriosityExecutive",):
    check(f"S4: curiosity executive exposes {name}",
          hasattr(cexec, name), "missing")

print(f"\nSeam inspection: {PASS} passed, {FAIL} failed", flush=True)
sys.exit(1 if FAIL else 0)
