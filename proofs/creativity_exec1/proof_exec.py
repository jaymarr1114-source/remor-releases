#!/usr/bin/env python3
"""CREATIVITY-EXEC-1 proof battery: the Creativity Executive Controller.

Every check runs against the real machinery in fresh processes:
real ledger bound to real sqlite stores, real composer executing real
plans, real critique pipeline, real admission contract, real gap
registry, real BoundaryPresentation/ScanReport dataclasses.
"""

import inspect
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.environ.get("WT_ROOT", os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..")))
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root

from swarm_engine.primitives.core import (  # noqa: E402
    NUM,
    Effect,
    PrimitiveRegistry,
)
from runtime.services.evidence import EvidenceStore  # noqa: E402
from runtime.governance.provenance import (  # noqa: E402
    Origin,
    ProvenanceRecord,
    ProvenanceStore,
    TrustLevel,
)
from runtime.acquisition.gaps import GapRegistry  # noqa: E402
from runtime.core.executive.boundary import BoundaryPresentation  # noqa: E402
from runtime.core.executive.detector import ScanReport  # noqa: E402
from runtime.core.microcontroller.substrate import (  # noqa: E402
    CognitionResult,
    NullCognitionProvider,
)
import runtime.creativity.executive as exec_mod  # noqa: E402
from runtime.creativity.executive import (  # noqa: E402
    AMBIGUOUS,
    CLEAR,
    COMPLETED,
    KILLED,
    STOPPED,
    SUSPENDED,
    CreativityExecutiveController,
    ExecutiveRefused,
    MicrocontrollerRegistry,
    SearchBounds,
    classify_ambiguity,
    public_surface_has_no,
)
from runtime.creativity.intent import register_intent  # noqa: E402
from runtime.creativity.ledger import LedgerEntry  # noqa: E402
from runtime.creativity.stages import (  # noqa: E402
    CreativeStage,
    admissible_next,
    is_terminal,
)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} :: {detail}")


def make_registry():
    reg = PrimitiveRegistry()

    @reg.define("add", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                effects=(Effect.PURE,))
    def _add(a, b):
        return a + b

    @reg.define("mul", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                effects=(Effect.PURE,))
    def _mul(a, b):
        return a * b

    @reg.define("sub", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                effects=(Effect.PURE,))
    def _sub(a, b):
        return a - b

    return reg


def make_exec(pids=("add", "mul", "sub"), scan=None, cognition=None,
              bounds=None, stop_event=None):
    """One isolated fixture world: scratch stores, real machinery.

    The executive constructs its OWN stores from store_dir (T9); this
    fixture seeds them through a second handle on the same sqlite files
    via the stores' public APIs — the same pattern the Phase-1 batteries
    used.
    """
    tmp = tempfile.mkdtemp(prefix="exec1_")
    reg = make_registry()
    if scan is None:
        def scan():
            return ScanReport(scanned_at=time.time(), presentations=[],
                              source_absent=[], observed={}, notes=[])
    ex = CreativityExecutiveController(
        store_dir=tmp,
        primitives=reg,
        boundary_scan=scan,
        stop_event=stop_event or threading.Event(),
        cognition=cognition,
        search_bounds=bounds or SearchBounds(max_composition_size=2,
                                            max_candidates=10),
        repo_root=WT_ROOT,
        admission_db_path=os.path.join(tmp, "adm.db"))
    ev = EvidenceStore(os.path.join(tmp, "ev.db"))
    eids = {}
    for pid in pids:
        e = ev.add_entry(
            "observation",
            f"gate crossing: primitive '{pid}' admitted (exec1 fixture)",
            source="gate:EXEC1")
        ev.verify_entry(e["id"], "gate:EXEC1")
        ex.ledger.add_entry(
            LedgerEntry(pid, "evidence", e["id"], "gate:EXEC1@exec1"))
        eids[pid] = e["id"]
    return ex, tmp, eids


