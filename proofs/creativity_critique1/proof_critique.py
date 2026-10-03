#!/usr/bin/env python3
"""CREATIVITY-CRITIQUE-1 proof battery.

Runs in a fresh process (see gate_run.sh). All fixtures are REAL records
in scratch sqlite stores created by this driver via the stores' own public
APIs: EvidenceStore.add_entry/verify_entry, ProvenanceStore.record/
set_trust, GapRegistry.register. The composer is the REAL
swarm_engine.synthesis.composer.Composer, the registry the REAL
PrimitiveRegistry, the panels the REAL build_panels() from
runtime.core.acceptance_panels. Nothing is asserted into existence.

One disclosed exception: the provenance store API offers no delete
operation, so the record-deletion adversarial case removes the row via
sqlite directly on the scratch DB. This drives the CRITIQUE (the system
under test) into the state; the critique's response is what is measured.
The case prints a NOTE saying so.
"""
import copy
import os
import sqlite3
import subprocess
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.environ.get("WT_ROOT", os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..")))
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root

import runtime.creativity.critique as critique_mod  # noqa: E402
from runtime.creativity.critique import (  # noqa: E402
    NOVEL,
    RETRIEVAL,
    VALUE_CRITERIA_V1,
    CandidateComposition,
    CritiqueRefused,
    CritiqueReport,
    check_novelty,
    critique_candidate,
    critique_value,
    submit_to_panels,
)
from runtime.creativity.intent import register_intent  # noqa: E402
from runtime.creativity.ledger import (  # noqa: E402
    Ledger,
    LedgerEntry,
)
from runtime.governance.provenance import (  # noqa: E402
    Origin,
    ProvenanceRecord,
    ProvenanceStore,
    TrustLevel,
)
from runtime.services.evidence import EvidenceStore  # noqa: E402
from swarm_engine.primitives.core import (  # noqa: E402
    NUM,
    Effect,
    PrimitiveRegistry,
)
from swarm_engine.synthesis.composer import Composer  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}", flush=True)
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}", flush=True)


def expect_raises(name, exc, fn, must_contain=""):
    try:
        fn()
    except exc as e:
        check(name, must_contain in str(e),
              f"message missing {must_contain!r}: {e}")
        return
    except Exception as e:  # noqa: BLE001
        check(name, False, f"wrong exception {type(e).__name__}: {e}")
        return
    check(name, False, "no exception raised")


PLAN = {
    "steps": [
        {"id": "s1", "op": "add",
         "args": {"a": {"$param": "x"}, "b": {"$param": "y"}}},
        {"id": "s2", "op": "mul",
         "args": {"a": {"$step": "s1"}, "b": {"$param": "z"}}},
    ],
    "output": {"$step": "s2"},
    "params": {"x": "num", "y": "num", "z": "num"},
}
ARGS = {"x": 2, "y": 3, "z": 4}  # (2+3)*4 = 20
HELD_OUT = (
    {"args": {"x": 1, "y": 1, "z": 2}, "expected": 4},
    {"args": {"x": 0, "y": 5, "z": 3}, "expected": 15},
)
CONSTRAINTS = (
    {"kind": "requires_primitive", "primitive": "add"},
    {"kind": "max_steps", "n": 5},
    {"kind": "exact_outcome", "value": 20},
    {"kind": "excludes_family", "family": "effect"},
)


class World:
    """One isolated fixture world: scratch stores, real machinery."""


def make_world():
    w = World()
    w.tmp = tempfile.TemporaryDirectory()
    w.evidence = EvidenceStore(os.path.join(w.tmp.name, "ev.db"))
    w.provenance = ProvenanceStore(os.path.join(w.tmp.name, "prov.db"))
    w.registry = PrimitiveRegistry()

    @w.registry.define("add", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                       effects=(Effect.PURE,))
    def _add(a, b):
        return a + b

    @w.registry.define("mul", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                       effects=(Effect.PURE,))
    def _mul(a, b):
        return a * b

    @w.registry.define("sub", "arithmetic", {"a": NUM, "b": NUM}, NUM,
                       effects=(Effect.PURE,))
    def _sub(a, b):
        return a - b

    w.composer = Composer(w.registry)
    w.ledger = Ledger(w.evidence, w.provenance,
                      gap_db_path=os.path.join(w.tmp.name, "gaps.db"),
                      engine=None)
    for pid in ("add", "mul", "sub"):
        e = w.evidence.add_entry(
            "observation",
            f"gate crossing: primitive '{pid}' admitted (proof fixture)",
            source="gate:PROOF")
        w.evidence.verify_entry(e["id"], "gate:PROOF")
        w.ledger.add_entry(LedgerEntry(pid, "evidence", e["id"],
                                       "gate:PROOF@proof"))
    w.intent = register_intent("proof", "compute (x+y)*z")
    return w


