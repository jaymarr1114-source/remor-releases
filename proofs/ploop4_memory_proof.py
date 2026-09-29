#!/usr/bin/env python3
"""PLOOP-4 unified-memory migration proof battery.

Proves, against REAL components (no mocks, no staged demos):

  A. Zero facade-bypassing writers: a structural scan over runtime/ finds no
     direct EpistemicStore write calls outside the store implementation
     itself and the unified-memory facade.
  B. Real facade write -> persist -> read-back for every memory class:
     observations (record_experience), evidence, hypotheses, experiments.
  C. Provenance invariants: the canonical block is stamped on every write,
     caller provenance is preserved, and the additive block is inert to
     the real EvidenceArbiter.
  D. Experiment backward compatibility: rows written before the provenance
     field existed still load.
  E. intent.db bridge: run_census verifies the IntentDispatchService's
     intent.db is a schema copy with no private tables (positive control,
     negative control with a rogue table, and the absent-file case).

Run:  PYTHONPATH=pylib python3 proofs/ploop4_memory_proof.py
Exits nonzero on the first failure; prints a per-check PASS/FAIL ledger.
"""
import json
import os
import re
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "pylib"))

from swarm_engine.intellect import unified_memory as um  # noqa: E402
from swarm_engine.intellect.epistemic import (  # noqa: E402
    EpistemicStore, Evidence, Experiment, Hypothesis, HypothesisState,
)
from swarm_engine.intellect.reasoner import EvidenceArbiter  # noqa: E402

CHECKS = []
REGISTRY = []


def check(name):
    def deco(fn):
        def run():
            try:
                fn()
            except AssertionError as exc:
                CHECKS.append((name, False, str(exc)))
                print(f"FAIL {name}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 -- a crash is a failure
                CHECKS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"FAIL {name}: {type(exc).__name__}: {exc}")
                return
            CHECKS.append((name, True, ""))
            print(f"PASS {name}")
        REGISTRY.append(run)
        return run
    return deco


# ---------------------------------------------------------------------------
# A. Zero-bypass structural scan
# ---------------------------------------------------------------------------
# The frozen EpistemicStore write API. Any call to one of these outside the
# two files below is a facade bypass and fails the mission.
WRITE_PATTERNS = [
    r"\.record_observation\s*\(",
    r"\.save_observation\s*\(",
    r"\.save_evidence\s*\(",
    r"\.save_hypothesis\s*\(",
    r"\.record_hypothesis\s*\(",
    r"\.save_experiment\s*\(",
    # indirect calls through getattr (the distill.py pattern, pre-migration)
    r"getattr\s*\([^,]+,\s*[\"'](?:save_observation|record_observation|"
    r"save_evidence|save_hypothesis|record_hypothesis|save_experiment)[\"']",
]
# Narrowly justified exclusions, and only these two:
#   runtime/intellect/epistemic.py      -- the physical store itself; it
#      DEFINES these methods (the frozen write API the facade delegates to).
#   runtime/intellect/unified_memory.py -- the facade; it is the single
#      authorized caller of the frozen API (record_experience etc. save
#      through save_observation/save_evidence/save_hypothesis/
#      save_experiment exactly once per write).
EXCLUDED = {
    os.path.join("runtime", "intellect", "epistemic.py"),
    os.path.join("runtime", "intellect", "unified_memory.py"),
}


@check("A1 zero direct EpistemicStore write calls outside store+facade")
def _a1():
    compiled = [re.compile(p) for p in WRITE_PATTERNS]
    hits = []
    for root, _dirs, files in os.walk(os.path.join(REPO, "runtime")):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, fname), REPO)
            if rel in EXCLUDED:
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except UnicodeDecodeError:
                # Vendored third-party files may carry exotic encodings
                # (e.g. joblib's test_func_inspect_special_encoding.py);
                # latin-1 decodes every byte so the file is still scanned.
                with open(path, encoding="latin-1") as fh:
                    text = fh.read()
            for i, line in enumerate(text.splitlines(), 1):
                if any(p.search(line) for p in compiled):
                    hits.append(f"{rel}:{i}: {line.strip()}")
    assert not hits, f"{len(hits)} bypass call(s) remain:\n" + "\n".join(hits)


@check("A2 exclusion set is exactly the two justified files")
def _a2():
    assert EXCLUDED == {
        os.path.join("runtime", "intellect", "epistemic.py"),
        os.path.join("runtime", "intellect", "unified_memory.py"),
    }, "exclusion set drifted -- re-justify every exclusion"


@check("A3 facade is the only caller: unified_memory references each write API")
def _a3():
    src = open(os.path.join(REPO, "runtime", "intellect",
                            "unified_memory.py"), encoding="utf-8").read()
    for api in ("save_observation", "save_evidence",
                "save_hypothesis", "save_experiment"):
        assert api in src, f"facade never calls {api} -- write path missing"