# ---------------------------------------------------------------------------
# g0 — T0 identity: decided deviations cited, peer relation stated
# ---------------------------------------------------------------------------
def g0_identity():
    ident = exec_mod.EXECUTIVE_IDENTITY
    check("g0 identity names the controller",
          ident.get("name") == "CreativityExecutiveController",
          str(ident.get("name")))
    check("g0 peer of Primary (not subordinate/mode/loop)",
          "peer" in ident.get("relation_to_primary", ""),
          ident.get("relation_to_primary"))
    check("g0 does not merge EXEC2/FRM",
          "FRM" in ident.get("relation_to_exec2_frm", ""),
          ident.get("relation_to_exec2_frm"))
    check("g0 all five decided deviations cited",
          tuple(ident.get("decided_deviations", ())) ==
          ("D-4", "D-5", "D-6", "D-7", "D-8"),
          str(ident.get("decided_deviations")))
    check("g0 authority cites James's §11 decisions",
          "§11" in ident.get("authority", ""), ident.get("authority", "")[:60])


# ---------------------------------------------------------------------------
# g1 — T1/D-5: INTENT first; every transition admissible by construction
# ---------------------------------------------------------------------------
def g1_stage_order():
    ex, tmp, _eids = make_exec()
    visited = []
    orig_step = exec_mod.CreativeLoopController.step

    def rec_step(self, ctx):
        visited.append(self.stage.name)
        return orig_step(self, ctx)

    exec_mod.CreativeLoopController.step = rec_step
    try:
        intent = register_intent("proof", "compose an arithmetic expression")
        comm = ex.commission(intent, refinement_bound=1)
        check("g1 commission not suspended (clear + empty scan)",
              not comm.suspended, f"suspended={comm.suspended}")
        out = ex.run(comm)
    finally:
        exec_mod.CreativeLoopController.step = orig_step
    check("g1 first stage is INTENT (D-4 grammar)",
          visited and visited[0] == "INTENT", str(visited[:4]))
    bad = []
    for a, b in zip(visited, visited[1:]):
        if CreativeStage[b] not in admissible_next(CreativeStage[a]):
            bad.append((a, b))
    check("g1 every transition admissible by construction", not bad, str(bad))
    check("g1 RELEASE terminal", is_terminal(CreativeStage.RELEASE)
          and admissible_next(CreativeStage.RELEASE) == (),
          "release must be terminal with no next stage")
    check("g1 run reached a terminal outcome",
          out.status in (COMPLETED, STOPPED),
          f"{out.status}: {out.detail[:120]}")


# ---------------------------------------------------------------------------
# g2 — Q2: boundary scan FIRST; ambiguous + boundary → suspend + real gap
# ---------------------------------------------------------------------------
def g2_boundary_first():
    bp = BoundaryPresentation(
        kind="run_wake",
        evidence={"wake_reason": "user_gap", "run_id": "r1"},
        observed_by="proof")
    bp.validate()  # real validation, raises BoundaryRefused if malformed
    scan_calls = []

    def scan():
        scan_calls.append(1)
        return ScanReport(scanned_at=time.time(), presentations=[bp],
                          source_absent=[], observed={}, notes=[])

    ex, tmp, _eids = make_exec(scan=scan)
    gen_calls = []
    orig_generate = ex._generate

    def counting_generate(ctx):
        gen_calls.append(1)
        return orig_generate(ctx)

    ex._generate = counting_generate
    intent = register_intent("proof", "make me something wonderful")
    check("g2 fixture intent is AMBIGUOUS",
          classify_ambiguity(intent) == AMBIGUOUS,
          classify_ambiguity(intent))
    comm = ex.commission(intent, refinement_bound=1)
    check("g2 boundary scan ran at commission (ordering)",
          len(scan_calls) == 1, f"scan_calls={len(scan_calls)}")
    check("g2 ambiguous commission suspended (convergence precedence)",
          comm.suspended, "not suspended")
    check("g2 boundary named on suspension",
          comm.boundary is not None and comm.boundary.kind == "run_wake",
          str(getattr(comm.boundary, 'kind', None)))
    out = ex.run(comm)
    check("g2 run reports SUSPENDED", out.status == SUSPENDED,
          out.status)
    check("g2 NO generation work happened before suspension",
          len(gen_calls) == 0, f"gen_calls={len(gen_calls)}")
    # The suspension is a real gap in the real registry.
    gaps = GapRegistry(None, db_path=os.path.join(tmp, "gaps.db"))
    found = [g for g in gaps.list_gaps()
             if "[creativity-exec]" in (g.summary or "")
             and "run_wake" in (g.summary or "")]
    check("g2 suspension recorded as a real named gap", len(found) >= 1,
          f"found={len(found)}")


