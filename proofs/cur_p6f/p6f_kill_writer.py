"""CUR-P6F kill-writer: hammers one store's real write path in a tight loop.

Run as a subprocess; the battery SIGKILLs it mid-write, then verifies from
a brand-new process that no torn write is ever presented as valid.

Usage: p6f_kill_writer.py --kind {enforcement,evidence,checkpoint,frm}
                           --state-dir DIR --heartbeat FILE --iters N

Domain discipline (mirrors the mission):
  * enforcement: this script runs as __main__ (governance plane) and drives
    the real EnforcementEngine -- the guard allows it.
  * evidence: the submit call is made from p6f_drill (a
    swarm_engine.curiosity.* frame) so the writer's domain fence passes.
  * checkpoint / frm: no domain fence; direct real writes.

The heartbeat file is touched every iteration so the parent can prove the
kill landed while the writer was actively writing (fresh heartbeat =>
SIGKILL immediately).
"""

from __future__ import annotations

import argparse
import os
import sys
import time


def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"{time.time()}\n")


def run_enforcement(state_dir: str, heartbeat: str, iters: int) -> None:
    from swarm_engine.governance.curiosity_enforcement._engine import (
        EnforcementEngine,
    )
    from swarm_engine.governance.curiosity_enforcement.states import (
        EnforcementState,
    )

    engine = EnforcementEngine(state_dir=os.path.join(state_dir,
                                                      "enforcement"))
    # Bounce between WARNING_1 and RUNNING: every iteration performs a real
    # guarded write (state file atomic rename; WARNING_1 is non-terminal so
    # the kill ledger is untouched here -- the ledger path is covered in
    # the battery's terminal-state crash run). The toggle is read from the
    # ACTUAL store each iteration -- the writer is SIGKILLed and restarted,
    # so in-memory state would desync (battery bug class, fixed 2026-10-04).
    for i in range(iters):
        cur = engine.current("curiosity").state
        if cur == EnforcementState.WARNING_1:
            engine.re_enable("curiosity", issuer="james",
                             reason_refs={"probe": f"p6f-kill-{i}"})
        else:
            engine.transition("curiosity", EnforcementState.WARNING_1,
                              issuer="safety-authority",
                              reason_refs={"probe": f"p6f-kill-{i}"})
        _touch(heartbeat)


def run_evidence(state_dir: str, heartbeat: str, iters: int) -> None:
    # The submit itself happens inside p6f_drill (curiosity frame).
    from swarm_engine.curiosity.hardening import p6f_drill

    stack = p6f_drill.build_stack(state_dir)
    for i in range(iters):
        p6f_drill.submit_one(stack, f"kill{i}")
        _touch(heartbeat)


def run_checkpoint(state_dir: str, heartbeat: str, iters: int) -> None:
    from swarm_engine.curiosity.hardening import p6f_drill

    stack = p6f_drill.build_stack(state_dir)
    for i in range(iters):
        p6f_drill.save_one_checkpoint(stack, f"kill{i}")
        _touch(heartbeat)


def run_frm(state_dir: str, heartbeat: str, iters: int) -> None:
    from swarm_engine.curiosity.hardening import p6f_drill

    stack = p6f_drill.build_stack(state_dir)
    for i in range(iters):
        p6f_drill.run_one_frm_round(stack, i)
        _touch(heartbeat)


def run_enforcement_ledger(state_dir: str, heartbeat: str,
                           iters: int) -> None:
    from swarm_engine.governance.curiosity_enforcement._engine import (
        EnforcementEngine,
    )
    from swarm_engine.governance.curiosity_enforcement.states import (
        EnforcementState,
    )

    engine = EnforcementEngine(state_dir=os.path.join(state_dir,
                                                      "enforcement"))
    # Bounce through the TERMINAL state: every SUSPENDED_SAFETY entry
    # performs a real kill-ledger append (hash-chained) plus a rollback
    # directive write. Toggle read from the ACTUAL store each iteration
    # (SIGKILL-restart safe; see run_enforcement). The decided ladder is
    # RUNNING -> WARNING_1 -> SUSPENDED_SAFETY -> RUNNING (no direct
    # RUNNING -> SUSPENDED_SAFETY).
    for i in range(iters):
        cur = engine.current("curiosity").state
        if cur == EnforcementState.SUSPENDED_SAFETY:
            engine.re_enable("curiosity", issuer="james",
                             reason_refs={"probe": f"p6f-kill-ledger-{i}"})
        elif cur == EnforcementState.WARNING_1:
            engine.transition(
                "curiosity", EnforcementState.SUSPENDED_SAFETY,
                issuer="safety-authority",
                reason_refs={"probe": f"p6f-kill-ledger-{i}",
                             "last_checkin_ref": f"ckpt_p6f_{i}"})
        else:
            engine.transition("curiosity", EnforcementState.WARNING_1,
                              issuer="safety-authority",
                              reason_refs={"probe": f"p6f-kill-ledger-{i}"})
        _touch(heartbeat)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True,
                    choices=("enforcement", "enforcement_ledger", "evidence",
                             "checkpoint", "frm"))
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--heartbeat", required=True)
    ap.add_argument("--iters", type=int, default=200)
    args = ap.parse_args()
    # Repo root for the swarm_engine symlink.
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    _touch(args.heartbeat)
    print("READY", flush=True)
    {"enforcement": run_enforcement,
     "enforcement_ledger": run_enforcement_ledger,
     "evidence": run_evidence, "checkpoint": run_checkpoint,
     "frm": run_frm}[args.kind](
        args.state_dir, args.heartbeat, args.iters)
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
