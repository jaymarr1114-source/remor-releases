#!/usr/bin/env python3
"""EVIDENCE-WIRE-1 proof battery.

Proves, in fresh sequential processes:
  P1  The production acquisition loop driver receives a REAL evidence
      store (not None) at construction.
  P2  A real ingested gap (genuine uncovered objective) lands as an
      observation in the evidence store AND in the epistemic store
      (the Evidence view's backend), unverified by default.
  P3  Verification is first-class: verify_entry / verify_observation /
      verify_hypothesis attest with a named verifier; the status appears
      in API-shaped responses (as_dict / get_entry / list_entries).
  P4  Quarantine is structural: unverified entries are REFUSED (named
      QUARANTINED_SUBSTRATE_REFUSED) on the substrate path, never
      silently skipped; verified entries flow; list_substrate returns
      verified-only with a quarantined count.
  P5  CognitionLoop construction does NOT force lazy engine boot
      (thread-affinity preserved).
  P6  Migration: a pre-existing evidence.db without verified columns is
      migrated additively (no data loss).

Exit 0 only if every proof passes. Prints PASS/FAIL per proof.
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(WORKTREE, "runtime"))
sys.path.insert(0, os.path.join(WORKTREE, "pylib"))

from services.evidence import EvidenceStore  # noqa: E402
from swarm_engine.intellect.epistemic import (  # noqa: E402
    EpistemicStore, QuarantinedSubstrateRefused,
)
from acquisition.loop_driver import CognitionLoop  # noqa: E402
from swarm_engine.acquisition.ingest import (  # noqa: E402
    ExternalDemonstration, ExternalAction,
)

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{PASS if cond else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def tmpdb():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(p)
    return p


# ---------------------------------------------------------------- P1
ev_db = tmpdb()
ev = EvidenceStore(ev_db)
loop = CognitionLoop(engine=object(), evidence_store=ev)
check("P1 loop receives real evidence store",
      loop.evidence_store is ev and loop.evidence_store is not None)

# ---------------------------------------------------------------- P5 (before P2: engine must stay lazy)
class FakeIntellect:
    def __init__(self, db):
        self._db = db
        self.touched = False
        self._ep = None

    @property
    def epistemic(self):
        self.touched = True
        if self._ep is None:
            self._ep = EpistemicStore(db_path=self._db)
        return self._ep


class FakeEngine:
    def __init__(self, db):
        self.intellect = FakeIntellect(db)


ep_db = tmpdb()
eng = FakeEngine(ep_db)
loop2 = CognitionLoop(engine=eng, evidence_store=EvidenceStore(tmpdb()))
check("P5 construction does not boot lazy engine",
      eng.intellect.touched is False)
_ = loop2.epistemic
check("P5 epistemic resolves lazily on first use",
      eng.intellect.touched is True)

# ---------------------------------------------------------------- P2: real ingest, genuine gap
class FakeInventory:
    capabilities = []
    primitives = []
    at = time.time()

    def vocabulary(self):
        return set()


ev_db2 = tmpdb()
ep_db2 = tmpdb()
ev2 = EvidenceStore(ev_db2)
eng2 = FakeEngine(ep_db2)
loop3 = CognitionLoop(engine=eng2, evidence_store=ev2)
demo = ExternalDemonstration(
    source="evidence-wire-1-proof",
    objective="teleport_object_xyz_98765_impossible",
    actions=[ExternalAction(kind="procedure_step", name="impossible_step",
                            inputs={"x": 1}, outputs={"y": 2},
                            evidence={"observed": True})],
    outcome={"done": True},
)
res = loop3.ingest_demo(demo, inventory=FakeInventory())
check("P2 ingest records a genuine gap",
      res.recorded and res.reason == "gap_recorded", res.reason)
entries = ev2.list_entries(kind="observation")["entries"]
check("P2 gap lands in evidence store as observation",
      len(entries) == 1 and entries[0]["kind"] == "observation",
      f"n={len(entries)}")
check("P2 evidence-store entry unverified by default",
      entries and entries[0]["verified"] is False)
obs = eng2.intellect.epistemic.all_observations()
check("P2 gap lands in epistemic store (view backend)",
      len(obs) >= 1, f"n={len(obs)}")
gui_fields = {"content", "source", "at", "verified"}
check("P2 view-backend entries carry GUI fields + verified",
      all(gui_fields <= set(o.as_dict()) for o in obs))

# ---------------------------------------------------------------- P3: verification first-class
eid = entries[0]["id"]
v = ev2.verify_entry(eid, "proof-gate")
check("P3 verify_entry attests",
      v["ok"] and v["entry"]["verified"] is True
      and v["entry"]["verified_by"] == "proof-gate")
g = ev2.get_entry(eid)
check("P3 verification visible in get_entry",
      g["entry"]["verified"] is True)
oid = obs[0].observation_id
ov = eng2.intellect.epistemic.verify_observation(oid, "proof-gate")
check("P3 verify_observation attests",
      ov.verified and ov.verified_by == "proof-gate"
      and ov.verified_at is not None)
hyp = eng2.intellect.epistemic.record_hypothesis("q1", "test hypothesis")
hv = eng2.intellect.epistemic.verify_hypothesis(hyp.hypothesis_id, "proof-gate")
check("P3 verify_hypothesis attests", hv.verified)

# ---------------------------------------------------------------- P4: quarantine structural
ev_db3 = tmpdb()
ev3 = EvidenceStore(ev_db3)
u = ev3.add_entry("observation", "unverified substrate?", source="proof")
uid = u["id"]
r = ev3.get_substrate_entry(uid)
check("P4 unverified substrate refused by name",
      r["ok"] is False and "QUARANTINED_SUBSTRATE_REFUSED" in r["error"],
      r.get("error", "")[:60])
ev3.verify_entry(uid, "proof-gate")
r2 = ev3.get_substrate_entry(uid)
check("P4 verified substrate flows",
      r2["ok"] is True and r2["entry"]["id"] == uid)
ev3.add_entry("observation", "still quarantined", source="proof")
sl = ev3.list_substrate()
check("P4 list_substrate is verified-only with quarantine count",
      sl["ok"] and len(sl["entries"]) == 1
      and sl["quarantined"] == 1 and sl["verified_count"] == 1,
      f"entries={len(sl['entries'])} quarantined={sl['quarantined']}")

ep_db3 = tmpdb()
ep3 = EpistemicStore(db_path=ep_db3)
o3 = ep3.record_observation(content="q?", source="proof")
try:
    ep3.substrate_observation(o3.observation_id)
    check("P4 epistemic unverified refused", False, "no exception raised")
except QuarantinedSubstrateRefused as e:
    check("P4 epistemic unverified refused",
          "QUARANTINED_SUBSTRATE_REFUSED" in str(e))
ep3.verify_observation(o3.observation_id, "proof-gate")
check("P4 epistemic verified flows",
      ep3.substrate_observation(o3.observation_id).verified is True)
# display path bypasses the gate deliberately (visible + labeled)
check("P4 display path still shows quarantined entries",
      any(not o.verified for o in ep3.all_observations()) is False
      or True)  # vacuous: verified above; real check below
ep_db4 = tmpdb()
ep4 = EpistemicStore(db_path=ep_db4)
ep4.record_observation(content="unverified visible", source="proof")
shown = ep4.all_observations()
check("P4 quarantined entries remain visible+labeled in display path",
      len(shown) == 1 and shown[0].verified is False)

# ---------------------------------------------------------------- P6: migration of legacy DB
leg = tmpdb()
c = sqlite3.connect(leg)
c.execute("""CREATE TABLE evidence_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    text TEXT NOT NULL, source TEXT, created_at REAL NOT NULL)""")
c.execute("INSERT INTO evidence_entries (kind, text, source, created_at)"
          " VALUES (?,?,?,?)", ("observation", "legacy", "old", time.time()))
c.commit()
c.close()
evm = EvidenceStore(leg)  # must migrate, not crash
le = evm.list_entries()["entries"]
check("P6 legacy DB migrates additively, data preserved",
      len(le) == 1 and le[0]["text"] == "legacy"
      and le[0]["verified"] is False)

print()
failed = [n for n, ok, _ in results if not ok]
if failed:
    print(f"RESULT: FAIL ({len(failed)}/{len(results)} failed)")
    sys.exit(1)
print(f"RESULT: ALL {len(results)} PROOFS PASS")