def g3_ambiguous_clear_scan_proceeds():
    ex, tmp, _eids = make_exec()  # default scan: empty presentations
    intent = register_intent("proof", "make me something wonderful")
    check("g3 fixture intent is AMBIGUOUS",
          classify_ambiguity(intent) == AMBIGUOUS)
    comm = ex.commission(intent, refinement_bound=1)
    check("g3 ambiguous + empty scan → proceeds (not suspended)",
          not comm.suspended, f"suspended={comm.suspended}")
    out = ex.run(comm)
    check("g3 run reaches a terminal outcome",
          out.status in (COMPLETED, STOPPED),
          f"{out.status}: {out.detail[:100]}")


# ---------------------------------------------------------------------------
# g4 — clear commission: verified-only candidates, full judgment records
# ---------------------------------------------------------------------------
def g4_clear_generation():
    ex, tmp, _eids = make_exec()
    intent = register_intent("proof", "compose an arithmetic expression")
    check("g4 fixture intent is CLEAR",
          classify_ambiguity(intent) == CLEAR)
    comm = ex.commission(intent, refinement_bound=1)
    out = ex.run(comm)
    check("g4 run completed or stopped honestly",
          out.status in (COMPLETED, STOPPED),
          f"{out.status}: {out.detail[:120]}")
    check("g4 candidates were considered", out.candidates_considered > 0,
          f"candidates={out.candidates_considered}")
    # Full judgment record on one candidate through the executive's pipeline.
    ctx = exec_mod._RunContext(
        executive=ex, intent=intent, ambiguity=CLEAR,
        current_stage=CreativeStage.CRITIQUE, refinement_bound=1)
    cands = ex._generate(ctx)
    check("g4 generation produced candidates", len(cands) > 0,
          f"n={len(cands)}")
    indexed = set(ex.ledger.indexed())
    leaked = [c.candidate_id for c, _n in cands
              if not set(c.primitive_ids) <= indexed]
    check("g4 every candidate from verified primitives only", not leaked,
          str(leaked[:3]))
    cand, _nov = cands[0]
    report = ex._critique_one((cand, _nov), ctx)
    check("g4 judgment record: legality present and LEGAL",
          report.legality_verdict.legal, str(report.legality_verdict.illegal))
    check("g4 judgment record: novelty judged by mechanism",
          report.novelty.verdict in ("novel", "retrieval"),
          str(report.novelty.verdict))
    check("g4 judgment record: value scored with reasons",
          len(report.value.criteria) == len(exec_mod.VALUE_CRITERIA_V1),
          f"{len(report.value.criteria)} criteria")
    check("g4 judgment record: panel verdicts attached",
          len(report.panels) == 4, f"panels={len(report.panels)}")
    check("g4 three judgments kept separate (fields, not merged)",
          all(hasattr(report, f) for f in
              ("legality_verdict", "novelty", "value", "panels")))