def make_candidate(cid="cand-1", pids=("add", "mul"), claimed=20,
                   constraints=CONSTRAINTS, held_out=HELD_OUT,
                   artifacts=(), addresses="compute (x+y)*z",
                   args=None):
    return CandidateComposition(
        candidate_id=cid,
        primitive_ids=tuple(pids),
        plan=copy.deepcopy(PLAN),
        args=dict(args or ARGS),
        claimed_outcome=claimed,
        held_out=tuple(held_out),
        file_artifacts=tuple(artifacts),
        addresses=addresses,
        constraints=tuple(constraints),
        run_id="proof-run-1",
    )


def legal_verdict(w, pids):
    v = w.ledger.check_composition(list(pids))
    assert v.legal, f"fixture verdict should be legal: {v.illegal}"
    return v


# ---------------------------------------------------------------------------
# t01: legality gate — ILLEGAL verdict refused BEFORE any stage runs
# ---------------------------------------------------------------------------
w = make_world()
v_illegal = w.ledger.check_composition(["add", "ghost"])
check("t01 fixture: verdict is ILLEGAL", not v_illegal.legal)
cand = make_candidate(pids=("add", "ghost"))


class SpyProvenance:
    def __init__(self, real):
        self._real = real
        self.calls = 0

    def list_by_trust(self, *a, **k):
        self.calls += 1
        return self._real.list_by_trust(*a, **k)


class SpyComposer:
    def __init__(self, real):
        self._real = real
        self.calls = 0

    def execute_sync(self, *a, **k):
        self.calls += 1
        return self._real.execute_sync(*a, **k)


spy_prov = SpyProvenance(w.provenance)
spy_comp = SpyComposer(w.composer)
panel_calls = []
orig_build_panels = critique_mod.build_panels


def _counting_build_panels():
    panel_calls.append(1)
    return orig_build_panels()


critique_mod.build_panels = _counting_build_panels
try:
    expect_raises(
        "t01 ILLEGAL verdict refused before any stage",
        CritiqueRefused,
        lambda: critique_candidate(
            verdict=v_illegal, intent=w.intent, candidate=cand,
            provenance_store=spy_prov, composer=spy_comp,
            primitives=w.registry, repo_root=WT_ROOT),
        must_contain="ILLEGAL")
finally:
    critique_mod.build_panels = orig_build_panels
check("t01 novelty stage never ran", spy_prov.calls == 0,
      f"list_by_trust called {spy_prov.calls}x")
check("t01 value stage never ran", spy_comp.calls == 0,
      f"execute_sync called {spy_comp.calls}x")
check("t01 panel stage never ran", panel_calls == [],
      f"build_panels called {len(panel_calls)}x")

# ---------------------------------------------------------------------------
# t02: verdict must cover exactly this candidate
# ---------------------------------------------------------------------------
w = make_world()
v_narrow = legal_verdict(w, ["add"])
cand_wide = make_candidate(pids=("add", "mul"))
expect_raises(
    "t02 LEGAL verdict for a different composition refused",
    CritiqueRefused,
    lambda: critique_candidate(
        verdict=v_narrow, intent=w.intent, candidate=cand_wide,
        provenance_store=w.provenance, composer=w.composer,
        primitives=w.registry, repo_root=WT_ROOT),
    must_contain="does not cover this candidate")

