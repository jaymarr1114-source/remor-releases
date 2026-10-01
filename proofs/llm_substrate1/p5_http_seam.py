"""P5: HTTP seam -- POST /api/agents with the llm template.

Wired service: register_agent('tpl_llm_coder_v1') -> ok:true with a real
agent record; list_templates marks the llm template instantiable.
Unwired service: register_agent -> honest unavailable payload with code
agent_instantiation (pre-wiring behavior preserved); instantiable False.
No inference in this probe (registration never thinks).
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (  # noqa: E402
    PASS, FAIL, check, report, real_wiring, SCRATCH,
)
from swarm_engine.services.agent_api import (  # noqa: E402
    AgentService, dispatch_agents,
)


def register(service, template_id):
    return dispatch_agents(service, "POST", "/api/agents",
                           {"template_id": template_id})


def main():
    base = tempfile.mkdtemp(prefix="llm_p5_", dir=SCRATCH)

    # -- wired ---------------------------------------------------------
    wiring = real_wiring()
    svc = AgentService(os.path.join(base, "wired"), llm_wiring=wiring)
    r = register(svc, "tpl_llm_coder_v1")
    check("P5", "wired_register_ok", r.get("ok") is True, str(r)[:200])
    agent = (r.get("agent") or {})
    check("P5", "wired_agent_record",
          agent.get("template_id") == "tpl_llm_coder_v1", str(agent))
    check("P5", "wired_agent_available",
          agent.get("state") == "AVAILABLE", str(agent.get("state")))
    check("P5", "wired_substrate_kind_llm",
          agent.get("substrate_kind") == "llm",
          str(agent.get("substrate_kind")))
    tpls = dispatch_agents(svc, "GET", "/api/agent-templates", {})
    by_id = {t["template_id"]: t for t in tpls["templates"]}
    check("P5", "wired_template_instantiable",
          by_id["tpl_llm_coder_v1"]["instantiable"] is True,
          str(by_id["tpl_llm_coder_v1"]))

    # -- unwired -------------------------------------------------------
    svc_u = AgentService(os.path.join(base, "unwired"))
    r2 = register(svc_u, "tpl_llm_coder_v1")
    check("P5", "unwired_register_unavailable",
          r2.get("ok") is not True and "unavailable" in r2,
          str(r2)[:200])
    check("P5", "unwired_code_agent_instantiation",
          (r2.get("unavailable") or {}).get("code") == "agent_instantiation",
          str(r2.get("unavailable")))
    tpls_u = dispatch_agents(svc_u, "GET", "/api/agent-templates", {})
    by_id_u = {t["template_id"]: t for t in tpls_u["templates"]}
    check("P5", "unwired_template_not_instantiable",
          by_id_u["tpl_llm_coder_v1"]["instantiable"] is False,
          str(by_id_u["tpl_llm_coder_v1"]))

    ok = report("P5")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