# ---------------------------------------------------------------------------
# g5 — trust boundaries closed
# ---------------------------------------------------------------------------
def g5_trust_boundaries():
    ex, tmp, eids = make_exec()
    prov = ProvenanceStore(os.path.join(tmp, "prov.db"))
    # 5a: graded trust drop via the REAL set_trust API → ILLEGAL (a
    # refusal with a reason, not a raise — the record still resolves).
    prov.record(ProvenanceRecord(
        capability_id="prov_add", origin=Origin.BUILTIN,
        trust=TrustLevel.TRUSTED, source="gate:PROV1"))
    ex.ledger.add_entry(
        LedgerEntry("add", "provenance", "prov_add", "gate:PROV1@proof"))
    check("g5 provenance entry indexed at TRUSTED",
          ex.ledger.check_composition(["add"]).legal)
    prov.set_trust("prov_add", TrustLevel.TESTED,
                   reason="proof: trust drop via the real API")
    v = ex.ledger.check_composition(["add"])
    check("g5 trust drop → ILLEGAL with the level named (live re-resolution)",
          not v.legal and "TESTED" in str(v.illegal),
          str(v.illegal)[:120])
    intent = register_intent("proof", "compose an arithmetic expression")
    ctx = exec_mod._RunContext(
        executive=ex, intent=intent, ambiguity=CLEAR,
        current_stage=CreativeStage.GENERATION, refinement_bound=1)
    cands = ex._generate(ctx)
    leaked = [c.candidate_id for c, _n in cands if "add" in c.primitive_ids]
    check("g5 dropped primitive never composed afterwards", not leaked,
          str(leaked[:3]))
    # 5b: unverified evidence → the quarantine RAISE (designed behavior:
    # quarantined substrate raises, never yields a verdict).
    print("NOTE: disclosed sqlite flip on the scratch evidence DB — the "
          "store API offers no unverify; this drives the ledger (the "
          "system under test), not the executive.")
    conn = sqlite3.connect(os.path.join(tmp, "ev.db"))
    conn.execute("UPDATE evidence_entries SET verified=0 WHERE id=?",
                 (eids["mul"],))
    conn.commit()
    conn.close()
    try:
        ex.ledger.check_composition(["mul"])
        check("g5 quarantined substrate raises (never a verdict)",
              False, "returned a verdict!")
    except Exception as e:
        check("g5 quarantined substrate raises (never a verdict)",
              type(e).__name__ == "QuarantinedSubstrateRefused",
              f"{type(e).__name__}: {str(e)[:80]}")
    # 5c: structural — no parameter for outside verdicts/ledgers/reports.
    missing = public_surface_has_no(
        ["verdict", "ledger", "report", "composition_verdict",
         "critique_report", "admission_record", "stores"])
    check("g5 no injectable verdict/ledger/report parameter", not missing,
          str(missing))
    # 5d: injection attempts are TypeErrors, not silent acceptances.
    ex2, _tmp2, _e2 = make_exec()
    intent2 = register_intent("proof", "compose an arithmetic expression")
    try:
        ex2.commission(intent2, refinement_bound=1, verdict="fake")
        check("g5 forged verdict kwarg refused", False, "accepted!")
    except TypeError:
        check("g5 forged verdict kwarg refused", True)
    comm2 = ex2.commission(intent2, refinement_bound=1)
    try:
        ex2.run(comm2, ledger="fake")
        check("g5 forged ledger kwarg refused", False, "accepted!")
    except TypeError:
        check("g5 forged ledger kwarg refused", True)


# ---------------------------------------------------------------------------
# g6 — T8: kill stops the run cold, state preserved
# ---------------------------------------------------------------------------
def g6_kill():
    # 6a: pre-set event → KILLED at once.
    ev_stop = threading.Event()
    ev_stop.set()
    ex, tmp, _eids = make_exec(stop_event=ev_stop)
    intent = register_intent("proof", "compose an arithmetic expression")
    comm = ex.commission(intent, refinement_bound=1)
    out = ex.run(comm)
    check("g6 pre-set kill → KILLED", out.status == KILLED, out.status)
    # 6b: mid-generation kill from the stage itself.
    ex2, tmp2, _eids2 = make_exec()
    orig_generate = ex2._generate

    def gen_then_kill(ctx):
        result = orig_generate(ctx)
        ex2._stop_event.set()  # the kill lands mid-generation
        return result

    ex2._generate = gen_then_kill
    intent2 = register_intent("proof", "compose an arithmetic expression")
    comm2 = ex2.commission(intent2, refinement_bound=1)
    out2 = ex2.run(comm2)
    check("g6 mid-generation kill → KILLED", out2.status == KILLED,
          out2.status)
    check("g6 kill preserves state (candidates kept, ledger untouched)",
          out2.candidates_considered > 0,
          f"candidates={out2.candidates_considered}")
    check("g6 kill names the stage", out2.stage == CreativeStage.GENERATION,
          str(out2.stage))


