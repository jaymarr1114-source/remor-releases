"""P1: factory wiring -- llm template instantiates iff wired.

Wired: factory.create('tpl_llm_coder_v1') returns a real AgentRecord whose
substrate is a wired LLMSubstrate (describe() reports REAL).
Unwired: factory.create raises SubstrateUnavailable (honest ABSENT,
pre-wiring behavior preserved).
No inference in this probe.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (  # noqa: E402
    PASS, FAIL, check, report, real_wiring, SCRATCH,
)

from swarm_engine.agent_org.factory import AgentFactory  # noqa: E402
from swarm_engine.agent_org.identity import AgentRecord  # noqa: E402
from swarm_engine.agent_org.registry import AgentRegistry  # noqa: E402
from swarm_engine.agent_org.store import OrgStore  # noqa: E402
from swarm_engine.agent_org.substrates import (  # noqa: E402
    LLMSubstrate, SubstrateUnavailable,
)
from swarm_engine.agent_org.templates import (  # noqa: E402
    TemplateRegistry, seed_templates,
)
from swarm_engine.governance.oracle_binding import OracleRegistry  # noqa: E402


def make_org(base_dir, llm_wiring=None):
    os.makedirs(base_dir, exist_ok=True)
    store = OrgStore(os.path.join(base_dir, "agent_org.db"))
    oreg = OracleRegistry(os.path.join(base_dir, "oracle.db"))
    engine = oreg.engine_handle()
    templates = TemplateRegistry(store, engine)
    seed_templates(templates)
    registry = AgentRegistry(store, oreg, engine,
                             os.path.join(base_dir, "agents"))
    factory = AgentFactory(registry, templates, llm_wiring=llm_wiring)
    return factory


def main():
    base = tempfile.mkdtemp(prefix="llm_p1_", dir=SCRATCH)

    # -- wired ---------------------------------------------------------
    wiring = real_wiring()
    check("P1", "wiring_built", wiring is not None)
    factory = make_org(os.path.join(base, "wired"), llm_wiring=wiring)
    rec = factory.create("tpl_llm_coder_v1")
    check("P1", "wired_create_returns_record",
          isinstance(rec, AgentRecord), type(rec).__name__)
    check("P1", "wired_record_state_available", rec.state == "AVAILABLE",
          rec.state)
    sub = factory.registry.get_substrate(rec.agent_id)
    check("P1", "wired_substrate_is_llm",
          isinstance(sub, LLMSubstrate), type(sub).__name__)
    check("P1", "wired_substrate_reports_real",
          sub.wired and "REAL" in sub.describe()["status"],
          str(sub.describe().get("status")))
    check("P1", "wired_teacher_is_qwen3",
          sub.describe()["teacher"]["model_id"] == "qwen3",
          str(sub.describe().get("teacher")))

    # -- unwired -------------------------------------------------------
    factory_u = make_org(os.path.join(base, "unwired"), llm_wiring=None)
    try:
        factory_u.create("tpl_llm_coder_v1")
        check("P1", "unwired_create_refuses", False,
              "no SubstrateUnavailable raised")
    except SubstrateUnavailable as ex:
        check("P1", "unwired_create_refuses", "llm" in str(ex), str(ex))

    ok = report("P1")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