# ---------------------------------------------------------------------------
# t03/t04/t05: novelty by mechanism
# ---------------------------------------------------------------------------
w = make_world()
w.provenance.record(ProvenanceRecord(
    capability_id="prior-comp-1", origin=Origin.SYNTHESIZED,
    trust=TrustLevel.TRUSTED, source="gate:PRIOR",
    primitives_used=["add", "mul"]))
nv = check_novelty(make_candidate(), w.provenance)
check("t03 exact combination -> RETRIEVAL",
      nv.verdict == RETRIEVAL and nv.matched_capability_id == "prior-comp-1",
      f"got {nv}")
nv_perm = check_novelty(make_candidate(pids=("mul", "add")), w.provenance)
check("t03 trivially permuted combination -> RETRIEVAL",
      nv_perm.verdict == RETRIEVAL
      and nv_perm.matched_capability_id == "prior-comp-1",
      f"got {nv_perm}")
check("t03 retrieval names its reason",
      bool(nv.reason) and "prior-comp-1" in nv.reason,
      f"reason={nv.reason!r}")

nv_new = check_novelty(make_candidate(pids=("add", "sub")), w.provenance)
check("t04 new combination -> NOVEL",
      nv_new.verdict == NOVEL and nv_new.matched_capability_id is None,
      f"got {nv_new}")

w.provenance.record(ProvenanceRecord(
    capability_id="prior-comp-2", origin=Origin.SYNTHESIZED,
    trust=TrustLevel.TESTED, source="gate:PRIOR",
    primitives_used=["sub", "mul"]))
nv_unver = check_novelty(make_candidate(pids=("sub", "mul")), w.provenance)
check("t05 matching combo without intact verification -> NOVEL",
      nv_unver.verdict == NOVEL, f"got {nv_unver}")
check("t05 unverified prior recorded as context, not retrieval",
      ("prior-comp-2", "TESTED") in nv_unver.prior_unverified,
      f"prior_unverified={nv_unver.prior_unverified}")

# ---------------------------------------------------------------------------
# t06/t07/t08: value critique
# ---------------------------------------------------------------------------
w = make_world()
vs = critique_value(make_candidate(), w.intent, w.composer, w.registry)
check("t06 value satisfied on good candidate", vs.satisfied, f"got {vs}")
check("t06 all four v1 criteria present",
      [c.criterion for c in vs.criteria]
      == ["executes", "outcome_reproduces", "intent_addressed",
          "constraints_satisfied"],
      f"got {[c.criterion for c in vs.criteria]}")
check("t06 every criterion carries a reason",
      all(c.reason for c in vs.criteria))
check("t06 criteria set is the documented Q7 v1 set",
      set(VALUE_CRITERIA_V1) == {c.criterion for c in vs.criteria})

vs_bad = critique_value(
    make_candidate(constraints=(
        {"kind": "requires_primitive", "primitive": "sub"},)),
    w.intent, w.composer, w.registry)
con = next(c for c in vs_bad.criteria
           if c.criterion == "constraints_satisfied")
check("t07 missing stated constraint scores down",
      not vs_bad.satisfied and not con.passed, f"got {vs_bad}")
check("t07 reason names the missing primitive",
      "'sub'" in con.reason, f"reason={con.reason!r}")

vs_wrong = critique_value(make_candidate(claimed=999), w.intent,
                          w.composer, w.registry)
rep = next(c for c in vs_wrong.criteria
           if c.criterion == "outcome_reproduces")
check("t08 wrong claimed outcome fails outcome_reproduces",
      not vs_wrong.satisfied and not rep.passed, f"got {vs_wrong}")
check("t08 reason shows observed vs claimed",
      "20" in rep.reason and "999" in rep.reason,
      f"reason={rep.reason!r}")

# ---------------------------------------------------------------------------
# t09: panels — real pass case, all four panels green
# ---------------------------------------------------------------------------
w = make_world()
# The scratch artifact lives directly in the tracked proofs/ dir (not in an
# untracked subdir): git then lists the file individually in --porcelain,
# which is what the provenance panel's session-attributable check reads.
# proofs/creativity_critique1/ itself is untracked, so git would collapse
# the whole directory to one line and the file would not resolve.
artifact_path = os.path.join(WT_ROOT, "proofs", "_proof_artifact_c1.txt")
with open(artifact_path, "w") as f:
    f.write("critique proof artifact\n")
