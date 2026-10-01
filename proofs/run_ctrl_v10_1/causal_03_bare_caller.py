#!/usr/bin/env python3
"""CAUSAL-03: the quarantine sweep's bare-string caller cannot pass the
restore authorization gate; the engine's own caller context can.

Mechanism under test (runtime/synthesis/integrity.py):
  * RunController._quarantine_sweep calls
    repair_all_quarantined(engine, caller="run_controller", ...) --
    a bare string.
  * repair_quarantine -> restore_everywhere gates the restore step on
    require_all(oreg, AgentDirectory(oreg), caller,
                (DECISION_RESTORE, DECISION_TRUST_TRANSITION), ...) --
    the caller must hold 'agent:restore' + 'trust:transition',
    token-authenticated ("a forged handle with a bare producer_id no
    longer passes").
  * The Controller already knows the legitimate path: _q7_substrate_trigger
    mints _engine_caller_context(self.engine) ("carries the engine's own
    caller context, not a bare string, so the restore path's
    authorization gate is satisfied, never bypassed").

Causal claim: the EXACT gate restore_everywhere uses refuses
caller="run_controller" (the sweep's current caller) and authorizes
caller=_engine_caller_context(engine) (the repair). So a quarantined
capability whose diagnostic verdict is reason_cleared can NEVER be
restored by the current sweep -- the restore step is dead on arrival --
while the authorized caller opens the real restore path.

Exit 0 only if the refusal/authorization difference is demonstrated on
the real gate.
"""

import os
import sys
import tempfile

WT = "/home/hatch/workspace/worktrees/warm-v10-convergence"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.governance.caller_authorization import (
    AgentDirectory, require_all, _engine_caller_context)
from swarm_engine.governance.oracle_binding import (
    DECISION_RESTORE, DECISION_TRUST_TRANSITION)

PASSED = 0


def check(cond, msg):
    global PASSED
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    PASSED += 1
    print(f"ok [{PASSED}]: {msg}")


td = tempfile.mkdtemp(prefix="rcv10_causal03_")
eng = SwarmEngine(db_path=os.path.join(td, "eng.db"))
oreg = getattr(eng, "oracle_registry", None)
check(oreg is not None, "engine exposes an oracle registry (the gate's "
                        "authority source)")

# The exact gate restore_everywhere applies, with the sweep's caller.
bare_refused = None
try:
    require_all(oreg, AgentDirectory(oreg), "run_controller",
                (DECISION_RESTORE, DECISION_TRUST_TRANSITION),
                "restore_everywhere", target="causal-cap-01")
    bare_refused = False
except Exception as exc:
    bare_refused = True
    bare_err = f"{type(exc).__name__}: {exc}"
check(bare_refused,
      "the real restore gate REFUSES caller='run_controller' "
      f"(bare string): {bare_err[:160]}")

# The same gate with the engine's own caller context (the repair).
ctx = _engine_caller_context(eng)
authed = None
try:
    authed = require_all(
        oreg, AgentDirectory(oreg), ctx,
        (DECISION_RESTORE, DECISION_TRUST_TRANSITION),
        "restore_everywhere", target="causal-cap-01")
except Exception as exc:
    authed = f"REFUSED: {type(exc).__name__}: {exc}"
# NB: require_all returns the authorized identity STRING ("remor:engine")
# on success -- success is "no REFUSED marker", not "non-string".
check(not (isinstance(authed, str) and authed.startswith("REFUSED")),
      "the real restore gate AUTHORIZES the engine's own caller context "
      f"(authorized as {authed!r}; the Controller's _q7 path already "
      "mints this)")

print(f"\nCAUSAL-03 PROVEN: bare-string caller refused, engine caller "
      f"context authorized, on restore_everywhere's real gate "
      f"({PASSED}/3 checks).")
