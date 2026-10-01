"""Shared helpers for the LLM-SUBSTRATE-1 proof battery.

Every probe runs in a FRESH process (invoked by gate_run.sh); nothing is
shared between probes except the scratch directory root.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = os.path.dirname(os.path.dirname(HERE))  # worktree root
sys.path.insert(0, os.path.join(TREE, "pylib"))

from swarm_engine.services.agent_api import build_llm_wiring  # noqa: E402
from swarm_engine.agent_org.substrates import LLMSubstrateWiring  # noqa: E402
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord  # noqa: E402

SCRATCH = os.environ.get("LLM_SUBSTRATE1_SCRATCH",
                         os.path.join(HERE, "scratch"))

PASS = []
FAIL = []


def check(section, name, cond, detail=""):
    if cond:
        PASS.append(f"{section}:{name}")
    else:
        FAIL.append(f"{section}:{name} -- {detail}")


def report(section):
    print(f"--- {section}: {len([p for p in PASS if p.startswith(section + ':')])} passed, "
          f"{len([f for f in FAIL if f.startswith(section + ':')])} failed")
    for f in FAIL:
        if f.startswith(section + ":"):
            print(f"  FAIL {f}")
    return not any(f.startswith(section + ":") for f in FAIL)


def real_wiring(timeout_s=3600.0):
    """The real wiring: Qwen3 teacher + governed provider + grant issuer.

    Cheap to build (the teacher only verifies files exist; weights page
    in on first inference). timeout_s is generous because the shared
    2-core bench contends with sibling missions' inference.
    """
    return build_llm_wiring(timeout_s=timeout_s)


def hostile_wiring(base, grant_issuer):
    """Same real provider/mc, hostile grant issuer (adversarial probes)."""
    return LLMSubstrateWiring(
        provider=base.provider, grant_issuer=grant_issuer,
        mc_id=base.mc_id)


def tiny_grant(note="adversarial-tiny"):
    return FrmGrant.issue(
        domain="agent_org", epoch_id=int(time.time()), epoch_s=3600.0,
        budget_s=0.001, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue="RUNNING",
        issued_at=time.time(), note=note)


QWEN3_REV = "7c41481f57cb95916b40956ab2f0b139b296d974"
EXPECTED_PROVENANCE_PREFIX = "borrowed:qwen3@"