try:
    verdicts = submit_to_panels(make_candidate(artifacts=(artifact_path,)),
                                w.composer, w.registry, WT_ROOT)
finally:
    os.remove(artifact_path)
check("t09 four real panel verdicts attached",
      len(verdicts) == 4, f"got {len(verdicts)}")
check("t09 panel order is honesty/correctness/safety/provenance",
      [v.panel for v in verdicts]
      == ["honesty", "correctness", "safety", "provenance"],
      f"got {[v.panel for v in verdicts]}")
check("t09 all four panels pass on the good case",
      all(v.passed for v in verdicts),
      f"got {[(v.panel, v.passed, v.reason) for v in verdicts]}")
check("t09 verdicts carry evidence, not bare booleans",
      all(isinstance(v.evidence, dict) and v.reason for v in verdicts))

# ---------------------------------------------------------------------------
# t10/t11/t12: panels — genuine fail cases from the real panels
# ---------------------------------------------------------------------------
w = make_world()
v_lie = submit_to_panels(make_candidate(claimed=9999), w.composer,
                         w.registry, WT_ROOT)
hon = next(v for v in v_lie if v.panel == "honesty")
check("t10 honesty panel really fails a confabulated claim",
      not hon.passed, f"got passed={hon.passed}")
check("t10 honesty failure names the divergence",
      "diverged" in hon.reason or "confabulated" in hon.reason,
      f"reason={hon.reason!r}")
check("t10 honesty evidence shows expected vs observed",
      hon.evidence.get("expected") == 9999
      and hon.evidence.get("observed") == 20,
      f"evidence={hon.evidence}")

committed = os.path.join(WT_ROOT, "runtime", "creativity", "stages.py")
dirty = subprocess.run(
    ["git", "status", "--porcelain"], cwd=WT_ROOT,
    capture_output=True, text=True, timeout=30).stdout
check("t11 fixture precondition: stages.py is committed and clean",
      "runtime/creativity/stages.py" not in dirty,
      "fixture invalid — stages.py is dirty in this worktree")
v_prov = submit_to_panels(make_candidate(artifacts=(committed,)),
                          w.composer, w.registry, WT_ROOT)
prov = next(v for v in v_prov if v.panel == "provenance")
check("t11 provenance panel really fails re-attributed work",
      not prov.passed, f"got passed={prov.passed}")
check("t11 provenance failure names the cause",
      "not session-attributable" in prov.reason,
      f"reason={prov.reason!r}")

w = make_world()
e = w.evidence.add_entry(
    "observation",
    "gate crossing: primitive 'ghost_prim' admitted (proof fixture)",
    source="gate:PROOF")
w.evidence.verify_entry(e["id"], "gate:PROOF")
w.ledger.add_entry(LedgerEntry("ghost_prim", "evidence", e["id"],
                               "gate:PROOF@proof"))
v_ghost = legal_verdict(w, ["add", "ghost_prim"])
cand_ghost = make_candidate(pids=("add", "ghost_prim"))
v_safe = submit_to_panels(cand_ghost, w.composer, w.registry, WT_ROOT)
saf = next(v for v in v_safe if v.panel == "safety")
check("t12 fixture: ghost_prim is ledger-LEGAL but not in the registry",
      v_ghost.legal and w.registry.resolve("ghost_prim") is None)
check("t12 safety panel really fails an unresolvable primitive",
      not saf.passed, f"got passed={saf.passed}")
check("t12 safety failure names the primitive",
      "ghost_prim" in saf.reason, f"reason={saf.reason!r}")

# ---------------------------------------------------------------------------
# t13/t14: adversarial — verification no longer intact
# ---------------------------------------------------------------------------
w = make_world()
w.provenance.record(ProvenanceRecord(
    capability_id="prior-comp-3", origin=Origin.SYNTHESIZED,
    trust=TrustLevel.TRUSTED, source="gate:PRIOR",
    primitives_used=["add", "mul"]))
check("t13 precondition: seeded combination is a retrieval",
      check_novelty(make_candidate(), w.provenance).verdict == RETRIEVAL)