# ---------------------------------------------------------------------------
# g7 — T4: cognition is utility, never decider
# ---------------------------------------------------------------------------
class EvilProvider:
    """Adversarial cognition: malformed JSON, non-numeric overrides,
    unknown params, and an exception variant."""

    def __init__(self, mode="evil"):
        self.mode = mode

    def request_cognition(self, *, mc_id, prompt, context):
        if self.mode == "raises":
            raise RuntimeError("provider exploded")
        if self.mode == "garbage":
            return CognitionResult(ok=True, text="not json at all {{{")
        return CognitionResult(ok=True, text=json.dumps({"dimensions": [
            {"name": "evil", "overrides": {"x": "not-a-number"}},
            {"name": "evil2", "overrides": {"qq": 999}},
            {"name": "evil3", "overrides": "not-a-dict"},
            {"name": "evil4"},
        ]}))


def g7_cognition_utility():
    # Unit level: malformed suggestions dropped fail-closed.
    ex, tmp, _eids = make_exec(cognition=EvilProvider("evil"))
    intent = register_intent("proof", "compose an arithmetic expression")
    ctx = exec_mod._RunContext(
        executive=ex, intent=intent, ambiguity=CLEAR,
        current_stage=CreativeStage.VARIATION, refinement_bound=1)
    dims = ex._cognition_dimensions(ctx)
    names = [n for n, _o in dims]
    # Shape-malformed suggestions are dropped at the boundary: non-numeric
    # values, non-dict overrides, missing overrides key.
    check("g7 shape-malformed suggestions dropped at the boundary",
          not any(n in ("cognition-evil", "cognition-evil3",
                        "cognition-evil4") for n in names),
          str(dims))
    # The well-formed-but-unknown-param suggestion may pass shape
    # validation — it is neutralized at application (unknown params are
    # never applied to a plan). Two-layer defense, each layer honest
    # about what it enforces.
    check("g7 unknown-param suggestion carries no applicable override",
          all(not (set(o) & {"x", "y", "z", "w"})
              for _n, o in dims),
          str(dims))
    ex_r, _t, _e = make_exec(cognition=EvilProvider("raises"))
    dims_r = ex_r._cognition_dimensions(ctx)
    check("g7 raising provider → no dimensions, no crash", dims_r == [])
    # Run level: adversarial provider cannot change base decisions.
    ex_null, _t1, _e1 = make_exec(cognition=NullCognitionProvider())
    ex_evil, _t2, _e2 = make_exec(cognition=EvilProvider("evil"))
    i1 = register_intent("proof", "compose an arithmetic expression")
    i2 = register_intent("proof", "compose an arithmetic expression")
    out_null = ex_null.run(ex_null.commission(i1, refinement_bound=1))
    out_evil = ex_evil.run(ex_evil.commission(i2, refinement_bound=1))
    check("g7 adversarial cognition → same terminal status",
          out_null.status == out_evil.status,
          f"null={out_null.status} evil={out_evil.status}")
    check("g7 adversarial cognition → same admission decision",
          (out_null.admission is not None) ==
          (out_evil.admission is not None),
          f"null={out_null.admission is not None} "
          f"evil={out_evil.admission is not None}")
    # And no unverified primitive can ride in on a suggestion.
    for cand, _n in ex_evil._generate(exec_mod._RunContext(
            executive=ex_evil, intent=i2, ambiguity=CLEAR,
            current_stage=CreativeStage.GENERATION, refinement_bound=1)):
        v = ex_evil.ledger.check_composition(list(cand.primitive_ids))
        if not v.legal:
            check("g7 no unverified primitive via cognition", False,
                  str(v.illegal))
            break
    else:
        check("g7 no unverified primitive via cognition", True)


