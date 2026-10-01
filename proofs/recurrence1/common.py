"""Shared helpers for the RECURRENCE-1 proof battery.

Every probe runs in its own FRESH process (gate_run.sh). Each probe
creates its own scratch dir under $RECURRENCE1_SCRATCH and cleans up
its service (pumper thread) before exiting.
"""
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WT = os.path.normpath(os.path.join(_HERE, "..", ".."))
sys.path.insert(0, os.path.join(_WT, "pylib"))

SCRATCH = os.environ.get("RECURRENCE1_SCRATCH") or tempfile.mkdtemp(
    prefix="recurrence1_")


def fresh_dir(prefix="r1_"):
    d = tempfile.mkdtemp(prefix=prefix, dir=SCRATCH)
    return d


class SpySubmit:
    """A guarded_submit stand-in that records every fire."""

    def __init__(self):
        self.calls = []

    def __call__(self, goal, examples=None, metadata=None,
                 conversation_id=None, project_id=None):
        self.calls.append({
            "goal": goal, "examples": examples, "metadata": metadata,
            "conversation_id": conversation_id, "project_id": project_id,
            "at": time.time(),
        })
        return {"ok": True, "run_id": f"run-{len(self.calls)}"}


def wait_for(cond, timeout=15.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def check(name, cond, detail=""):
    print(("  ok: " if cond else "  FAIL: ") + name +
          (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(f"PROBE FAILURE: {name} {detail}")