w.provenance.set_trust("prior-comp-3", TrustLevel.QUARANTINED,
                       "adversarial test: trust dropped after the fact")
nv_q = check_novelty(make_candidate(), w.provenance)
check("t13 trust dropped -> NOVEL (verification no longer intact)",
      nv_q.verdict == NOVEL, f"got {nv_q}")
check("t13 quarantined prior recorded as context",
      ("prior-comp-3", "QUARANTINED") in nv_q.prior_unverified,
      f"prior_unverified={nv_q.prior_unverified}")

w = make_world()
w.provenance.record(ProvenanceRecord(
    capability_id="prior-comp-4", origin=Origin.SYNTHESIZED,
    trust=TrustLevel.TRUSTED, source="gate:PRIOR",
    primitives_used=["add", "sub"]))
check("t14 precondition: seeded combination is a retrieval",
      check_novelty(
          make_candidate(pids=("add", "sub")), w.provenance).verdict
      == RETRIEVAL)
print("NOTE t14: the store API offers no delete; removing the row via "
      "sqlite directly on the scratch DB (disclosed).", flush=True)
conn = sqlite3.connect(os.path.join(w.tmp.name, "prov.db"))
conn.execute("DELETE FROM provenance WHERE capability_id='prior-comp-4'")
conn.commit()
conn.close()
nv_del = check_novelty(make_candidate(pids=("add", "sub")), w.provenance)
check("t14 deleted record -> NOVEL (not derivable from existing records)",
      nv_del.verdict == NOVEL, f"got {nv_del}")

# ---------------------------------------------------------------------------
# t15: separation — no judgment absorbs another
# ---------------------------------------------------------------------------
w = make_world()
rep_sep = critique_candidate(
    verdict=legal_verdict(w, ["add", "mul"]),
    intent=w.intent,
    candidate=make_candidate(claimed=9999),  # honesty will fail at panel
    provenance_store=w.provenance, composer=w.composer,
    primitives=w.registry, repo_root=WT_ROOT)
check("t15 report carries all three judgments separately",
      isinstance(rep_sep, CritiqueReport)
      and rep_sep.novelty.verdict in (NOVEL, RETRIEVAL)
      and isinstance(rep_sep.value.satisfied, bool)
      and len(rep_sep.panels) == 4)
hon_sep = next(v for v in rep_sep.panels if v.panel == "honesty")
check("t15 panel failure present and labeled",
      not hon_sep.passed and hon_sep.panel == "honesty")
check("t15 panel failure did not rewrite the novelty judgment",
      rep_sep.novelty.verdict == NOVEL, f"got {rep_sep.novelty}")
check("t15 panel failure did not rewrite the value judgment",
      rep_sep.value.satisfied is False
      and any(c.criterion == "outcome_reproduces" and not c.passed
              for c in rep_sep.value.criteria))

# ---------------------------------------------------------------------------
# t16: end-to-end — the objective's demonstrable end-state
# ---------------------------------------------------------------------------
w = make_world()
artifact_path = os.path.join(WT_ROOT, "proofs", "_proof_artifact_c1e2e.txt")
with open(artifact_path, "w") as f:
    f.write("critique e2e artifact\n")
try:
    rep_e2e = critique_candidate(
        verdict=legal_verdict(w, ["add", "mul"]),
        intent=w.intent,
        candidate=make_candidate(artifacts=(artifact_path,)),
        provenance_store=w.provenance, composer=w.composer,
        primitives=w.registry, repo_root=WT_ROOT)
finally:
    os.remove(artifact_path)
check("t16 end-to-end report complete",
      rep_e2e.legality_verdict.legal
      and rep_e2e.novelty.verdict == NOVEL
      and rep_e2e.value.satisfied
      and all(v.passed for v in rep_e2e.panels),
      f"got novelty={rep_e2e.novelty.verdict} "
      f"value={rep_e2e.value.satisfied} "
      f"panels={[(v.panel, v.passed) for v in rep_e2e.panels]}")
check("t16 intent carried into the report",
      rep_e2e.intent_outcome == "compute (x+y)*z")

print(f"\nCRITIQUE_PROOF_DONE pass={PASS} fail={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
