"""Shared harness for Phase 4 batteries (repair lifecycle + learning anti-gaming).

Operational model (same as the repair-loop and trust-anchor missions):

* Each battery deploys a fresh org into an isolated nested workdir via
  the scratch anchor_shim: explicit anchor genesis (AnchorStore.initialize
  -- no lazy genesis), then install_auto_anchor re-anchors the journal
  after every legitimate chained write (store.insert and
  OracleRegistry._insert_chained). Attack writes use raw SQL through
  separate connections and bypass the wrapper -- they are detected by
  the internal chain audits and the external anchor verify.
* Caller identities are provisioned in the caller-authorization
  directory (AgentDirectory) with bearer tokens; the engine operator
  (root 'agent:grant_issue'... actually 'agent:grant' issuance) is the
  registrar.
* Repair verification uses the real ReviewBoard.verify_repair path
  (real IndependentValidator in a real subprocess, oracle-bound) on a
  REAL defect (add returning a-b, repaired to a+b).
* Dispatch anti-gaming uses a real SwarmEngine, a real admitted
  capability, and a real NLToolDispatcher dispatch.

check()/failfast() accounting: every battery prints per-check results
and exits nonzero on any failure, so counts are real.
"""
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CANONICAL = os.path.join(_THIS_DIR, "..", "..")
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))  # swarm_engine -> runtime
sys.path.insert(0, os.path.join(_CANONICAL, "tests", "agent_org"))  # anchor_shim

# The dispatch-evidence re-execution harness and the IndependentValidator
# run in real subprocesses via run_code, which inherits the environment:
# PYTHONPATH must let those subprocesses import swarm_engine.
_PYL = os.path.join(_CANONICAL, "pylib")
os.environ["PYTHONPATH"] = _PYL + os.pathsep + os.environ.get("PYTHONPATH", "")

_WORK_ROOT = os.environ.get("REMOR_TEST_WORK_ROOT", "/tmp/remor_phase4_test")
WORK = os.path.join(_WORK_ROOT, "phase4")
os.makedirs(WORK, exist_ok=True)

import anchor_shim  # noqa: E402
from swarm_engine.agent_org.store import digest  # noqa: E402
from swarm_engine.acquisition.semantic import Case  # noqa: E402

CHECKS = {"pass": 0, "fail": 0, "log": []}


def check(name, cond, detail=""):
    CHECKS["log"].append((name, bool(cond), detail))
    if cond:
        CHECKS["pass"] += 1
        print(f"  ok: {name}", flush=True)
    else:
        CHECKS["fail"] += 1
        print(f"  FAIL: {name}" + (f" -- {detail}" if detail else ""),
              flush=True)


def failfast(label="battery"):
    if CHECKS["fail"]:
        raise SystemExit(
            f"{label}: {CHECKS['fail']} checks failed "
            f"({CHECKS['pass']} passed)")
    print(f"{label}: ALL {CHECKS['pass']} checks passed", flush=True)


def deploy_org(workdir):
    """Fresh org + explicit anchor genesis + auto-anchor wrapper.

    The org workdir is nested one level (<workdir>/nested/work): the
    anchor journal lives at <dbdir>/../anchor_store, so nesting gives
    every org its own isolated journal (the batteryE pattern) -- two
    orgs under one test BASE never share a journal.
    """
    nested = os.path.join(workdir, "nested", "work")
    os.makedirs(nested, exist_ok=True)
    return anchor_shim.deploy_org(nested)


def provision_caller(org, agent_id, *decision_classes):
    """Register agent_id in the caller-authorization directory with the
    given decision-class grants (engine operator is the registrar).
    Returns the bearer token."""
    from swarm_engine.governance.caller_authorization import AgentDirectory
    agents = AgentDirectory(org.oregistry)
    cred = agents.register_agent(
        org.oregistry.engine_handle(), source="phase4 harness",
        agent_id=agent_id, decision_classes=decision_classes)
    return cred.token


# ---------------------------------------------------------------------------
# Real defect + repair used by the lifecycle battery.
# D1: add(a,b) returns a-b (defect). V1 repair: a+b. V2 repair: a+b via a
# differently-shaped (but semantically identical) body, so both verify
# against the same held-out examples and form one lineage.
# ---------------------------------------------------------------------------
D1_SRC = "def add(a, b):\n    return a - b\n"
D1_TEST = ("assert add(1, 2) == 3\n"
           "assert add(2, 3) == 5\n"
           "assert add(0, 0) == 0\n"
           "assert add(-1, 1) == 0\n")
V1_POST = "def add(a, b):\n    return a + b\n"
V2_POST = "def add(a, b):\n    total = a + b\n    return total\n"
V3_POST = "def add(a, b):\n    return (a + b) + 0\n"

EXAMPLES = [((1, 2), 3), ((2, 3), 5), ((0, 0), 0), ((-1, 1), 0)]


class FuncSpec:
    input_names = ["a", "b"]
    examples = [({"a": 1, "b": 2}, None)]


def instance_cases(examples, label_prefix="ex"):
    return [Case(args={"a": a, "b": b}, expect=expected,
                 label=f"{label_prefix}{i}")
            for i, ((a, b), expected) in enumerate(examples)]


def verify_one_repair(org, repair_id, agent_cred_pair, post_code,
                      family="binary-operator", target_path="calc.py"):
    """Independently verify one repair instance through the REAL
    ReviewBoard.verify_repair path. Returns (repair_id, post_code)."""
    agent_id, token = agent_cred_pair
    pre_digest = digest(D1_SRC)
    verdict = org.review.verify_repair(
        repair_id=repair_id, agent_id=agent_id, caller=(agent_id, token),
        defect_signature={"family": family,
                          "file": target_path, "function": "add",
                          "observed": "3/4 examples fail",
                          "pre_digest": pre_digest},
        diagnosis="add returns a-b; tests expect a+b",
        pre_code=D1_SRC, post_code=post_code,
        entrypoint="add", spec=FuncSpec(),
        cases=instance_cases(EXAMPLES))
    assert verdict.admitted, f"verify_repair did not admit {repair_id}"
    return repair_id


class RepairServiceStub:
    """Duck-typed stand-in for worker 1's runtime/services/repair.py::
    RepairService. Delegates admission to the real ReviewBoard (the
    trust derivation is unchanged); get_repair enriches the stored
    record with the verified post bytes the driver captured at verify
    time -- RepairLifecycle digest-checks them against the chain-stored
    post_digest before trusting them."""

    def __init__(self, org):
        self.org = org
        self._bytes = {}

    def register_bytes(self, repair_id, post_code):
        self._bytes[repair_id] = post_code

    def admit_repair(self, repair_id, caller):
        return self.org.review.admit_repair(repair_id, caller=caller)

    def get_repair(self, repair_id, caller=None):
        # Signature mirrors the real RepairService.get_repair(repair_id,
        # caller); the stub does not authenticate (test double), but the
        # lifecycle digest-checks the returned bytes all the same.
        rec = dict(self.org.review.get_repair_record(repair_id))
        if repair_id in self._bytes:
            rec["post_code"] = self._bytes[repair_id]
        return rec
