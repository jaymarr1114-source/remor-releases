"""P6: runner end-to-end -- real inference through the authority gate.

Wired factory -> agent -> assignment (authority scope with patterns) ->
AgentRunner.execute -> the substrate's result passes the capability gate
(empty claimed_capabilities passes fail-closed) -> work product persisted
with borrowed:qwen3@<rev> provenance in its outputs.

Second REAL inference in the battery (~2-4 min; contention caveat as P2).
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (  # noqa: E402
    PASS, FAIL, check, report, real_wiring, SCRATCH,
    QWEN3_REV, EXPECTED_PROVENANCE_PREFIX,
)
from swarm_engine.agent_org.assignment import AssignmentManager  # noqa: E402
from swarm_engine.agent_org.factory import AgentFactory  # noqa: E402
from swarm_engine.agent_org.registry import AgentRegistry  # noqa: E402
from swarm_engine.agent_org.runner import AgentRunner  # noqa: E402
from swarm_engine.agent_org.store import OrgStore  # noqa: E402
from swarm_engine.agent_org.templates import (  # noqa: E402
    TemplateRegistry, seed_templates,
)
from swarm_engine.agent_org.work_product import (  # noqa: E402
    get_work_product,
)
from swarm_engine.governance.oracle_binding import OracleRegistry  # noqa: E402


def main():
    base = tempfile.mkdtemp(prefix="llm_p6_", dir=SCRATCH)
    wiring = real_wiring()

    store = OrgStore(os.path.join(base, "agent_org.db"))
    oreg = OracleRegistry(os.path.join(base, "oracle.db"))
    engine = oreg.engine_handle()
    templates = TemplateRegistry(store, engine)
    seed_templates(templates)
    registry = AgentRegistry(store, oreg, engine,
                             os.path.join(base, "agents"))
    factory = AgentFactory(registry, templates, llm_wiring=wiring)
    rec = factory.create("tpl_llm_coder_v1")
    check("P6", "agent_created", rec.state == "AVAILABLE", rec.state)

    assignments = AssignmentManager(store, oreg, engine, agents=registry)
    asg = assignments.create(
        agent_id=rec.agent_id,
        objective={"task": "write python add(a, b)"},
        constraints={},
        authority_scope={"allowed_capability_patterns": ["*"]},
        expected_outputs={},
        validation_requirements={},
        originating_decision="llm-substrate-1 proof")
    assignments.activate(asg.assignment_id)
    check("P6", "assignment_active",
          assignments.get(asg.assignment_id).state == "ACTIVE")

    runner = AgentRunner(store, assignments, agents=registry, engine=engine)
    task = {"objective": "write a python function add(a, b) returning a + b",
            "params": {"language": "python"}, "entrypoint": "add"}
    t0 = time.monotonic()
    wp_id = runner.execute(asg.assignment_id, task)
    wall = time.monotonic() - t0
    print(f"    [runner e2e wall: {wall:.1f}s]")
    check("P6", "execute_returned_wp", wp_id.startswith("wp_"), wp_id)

    wp = get_work_product(store, wp_id)
    check("P6", "wp_submitted", wp.state == "SUBMITTED", wp.state)
    outputs = wp.outputs or {}
    prov = outputs.get("provenance", "")
    check("P6", "wp_provenance_borrowed_qwen3",
          prov.startswith(EXPECTED_PROVENANCE_PREFIX), prov)
    check("P6", "wp_provenance_pinned",
          prov == f"borrowed:qwen3@{QWEN3_REV}", prov)
    check("P6", "wp_has_implementation_artifact",
          any(a.get("name") == "codec.py" for a in (wp.artifacts or [])),
          str([a.get("name") for a in (wp.artifacts or [])]))
    check("P6", "agent_submitted",
          registry.get(rec.agent_id).state == "SUBMITTED",
          registry.get(rec.agent_id).state)

    ok = report("P6")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
