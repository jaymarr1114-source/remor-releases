"""P4: insufficient grant defers with ZERO charge.

Adversarial: the grant issuer returns a real FrmGrant with a 0.001s
budget -- far below the teacher's ~180s+ cost estimate. The provider's
genuine deferral path must fire (cognition_deferred:insufficient_grant),
no inference happens, and grant_consumed_s stays 0.0 (no silent partial
charge). Surfaces as LLMCognitionRefused with outcome cognition_deferred.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (  # noqa: E402
    PASS, FAIL, check, report, real_wiring, hostile_wiring, tiny_grant,
)
from swarm_engine.agent_org.substrates import LLMCognitionRefused  # noqa: E402


def main():
    base = real_wiring()
    grant_box = {}

    def issuer(estimate):
        g = tiny_grant()
        grant_box["grant"] = g
        return g

    wiring = hostile_wiring(base, issuer)
    sub = wiring.build_substrate()
    try:
        sub.run({"objective": "anything"})
        check("P4", "insufficient_defers", False, "no deferral raised")
    except LLMCognitionRefused as ex:
        check("P4", "insufficient_defers", True)
        check("P4", "deferral_is_insufficient_grant",
              "cognition_deferred:insufficient_grant" in str(ex),
              str(ex)[:140])
        check("P4", "outcome_prefix", ex.outcome == "cognition_deferred",
              ex.outcome)
    g = grant_box.get("grant")
    check("P4", "grant_was_real_frmgrant", g is not None)
    if g is not None:
        check("P4", "zero_charge_on_deferral",
              base.provider.grant_consumed_s(g.grant_id) == 0.0,
              str(base.provider.grant_consumed_s(g.grant_id)))

    ok = report("P4")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
