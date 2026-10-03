#!/usr/bin/env python3
"""CREATIVITY-RELEASE-1 proof battery.

Runs in a fresh process (see gate_run.sh). All fixtures are REAL records
in scratch sqlite stores created by this driver via the stores' own public
APIs: EvidenceStore.add_entry/verify_entry, ProvenanceStore.record,
GapRegistry via the ledger, AdmissionRegistry with an injected tmp db_path.
The composer is the REAL swarm_engine.synthesis.composer.Composer, the
registry the REAL PrimitiveRegistry, the panels the REAL build_panels(),
the ledger the REAL Ledger, the critique the REAL critique_candidate.
Nothing is asserted into existence.

The admission path under test (runtime.creativity.release.admit) takes
pipeline INPUTS and runs the pipeline itself — it accepts no
CompositionVerdict and no CritiqueReport object, so the forged-report
adversarial case is structural.

File artifacts cited to the panels are written INSIDE the worktree (the
provenance panel requires session-attributable, git-dirty paths) and
deleted at the end of the run; the driver asserts no fixture files remain.
"""
import copy
import inspect
import os
import sys
import tempfile
import zipfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.environ.get("WT_ROOT", os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..")))
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root

from runtime.creativity.critique import (  # noqa: E402
    CandidateComposition,
    CritiqueRefused,
)
from runtime.creativity.intent import register_intent  # noqa: E402
from runtime.creativity.ledger import (  # noqa: E402
    Ledger,
    LedgerEntry,
)
from runtime.creativity.release import (  # noqa: E402
    ADMISSION_CRITERIA_V1,
    AdmissionRecord,
    AdmissionRefused,
    AdmissionRegistry,
    _verify_file_artifact,
    admit,
    record_removal,
    run_release_flow,
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
    STR,
    Effect,
    PrimitiveRegistry,
)
from swarm_engine.synthesis.composer import Composer  # noqa: E402

PASS = 0
FAIL = 0
CREATED_FILES = []


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

    @w.registry.define("write_text", "io",
                       {"path": STR, "content": STR}, STR,
                       effects=(Effect.WRITE_FS,))
    def _write_text(path, content):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    w.composer = Composer(w.registry)
    # The sandbox is deny-by-default: the file-writing primitive needs an
    # explicit, narrowly-scoped grant (as an operator would issue). The
    # denial without it is the sandbox working, not a test obstacle.
    # The fixture files live in runtime/creativity/ (a tracked directory)
    # because the provenance panel's git-dirty check compares exact
    # relative paths against `git status --porcelain`, which collapses
    # fully-untracked directories to a single entry -- a file under
    # proofs/creativity_release1/ (fully untracked pre-landing) is
    # misclassified as not session-attributable. That collapsing blind
    # spot is a real defect in the panel (named in the mission report for
    # the owning track); the fixture placement works around it without
    # weakening the check -- the file genuinely is session work.
    w.registry.governor.grant(
        Effect.WRITE_FS,
        os.path.join(WT_ROOT, "runtime", "creativity") + os.sep + "*",
        note="proof fixture: session files only")
    w.ledger = Ledger(w.evidence, w.provenance,
                      gap_db_path=os.path.join(w.tmp.name, "gaps.db"),
                      engine=None)
    for pid in ("add", "mul", "sub", "write_text"):
        e = w.evidence.add_entry(
            "observation",
            f"gate crossing: primitive '{pid}' admitted (proof fixture)",
            source="gate:PROOF")
        w.evidence.verify_entry(e["id"], "gate:PROOF")
        w.ledger.add_entry(LedgerEntry(pid, "evidence", e["id"],
                                       "gate:PROOF@proof"))
    w.admissions = AdmissionRegistry(os.path.join(w.tmp.name, "adm.db"))
    w.intent = register_intent("proof", "compute (x+y)*z")
    return w


def make_compute_candidate(cid="cand-compute", claimed=20):
    return CandidateComposition(
        candidate_id=cid,
        primitive_ids=("add", "mul"),
        plan=copy.deepcopy(PLAN),
        args=dict(ARGS),
        claimed_outcome=claimed,
        held_out=tuple(HELD_OUT),
        file_artifacts=(),
        addresses="compute (x+y)*z",
        constraints=tuple(CONSTRAINTS),
        run_id="proof-run-1",
    )


def admit_compute(w, candidate=None, **over):
    kw = dict(ledger=w.ledger, primitive_ids=["add", "mul"],
              intent=w.intent,
              candidate=candidate or make_compute_candidate(),
              provenance_store=w.provenance, composer=w.composer,
              primitives=w.registry, repo_root=WT_ROOT,
              registry=w.admissions)
    kw.update(over)
    return admit(**kw)

# ---------------------------------------------------------------------------
# t01-t03: fully-evidenced artifact -> admitted, record complete, registry
# round-trip
# ---------------------------------------------------------------------------
w = make_world()
rec = admit_compute(w)
check("t01 fully-evidenced artifact admitted",
      isinstance(rec, AdmissionRecord), f"got {type(rec).__name__}")
check("t01 every criterion passed",
      all(c.passed for c in rec.criteria),
      f"{[(c.criterion, c.passed) for c in rec.criteria]}")
check("t01 criteria set is the frozen v1 identifiers",
      {c.criterion for c in rec.criteria} == set(ADMISSION_CRITERIA_V1),
      f"{[c.criterion for c in rec.criteria]}")
check("t01 record carries the evaluation evidence",
      rec.novelty.get("verdict") == "novel"
      and rec.value.get("satisfied") is True
      and len(rec.panels) == 4
      and all(p["passed"] for p in rec.panels),
      f"novelty={rec.novelty.get('verdict')} value={rec.value.get('satisfied')}")
check("t01 verification events carry gate references",
      all(e.get("gate_reference") for e in rec.verification_events),
      f"{rec.verification_events}")
check("t02 admission id unique and well-formed",
      rec.admission_id.startswith("adm_") and len(rec.admission_id) > 20,
      f"{rec.admission_id!r}")
back = w.admissions.get_admission(rec.admission_id)
check("t03 registry round-trip returns an equal record",
      back is not None and back.to_dict() == rec.to_dict())
check("t03 list_admissions contains the id",
      rec.admission_id in w.admissions.list_admissions())

# ---------------------------------------------------------------------------
# t04: file candidate -> admitted; the file was REALLY written by the
# composer during the run and REALLY opened by the admission check
# ---------------------------------------------------------------------------
w = make_world()
fpath = os.path.join(WT_ROOT, "runtime", "creativity",
                     "_admission_fixture.txt")
hpath = os.path.join(WT_ROOT, "runtime", "creativity",
                     "_admission_fixture_heldout.txt")
CREATED_FILES.extend([fpath, hpath])
FILE_PLAN = {
    "steps": [{"id": "w1", "op": "write_text",
               "args": {"path": {"$param": "p"},
                        "content": {"$param": "c"}}}],
    "output": {"$step": "w1"},
    "params": {"p": "str", "c": "str"},
}
FILE_INTENT = register_intent("proof", "produce the commissioned file")
file_cand = CandidateComposition(
    candidate_id="cand-file",
    primitive_ids=("write_text",),
    plan=copy.deepcopy(FILE_PLAN),
    args={"p": fpath, "c": "hello gallery"},
    claimed_outcome=fpath,
    held_out=({"args": {"p": hpath, "c": "again"}, "expected": hpath},),
    file_artifacts=(fpath,),
    addresses="produce the commissioned file",
    constraints=(
        {"kind": "requires_primitive", "primitive": "write_text"},
        {"kind": "max_steps", "n": 5},
        {"kind": "exact_outcome", "value": fpath},
    ),
    run_id="proof-run-file",
)
rec_f = admit(ledger=w.ledger, primitive_ids=["write_text"],
              intent=FILE_INTENT, candidate=file_cand,
              provenance_store=w.provenance, composer=w.composer,
              primitives=w.registry, repo_root=WT_ROOT,
              registry=w.admissions)
check("t04 file candidate admitted", isinstance(rec_f, AdmissionRecord))
check("t04 the file was really written by the plan (not the test)",
      os.path.isfile(fpath)
      and open(fpath, encoding="utf-8").read() == "hello gallery",
      "content mismatch — the composer did not write it")
crit = next(c for c in rec_f.criteria
            if c.criterion == "executes_as_claimed")
check("t04 executes_as_claimed passed on the real open check",
      crit.passed, f"reason={crit.reason!r}")

# ---------------------------------------------------------------------------
# t05: incomplete provenance -> refused with the exact reason
# ---------------------------------------------------------------------------
w = make_world()
expect_raises("t05 missing intent refused as incomplete provenance",
              AdmissionRefused,
              lambda: admit_compute(w, intent=None),
              "provenance incomplete")

# ---------------------------------------------------------------------------
# t06: genuine panel fail (honesty confabulation, real panel) -> refused
# with the verdict attached
# ---------------------------------------------------------------------------
w = make_world()
try:
    admit_compute(w, candidate=make_compute_candidate(claimed=9999))
    check("t06 confabulated claim refused", False, "admitted anyway")
except AdmissionRefused as e:
    msg = str(e)
    check("t06 confabulated claim refused", "panels_pass" in msg, msg[:200])
    check("t06 refusal names the honesty failure",
          "honesty" in msg, msg[:300])
    check("t06 refusal carries expected vs observed",
          "9999" in msg and "20" in msg, msg[:300])

# ---------------------------------------------------------------------------
# t07: missing critique evidence (composer=None) -> refused via the real
# pipeline refusal
# ---------------------------------------------------------------------------
w = make_world()
expect_raises("t07 composer=None refused through the pipeline",
              AdmissionRefused,
              lambda: admit_compute(w, composer=None),
              "critique: pipeline refused")

# ---------------------------------------------------------------------------
# t08: illegal composition never reaches admission
# ---------------------------------------------------------------------------
w = make_world()
ghost_cand = CandidateComposition(
    candidate_id="cand-ghost",
    primitive_ids=("add", "ghost_prim"),
    plan=copy.deepcopy(PLAN),
    args=dict(ARGS),
    claimed_outcome=20,
    held_out=tuple(HELD_OUT),
    file_artifacts=(),
    addresses="compute (x+y)*z",
    constraints=tuple(CONSTRAINTS),
    run_id="proof-run-ghost",
)
expect_raises("t08 unverified primitive refused at legality",
              AdmissionRefused,
              lambda: admit(ledger=w.ledger,
                           primitive_ids=["add", "ghost_prim"],
                           intent=w.intent, candidate=ghost_cand,
                           provenance_store=w.provenance,
                           composer=w.composer, primitives=w.registry,
                           repo_root=WT_ROOT, registry=w.admissions),
              "legality")

# ---------------------------------------------------------------------------
# t09-t13: _verify_file_artifact distinguishes by execution, not by flag
# ---------------------------------------------------------------------------
ztmp = tempfile.TemporaryDirectory()
good_zip = os.path.join(ztmp.name, "good.zip")
with zipfile.ZipFile(good_zip, "w") as zf:
    zf.writestr("a.txt", "real content")
ok, reason = _verify_file_artifact(good_zip)
check("t09 real zip extracts cleanly", ok, f"reason={reason!r}")

bad_zip = os.path.join(ztmp.name, "bad.zip")
with open(bad_zip, "wb") as fh:
    fh.write(b"not a zip at all")
ok, reason = _verify_file_artifact(bad_zip)
check("t10 corrupt zip fails the open check", not ok, f"reason={reason!r}")

ok, reason = _verify_file_artifact(os.path.join(ztmp.name, "missing.txt"))
check("t11 missing file fails", not ok, f"reason={reason!r}")

weird = os.path.join(ztmp.name, "artifact.blend")
with open(weird, "w", encoding="utf-8") as fh:
    fh.write("x")
ok, reason = _verify_file_artifact(weird)
check("t12 unknown kind fails closed (no assumption)",
      not ok and "unverifiable kind" in reason, f"reason={reason!r}")

empty = os.path.join(ztmp.name, "empty.txt")
open(empty, "w").close()
ok, reason = _verify_file_artifact(empty)
check("t13 empty text file fails", not ok, f"reason={reason!r}")
ztmp.cleanup()

# ---------------------------------------------------------------------------
# t14-t16: removal — James's authority as a real record
# ---------------------------------------------------------------------------
w = make_world()
rec = admit_compute(w)
rem = record_removal(w.admissions, rec.admission_id,
                     reason="James: not gallery-worthy on review")
check("t14 removal record created",
      rem.admission_id == rec.admission_id
      and rem.authority == "james"
      and rem.reason == "James: not gallery-worthy on review"
      and rem.artifact_disposition == "quarantined"
      and rem.removed_at > 0)
back_rem = w.admissions.get_removal(rec.admission_id)
check("t14 removal round-trips from the registry",
      back_rem is not None and back_rem.to_dict() == rem.to_dict())
expect_raises("t15 removal of a nonexistent admission refused",
              AdmissionRefused,
              lambda: record_removal(w.admissions, "adm_nope",
                                     reason="x"),
              "no such admission")
expect_raises("t16 removal without a reason refused",
              AdmissionRefused,
              lambda: record_removal(w.admissions, rec.admission_id,
                                     reason="  "),
              "needs a reason")

# ---------------------------------------------------------------------------
# t17: adversarial — a hand-assembled report cannot be smuggled in;
# admit() takes pipeline inputs only
# ---------------------------------------------------------------------------
w = make_world()
sig = inspect.signature(admit)
params = set(sig.parameters)
check("t17 admit() has no report parameter",
      "report" not in params and "critique_report" not in params,
      f"params={sorted(params)}")
check("t17 admit() has no verdict parameter",
      "verdict" not in params and "composition_verdict" not in params,
      f"params={sorted(params)}")
try:
    admit_compute(w, report="forged-but-plausible")
    check("t17 forged report kwarg rejected", False, "accepted?!")
except TypeError as e:
    check("t17 forged report kwarg rejected with TypeError",
          "report" in str(e), str(e)[:120])

# ---------------------------------------------------------------------------
# t18-t19: run_release_flow — the §7 staged flow, admitted and refused
# ---------------------------------------------------------------------------
w = make_world()
flow = run_release_flow(ledger=w.ledger, primitive_ids=["add", "mul"],
                        intent=w.intent,
                        candidate=make_compute_candidate(),
                        provenance_store=w.provenance,
                        composer=w.composer, primitives=w.registry,
                        repo_root=WT_ROOT, registry=w.admissions)
check("t18 release flow admits the evidenced candidate",
      flow.admitted and flow.admission is not None)
check("t18 flow stages recorded",
      any(s.stage == "flow" and s.ok for s in flow.stages),
      f"{flow.stages}")

w = make_world()
flow_bad = run_release_flow(
    ledger=w.ledger, primitive_ids=["add", "mul"], intent=w.intent,
    candidate=make_compute_candidate(claimed=9999),
    provenance_store=w.provenance, composer=w.composer,
    primitives=w.registry, repo_root=WT_ROOT, registry=w.admissions)
check("t19 release flow refuses the confabulated candidate",
      not flow_bad.admitted and flow_bad.admission is None)
check("t19 refusal names the failing criterion",
      "panels_pass" in flow_bad.refusal_reason,
      flow_bad.refusal_reason[:200])

# ---------------------------------------------------------------------------
# t20: novelty RETRIEVAL -> refused, the existing record named
# ---------------------------------------------------------------------------
w = make_world()
w.provenance.record(ProvenanceRecord(
    capability_id="prior-composition",
    origin=Origin.SYNTHESIZED,
    trust=TrustLevel.TRUSTED,
    primitives_used=["add", "mul"],
    source="gate:PROOF"))
try:
    admit_compute(w)
    check("t20 retrieval refused at the novelty bar", False,
          "admitted a retrieval as a creation")
except AdmissionRefused as e:
    msg = str(e)
    check("t20 retrieval refused at the novelty bar",
          "novelty_bar" in msg, msg[:200])
    check("t20 refusal names the existing record",
          "prior-composition" in msg, msg[:300])

# ---------------------------------------------------------------------------
# cleanup + summary
# ---------------------------------------------------------------------------
for p in CREATED_FILES:
    try:
        os.remove(p)
    except OSError:
        pass
leftovers = [p for p in CREATED_FILES if os.path.exists(p)]
check("t21 no fixture files left in the worktree", not leftovers,
      f"left: {leftovers}")

print(f"\nRELEASE_PROOF_DONE pass={PASS} fail={FAIL}", flush=True)
sys.exit(1 if FAIL else 0)