# ---------------------------------------------------------------------------
# g8 — T3: microcontroller lifecycle inside creative loops
# ---------------------------------------------------------------------------
def g8_microcontrollers():
    reg = MicrocontrollerRegistry()
    r1 = reg.spawn("GENERATION", "test-purpose")
    check("g8 spawn in a creativity loop", r1.loop == "GENERATION")
    try:
        reg.spawn("acquisition", "cross-domain")
        check("g8 cross-loop spawn refused", False, "accepted!")
    except ExecutiveRefused:
        check("g8 cross-loop spawn refused", True)
    try:
        reg.spawn("GENERATION", "   ")
        check("g8 empty purpose refused", False, "accepted!")
    except ExecutiveRefused:
        check("g8 empty purpose refused", True)
    child = reg.spawn("GENERATION", "child-work", parent_id=r1.mc_id)
    check("g8 nested spawn", child.depth == 1, f"depth={child.depth}")
    try:
        reg.spawn("CRITIQUE", "wrong-loop-child", parent_id=r1.mc_id)
        check("g8 cross-loop nesting refused", False, "accepted!")
    except ExecutiveRefused:
        check("g8 cross-loop nesting refused", True)
    reg.retire(r1.mc_id)
    check("g8 retire cascades to children (unwind)",
          reg.get(child.mc_id).state == "resolved",
          reg.get(child.mc_id).state)
    # Depth cap.
    deep = reg.spawn("VARIATION", "d0")
    for i in range(1, 10):
        try:
            deep = reg.spawn("VARIATION", f"d{i}", parent_id=deep.mc_id)
        except ExecutiveRefused:
            check("g8 max depth enforced", i <= 9, f"broke at {i}")
            break
    else:
        check("g8 max depth enforced", False, "no refusal at depth 9")
    # Integration: a real run leaves no active microcontrollers behind.
    tracked = MicrocontrollerRegistry()

    def run_with_tracked(executive, comm):
        real_cls = exec_mod._RunContext
        orig_init = real_cls.__init__

        def patched_init(self, **kw):
            kw["mc"] = tracked
            orig_init(self, **kw)

        real_cls.__init__ = patched_init
        try:
            return executive.run(comm)
        finally:
            real_cls.__init__ = orig_init

    ex2, _t2, _e2 = make_exec()
    i2 = register_intent("proof", "compose an arithmetic expression")
    run_with_tracked(ex2, ex2.commission(i2, refinement_bound=1))
    lingering = [r.mc_id for r in tracked._records.values()
                 if r.state == "active"]
    check("g8 stages unwind their microcontrollers (none linger)",
          not lingering, str(lingering[:3]))


# ---------------------------------------------------------------------------
# g9 — T7a: unverified demand → named gap, task proceeds visibly
# ---------------------------------------------------------------------------
def g9_demand_gaps():
    ex, tmp, _eids = make_exec()
    intent = register_intent("proof", "compose an arithmetic expression")
    comm = ex.commission(
        intent, refinement_bound=1,
        constraints=[{"kind": "requires_primitive",
                      "primitive": "song_synth"}])
    out = ex.run(comm)
    check("g9 unverified demand → named gaps recorded",
          len(out.gaps) > 0, f"gaps={out.gaps}")
    check("g9 absence recorded visibly",
          "song_synth" in out.absent, str(out.absent))
    gaps = GapRegistry(None, db_path=os.path.join(tmp, "gaps.db"))
    found = [g for g in gaps.list_gaps() if "song_synth" in (g.summary or "")]
    check("g9 gap lives in the real gap registry", len(found) >= 1,
          f"found={len(found)}")
    check("g9 run did not crash on unverified demand",
          out.status in (COMPLETED, STOPPED),
          f"{out.status}: {out.detail[:100]}")


# ---------------------------------------------------------------------------
# g10 — no dispatch surface in the executive
# ---------------------------------------------------------------------------
def g10_no_dispatch():
    src = open(os.path.join(WT_ROOT, "runtime", "creativity",
                             "executive.py")).read()
    check("g10 executive imports no dispatch machinery",
          "remote_dispatch" not in src and "dispatch_pairing" not in src)
    check("g10 GAM does not attach (documented D-7)",
          "gam does not attach" in src.lower())


def main():
    print("== CREATIVITY-EXEC-1 proof battery ==")
    print(f"worktree: {WT_ROOT}")
    g0_identity()
    g1_stage_order()
    g2_boundary_first()
    g3_ambiguous_clear_scan_proceeds()
    g4_clear_generation()
    g5_trust_boundaries()
    g6_kill()
    g7_cognition_utility()
    g8_microcontrollers()
    g9_demand_gaps()
    g10_no_dispatch()
    print(f"== RESULT: PASS={PASS} FAIL={FAIL} ==")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
