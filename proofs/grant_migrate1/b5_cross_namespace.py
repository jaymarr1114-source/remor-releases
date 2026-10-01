"""b5: cross-namespace identity — the hardlink dual-import hazard.

runtime/curiosity and pylib/swarm_engine/curiosity are the same files
(hardlinked) but import as different module identities. Every FrmGrant
issuer/consumer uses the swarm_engine spelling; the migrated ledger must
accept exactly that identity no matter which namespace imported the
ledger itself.
"""
import os
import tempfile
from common import check, summary

TMP = tempfile.mkdtemp(prefix="gm_b5_")


@check("b5_ledger_both_namespaces_one_identity")
def _():
    import runtime.curiosity.attribution.grants as r_ledger_mod
    import swarm_engine.curiosity.attribution.grants as s_ledger_mod
    assert r_ledger_mod.FrmGrant is s_ledger_mod.FrmGrant, \
        "ledger binds two FrmGrant identities"
    return "one FrmGrant object across both import paths"


@check("b5_issuer_grant_accepted")
def _():
    # Issued the way the real FRM evaluation issues it (swarm_engine
    # spelling, cf. runtime/curiosity/frm/evaluation.py:56), recorded
    # through the ledger imported via the runtime spelling.
    from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
    import runtime.curiosity.attribution.grants as r_ledger_mod
    import time
    g = FrmGrant.issue(domain="frm", epoch_id=42, epoch_s=300.0,
                       budget_s=10.0, max_concurrent=1,
                       primary_minimum_budget_s=1.0,
                       primary_minimum_concurrent=1,
                       lent=True, lending=LendingRecord(5.0, 1),
                       enforcement_state_at_issue="RUNNING",
                       issued_at=time.time())
    led = r_ledger_mod.AllocationLedger(os.path.join(TMP, "b5.db"))
    led.record_grant(g)  # must not raise "grant must be FrmGrant"
    g2 = led.get_grant(g.grant_id)
    assert g2.lent is True
    assert g2.lending.lent_budget_s == 5.0, g2.lending
    assert g2.domain == "frm"
    led.close()
    return "issuer-identity grant recorded, lending detail round-tripped"


summary("b5")
