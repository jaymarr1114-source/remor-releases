"""Production construction of the CuriosityExecutive.

THE production construction site for the curiosity executive with the
acquisition bridge bound.

Prior to this module, the CuriosityExecutive was constructed only in
hardening drills (runtime/curiosity/hardening/) and in tests/proofs: no
production construction site existed, so James's audit premise ("no
acquisition_bridge= is passed by any production constructor") was true
only because there were no production constructors at all. This module
is the first one.

``build_production_executive`` builds the real governance stack (FRM,
roll-call/GAM, enforcement state, substrate, run controller) under a base
directory and binds ``acquisition_bridge`` to a REAL AcquisitionPipeline
with REAL governed sources (LocalSource + governor-gated NetworkSource,
the same source configuration the SwarmEngine uses in production). The
bridge is built by
``swarm_engine.acquisition.curiosity_bridge.build_acquisition_bridge``.

What this module does NOT do (explicit non-goals, owned elsewhere):
- It does not wire the executive into the engine's HTTP boot
  (runtime/services/http_adapter). Engine-boot integration is a
  cross-track architectural decision (curiosity track + engine boot
  owners) and is named as the exact next boundary.
- It does not alter curiosity's refusal logic. The bridge adds
  acquisition; refusal stays fail-closed exactly as built.
- It does not conduct the roll-call attestation. The caller conducts it
  (the proof does, following the drill pattern); without a MET
  attestation on record, request_activation refuses exactly as designed.

Fail-closed preserved: with no bridge the executive refuses
(NO_ACQUISITION_BRIDGE). This module is how the bridge gets bound; it
never bypasses the refusal.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def build_production_executive(
    *,
    base_dir: str,
    pipeline: Optional[Any] = None,
    network_endpoint: Optional[str] = None,
    demand_budget_s: float = 60.0,
    demand_concurrent: int = 2,
    total_budget_s: float = 600.0,
    corpus_docs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construct the production CuriosityExecutive with the bridge bound.

    base_dir: isolated root holding all curiosity state (dbs, payloads).
    pipeline: a real AcquisitionPipeline; when None, one is built with the
        production governed sources (LocalSource + governor-gated
        NetworkSource over the trusted endpoint configuration).
    Returns a dict with the executive under "executive" plus the built
    components (frm, gam, run_controller, pipeline, local_source, dirs)
    for the caller (proofs, future engine integration).
    """
    from swarm_engine.curiosity.executive.executive import CuriosityExecutive
    from swarm_engine.curiosity.frm.policy import FrmPolicy
    from swarm_engine.curiosity.frm.evaluation import FinancialResourceManager
    from swarm_engine.curiosity.rollcall.scheduler import (
        RollCallPolicy, RollCallScheduler)
    from swarm_engine.curiosity.rollcall.ledger import AttestationLedger
    from swarm_engine.curiosity.rollcall.gam import GovernanceAttestationMonitor
    from swarm_engine.curiosity.substrate import CuriositySubstrate
    from swarm_engine.curiosity.run_controller.controller import (
        CuriosityRunController)
    from swarm_engine.acquisition.curiosity_bridge import (
        build_acquisition_bridge)

    root = Path(base_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "payloads").mkdir(parents=True, exist_ok=True)

    # -- real FRM (the evaluation layer, not a double) -------------------
    policy = FrmPolicy(
        total_budget_s=total_budget_s, total_max_concurrent=8,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        epoch_s=300.0)
    frm = FinancialResourceManager(policy)

    # -- real roll-call / GAM --------------------------------------------
    sched = RollCallScheduler(RollCallPolicy())
    gam = GovernanceAttestationMonitor(
        sched, AttestationLedger(str(root / "att.db")))

    # -- enforcement state dir (absent record reads as RUNNING, the
    #    authority's own bootstrap semantic -- see executive._read_enforcement_state)
    enf_dir = str(root / "enf")
    os.makedirs(enf_dir, exist_ok=True)

    # -- real substrate + run controller ---------------------------------
    sub = CuriositySubstrate()
    rc = CuriosityRunController(
        substrate=sub, checkpoint_db=str(root / "ckpt.db"),
        evidence_db=str(root / "ev.db"),
        ledger_db=str(root / "term.db"),
        attribution_db=str(root / "attr.db"),
        payload_dir=str(root / "payloads"),
        corpus_docs=list(corpus_docs or []))

    # -- real governed pipeline (when the caller does not supply one) ----
    local_source = None
    if pipeline is None:
        from swarm_engine.acquisition.pipeline import (
            AcquisitionPipeline, LocalSource, NetworkSource,
            http_json_index_fetcher)
        from swarm_engine.primitives.core import Governor
        from swarm_engine.governance.provenance import ProvenanceStore
        local_source = LocalSource()
        endpoint = network_endpoint or os.environ.get(
            "REMOR_NETWORK_INDEX", "https://example.invalid/index")
        governor = Governor()  # deny-by-default: real governance
        net_source = NetworkSource(
            governor, fetcher=http_json_index_fetcher(endpoint),
            endpoint=endpoint)
        provenance = ProvenanceStore(db_path=str(root / "provenance.db"))
        pipeline = AcquisitionPipeline(
            sources=[local_source, net_source],
            provenance=provenance,
            # registrar omitted: the bridge proof asserts discovery, not
            # admission; admission wiring is the landing's business.
        )

    # -- THE binding: one production construction -------------------------
    bridge = build_acquisition_bridge(pipeline)
    executive = CuriosityExecutive(
        frm=frm, enforcement_state_dir=enf_dir, gam=gam,
        run_controller=rc, demand_budget_s=demand_budget_s,
        demand_concurrent=demand_concurrent,
        acquisition_bridge=bridge)

    return {
        "executive": executive,
        "bridge": bridge,
        "pipeline": pipeline,
        "local_source": local_source,
        "frm": frm,
        "gam": gam,
        "run_controller": rc,
        "enf_dir": enf_dir,
        "base_dir": str(root),
    }
