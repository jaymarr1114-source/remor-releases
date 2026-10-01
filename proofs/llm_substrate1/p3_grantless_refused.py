"""P3: grantless cognition refuses through the REAL path.

Adversarial: the grant issuer returns None. The substrate must still call
provider.request_cognition WITHOUT frm_grant, so the provider's genuine
R_NO_GRANT refusal fires (cognition_refused:no_grant) -- not a
substrate-side pre-check. No inference happens (refusal precedes the
teacher). The refusal surfaces as LLMCognitionRefused.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (  # noqa: E402
    PASS, FAIL, check, report, real_wiring, hostile_wiring,
)
from swarm_engine.agent_org.substrates import LLMCognitionRefused  # noqa: E402


def main():
    base = real_wiring()
    wiring = hostile_wiring(base, lambda estimate: None)
    sub = wiring.build_substrate()
    try:
        sub.run({"objective": "anything"})
        check("P3", "grantless_refuses", False, "no refusal raised")
    except LLMCognitionRefused as ex:
        check("P3", "grantless_refuses", True)
        check("P3", "refusal_is_no_grant",
              "cognition_refused:no_grant" in str(ex), str(ex)[:120])
        check("P3", "outcome_prefix", ex.outcome == "cognition_refused",
              ex.outcome)
    # The provider must not have consumed anything.
    check("P3", "no_charge_without_grant",
          base.provider.grant_consumed_s("") == 0.0)

    ok = report("P3")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