# ---------------------------------------------------------------------------
# B/C/D. Functional round-trips through the real facade + real store
# ---------------------------------------------------------------------------
TMP = tempfile.mkdtemp(prefix="ploop4_")
ENGINE_DB = os.path.join(TMP, "engine.db")
store = EpistemicStore(ENGINE_DB)
arbiter = EvidenceArbiter()

CANON_KEYS = {"schema", "origin_loop", "kind", "causal_chain",
              "recorded_at", "write_path"}


def _block_of(record_raw_or_provenance):
    """Extract and validate the canonical block.

    Observations nest it under _provenance in the raw payload; evidence
    nests it under _provenance in the content dict; hypotheses and
    experiments carry it merged at the top level of their dedicated
    provenance field (that field already IS a provenance dict).
    """
    d = record_raw_or_provenance
    assert isinstance(d, dict), "provenance payload missing"
    blk = d.get(um.PROVENANCE_KEY) if um.PROVENANCE_KEY in d else d
    assert isinstance(blk, dict), "canonical provenance block missing"
    assert CANON_KEYS <= set(blk), f"block missing keys: {CANON_KEYS - set(blk)}"
    return blk


@check("B1 observation: facade write -> persist -> read-back")
def _b1():
    oid = um.record_experience(
        store, origin_loop="acquisition", kind="ploop4_probe",
        content="ploop4 observation probe",
        raw={"probe": True}, causal_chain=["cause_1"],
        source="ploop4-proof")
    assert oid.startswith("exp_") or oid
    rows = um.read_experiences(store, origin_loop="acquisition",
                               kind="ploop4_probe")
    assert any(r["observation_id"] == oid for r in rows), "write not readable"
    row = next(r for r in rows if r["observation_id"] == oid)
    blk = _block_of(row["raw"])
    assert blk["origin_loop"] == "acquisition"
    assert blk["kind"] == "ploop4_probe"
    assert blk["causal_chain"] == ["cause_1"]
    assert blk["write_path"] == "unified_memory.record_experience"
    assert row["raw"]["probe"] is True, "caller raw payload lost"


@check("B2 evidence: facade write -> evidence_for -> canonical block")
def _b2():
    ev = Evidence(evidence_id="ev_ploop4_1", target_id="hyp_ploop4",
                  supports=True,
                  content={"verdict": "passed", "score": 0.9},
                  source="ploop4-proof")
    got = um.record_evidence(store, origin_loop="intellect",
                             kind="ploop4_evidence", evidence=ev,
                             causal_chain=["exp_ploop4_cause"])
    assert got == "ev_ploop4_1"
    evs = store.evidence_for("hyp_ploop4")
    assert any(e.evidence_id == "ev_ploop4_1" for e in evs)
    back = next(e for e in evs if e.evidence_id == "ev_ploop4_1")
    assert back.supports is True, "supports flag corrupted by stamp"
    assert back.content["verdict"] == "passed", "caller content lost"
    blk = _block_of(back.content)
    assert blk["origin_loop"] == "intellect"
    assert blk["kind"] == "ploop4_evidence"
    assert blk["write_path"] == "unified_memory.record_evidence"


@check("C1 arbiter is inert to the additive provenance block")
def _c1():
    evs = store.evidence_for("hyp_ploop4")
    v = arbiter.decide("hyp_ploop4", evs)
    assert v.new_state is not None
    assert 0.0 <= v.new_confidence <= 1.0
    # and with a refuting twin the verdict flips the honest way
    ev2 = Evidence(evidence_id="ev_ploop4_2", target_id="hyp_ploop4",
                   supports=False, content={"verdict": "failed"},
                   source="ploop4-proof")
    um.record_evidence(store, origin_loop="intellect",
                       kind="ploop4_evidence", evidence=ev2)
    v2 = arbiter.decide("hyp_ploop4", store.evidence_for("hyp_ploop4"))
    assert v2.new_confidence <= v.new_confidence, \
        "refuting evidence did not move the verdict down"


@check("B3 hypothesis: write -> get -> re-save after mutation (arbiter pattern)")
def _b3():
    hyp = Hypothesis(hypothesis_id="hyp_ploop4", question_id="q_ploop4",
                     statement="ploop4 probe hypothesis",
                     provenance={"generated_by": "ploop4-proof"})
    um.record_hypothesis(store, origin_loop="intellect",
                         kind="ploop4_hypothesis", hypothesis=hyp)
    back = store.get_hypothesis("hyp_ploop4")
    assert back is not None and back.statement == "ploop4 probe hypothesis"
    assert back.provenance["generated_by"] == "ploop4-proof", \
        "caller provenance key lost"
    blk = _block_of(back.provenance)
    assert blk["write_path"] == "unified_memory.record_hypothesis"
    # the competition/engine re-save pattern: mutate, stamp again, persist
    back.state = HypothesisState.UNDER_TEST
    back.confidence = 0.6
    um.record_hypothesis(store, origin_loop="intellect",
                         kind="arbitrated_hypothesis", hypothesis=back)
    back2 = store.get_hypothesis("hyp_ploop4")
    assert back2.state == HypothesisState.UNDER_TEST
    assert back2.confidence == 0.6
    assert _block_of(back2.provenance)["kind"] == "arbitrated_hypothesis"


