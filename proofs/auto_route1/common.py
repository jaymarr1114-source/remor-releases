"""Shared harness for the AUTO-ROUTE-1 gate: real FRM grants, a real
budget-tracking substrate double (genuinely depletes), the governed
student inlet, and the deep GrantedCognitionProvider. Nothing is mocked:
the student talks to the real persistent llama-server and the deep path
runs the real Qwen3-8B through llama-cli (or fails honestly)."""
import os
import sys
import time

TREE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(TREE, "pylib"))
sys.path.insert(0, os.path.join(TREE, "distill1"))
sys.path.insert(0, TREE)

from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord  # noqa: E402
from governed_student import GovernedStudent  # noqa: E402
from router import AutoRouter  # noqa: E402
from runtime.core.microcontroller.granted_cognition import (  # noqa: E402
    GrantedCognitionProvider,
)
from runtime.core.microcontroller.qwen3_teacher import Qwen3Teacher  # noqa: E402

GGUF = os.path.expanduser(
    "~/workspace/models/qwen3-8b/qwen3-8b-q4_k_m.d98cdcbd03e17ce4.gguf")
LLAMA_CLI = os.path.expanduser(
    "~/workspace/tools/llama.cpp-b11284/llama-b11284/llama-cli")


class SubstrateDouble:
    """Real budget-tracking substrate double (genuinely depletes)."""

    def __init__(self, budget_s=100000.0):
        self.remaining_s = float(budget_s)
        self.calls = []

    def charge(self, mc_id, seconds):
        self.calls.append((mc_id, float(seconds)))
        self.remaining_s -= float(seconds)
        if self.remaining_s > 0:
            return False, "ACTIVE"
        return True, "EXHAUSTED"


def make_grant(budget_s=400.0, max_concurrent=1,
               enforcement_state_at_issue="RUNNING", epoch_id=1,
               note="auto-route-1-gate"):
    return FrmGrant.issue(
        domain="curiosity", epoch_id=epoch_id, epoch_s=300.0,
        budget_s=budget_s, max_concurrent=max_concurrent,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue=enforcement_state_at_issue,
        issued_at=time.time(), note=note)


def make_router(enforcement_state=None, teacher=None,
                substrate=None, mc_id="auto-route-gate"):
    substrate = substrate or SubstrateDouble()
    student = GovernedStudent(substrate, mc_id)
    deep = GrantedCognitionProvider(
        substrate, native=None,
        teacher=teacher or Qwen3Teacher(gguf_path=GGUF, llama_cli=LLAMA_CLI))
    router = AutoRouter(student, deep, mc_id=mc_id,
                        enforcement_state=enforcement_state)
    return router, substrate


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"{status} {name}" + (f": {detail}" if detail else ""),
          flush=True)
    if not cond:
        raise SystemExit(f"battery failed at {name}: {detail}")
