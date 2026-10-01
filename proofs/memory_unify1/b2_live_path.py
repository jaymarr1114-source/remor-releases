#!/usr/bin/env python3
"""MEMORY-UNIFY-1 b2: live write/read path still works through the engine.

Proves the ADOPT-classified functions (record_experience, read_experiences,
etc.) work via the real EpistemicStore — not just in isolation.
"""
import os
import sys
import tempfile

WT = "/home/hatch/workspace/worktrees/memory-unify-1"
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "pylib"))

from runtime.intellect.unified_memory import (
    record_experience,
    read_experiences,
    record_evidence,
    record_hypothesis,
    record_experiment,
    PROVENANCE_KEY,
)
from runtime.intellect.epistemic import (
    EpistemicStore, Evidence, Hypothesis, Experiment,
)

passed, failed = 0, 0

def check(name, cond):
    global passed, failed
    if cond:
        print(f"PASS: {name}")
        passed += 1
    else:
        print(f"FAIL: {name}")
        failed += 1

tmp = tempfile.mkdtemp(prefix="mu1b2_")
ep = EpistemicStore(db_path=os.path.join(tmp, "ep.db"))

# 1. record_experience writes with provenance
oid = record_experience(ep, origin_loop="acquisition", kind="test_kind",
                        content="test experience", raw={"foo": "bar"},
                        causal_chain=["cause1"])
check("record_experience returns id", oid and oid.startswith("exp_"))

# 2. read_experiences finds it with provenance filter
recs = read_experiences(ep, origin_loop="acquisition", kind="test_kind")
check("read_experiences finds by origin_loop+kind", len(recs) == 1)
check("provenance block present",
      recs[0]["provenance"].get("origin_loop") == "acquisition")
check("provenance has schema version",
      recs[0]["provenance"].get("schema") == 1)

# 3. record_evidence / record_hypothesis / record_experiment
ev = Evidence(evidence_id="ev_b2", target_id="hyp_b2", supports=True,
              content={}, source="b2")
eid = record_evidence(ep, "intellect", "test", ev)
check("record_evidence persists", eid == "ev_b2")

hyp = Hypothesis(hypothesis_id="hyp_b2", question_id="q_b2",
                 statement="test hypothesis")
hid = record_hypothesis(ep, "intellect", "test", hyp)
check("record_hypothesis persists", hid == "hyp_b2")

exp = Experiment(experiment_id="exp_b2", question_id="q_b2",
                 hypothesis_ids=[], design={})
xid = record_experiment(ep, "intellect", "test", exp)
check("record_experiment persists", xid == "exp_b2")

# 4. Provenance is additive (caller keys preserved)
recs2 = read_experiences(ep, origin_loop="acquisition")
check("raw preserved alongside provenance",
      recs2[0]["raw"].get("foo") == "bar")

print(f"\n=== b2: {passed} passed, {failed} failed ===")
sys.exit(0 if failed == 0 else 1)