@check("B4 experiment: write -> experiments_for -> provenance round-trips JSON")
def _b4():
    exp = Experiment(experiment_id="exp_ploop4_1", question_id="q_ploop4",
                     hypothesis_ids=["hyp_ploop4"],
                     design={"probe": "ploop4"}, executed=True,
                     result={"observed": True})
    um.record_experiment(store, origin_loop="intellect",
                         kind="ploop4_experiment", experiment=exp)
    exps = store.experiments_for("q_ploop4")
    assert any(e.experiment_id == "exp_ploop4_1" for e in exps)
    back = next(e for e in exps if e.experiment_id == "exp_ploop4_1")
    assert back.executed is True and back.result == {"observed": True}
    blk = _block_of(back.provenance)
    assert blk["write_path"] == "unified_memory.record_experiment"


@check("D1 pre-cutover experiment rows (no provenance key) still load")
def _d1():
    legacy_db = os.path.join(TMP, "legacy.db")
    s = EpistemicStore(legacy_db)  # creates the real current schema
    # Simulate a row written before the provenance field existed: raw JSON
    # with no "provenance" key, inserted exactly as the old code wrote it.
    legacy = {"experiment_id": "exp_legacy", "question_id": "q_legacy",
              "hypothesis_ids": [], "design": {}, "executed": False,
              "result": None, "at": 1.0}
    import time as _t
    with s._conn() as conn:
        conn.execute("INSERT INTO experiments "
                     "(experiment_id, question_id, data, updated_at) "
                     "VALUES (?,?,?,?)",
                     ("exp_legacy", "q_legacy", json.dumps(legacy), _t.time()))
    exps = s.experiments_for("q_legacy")
    assert len(exps) == 1 and exps[0].experiment_id == "exp_legacy"
    assert exps[0].provenance == {}, "legacy row should get empty provenance"


@check("B5 UnifiedMemory method attachments route through the facade")
def _b5():
    mem = um.UnifiedMemory(store, capabilities=None, registry=None)
    ev = Evidence(evidence_id="ev_ploop4_m", target_id="hyp_ploop4",
                  supports=True, content={}, source="ploop4-proof")
    mem.record_evidence("intellect", "ploop4_method", ev)
    hyp = Hypothesis(hypothesis_id="hyp_ploop4_m", question_id="q_ploop4",
                     statement="method attachment probe")
    mem.record_hypothesis("intellect", "ploop4_method", hyp)
    exp = Experiment(experiment_id="exp_ploop4_m", question_id="q_ploop4",
                     hypothesis_ids=[], design={})
    mem.record_experiment("intellect", "ploop4_method", exp)
    assert any(e.evidence_id == "ev_ploop4_m"
               for e in store.evidence_for("hyp_ploop4"))
    assert store.get_hypothesis("hyp_ploop4_m") is not None
    assert any(e.experiment_id == "exp_ploop4_m"
               for e in store.experiments_for("q_ploop4"))


# ---------------------------------------------------------------------------
# E. intent.db bridge
# ---------------------------------------------------------------------------
@check("E1 census bridges a real intent.db: present, no tables outside schema")
def _e1():
    intent_db = os.path.join(TMP, "intent.db")
    EpistemicStore(intent_db)  # the service boots a full engine-schema DB
    res = um.run_census(ENGINE_DB, intent_db_path=intent_db)
    entry = res["intent_db"]
    assert entry["present"] is True, "intent.db not detected"
    assert entry["tables_outside_engine_schema"] == [], \
        f"private tables in intent.db: {entry['tables_outside_engine_schema']}"
    assert not res["unclaimed_tables"], \
        f"census defects: {res['unclaimed_tables']}"


@check("E2 negative control: a rogue table in intent.db is flagged")
def _e2():
    rogue_db = os.path.join(TMP, "intent_rogue.db")
    EpistemicStore(rogue_db)
    conn = sqlite3.connect(rogue_db)
    conn.execute("CREATE TABLE rogue_private_store (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    res = um.run_census(ENGINE_DB, intent_db_path=rogue_db)
    flagged = [t for t in res["unclaimed_tables"]
               if t == "intent:rogue_private_store"]
    assert flagged, "rogue table in intent.db was NOT flagged -- bridge blind"


@check("E3 absent intent.db is not a defect (service boots it lazily)")
def _e3():
    res = um.run_census(ENGINE_DB,
                        intent_db_path=os.path.join(TMP, "no_such.db"))
    entry = res["intent_db"]
    assert entry["present"] is False
    assert not res["unclaimed_tables"], \
        f"absent intent.db raised defects: {res['unclaimed_tables']}"
    # and the old single-arg call still works
    res2 = um.run_census(ENGINE_DB)
    assert res2["intent_db"]["present"] is False


def main():
    for fn in REGISTRY:
        fn()
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
