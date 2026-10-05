"""UNIFIED-MEMORY-1 gate battery 3: cross-loop continuity (live run).

Proves a fact written by one loop is provably readable by another loop
through the unified interface, in a live run with real machinery:
  - Fresh EpistemicStore on a temp sqlite DB (no mocks, no fakes)
  - "acquisition" loop writes via record_experience (unified facade)
  - "generalization" loop reads via read_experiences (unified facade)
  - The fact is visible WITH its provenance block (origin_loop intact)
  - A second write from a different loop is distinguishable by provenance
  - Persistence: re-open the store in a new connection, facts survive
"""
import os
import sys
import tempfile

WT = os.path.expanduser("~/workspace/worktrees/unified-memory-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)

from swarm_engine.intellect.epistemic import EpistemicStore
from swarm_engine.intellect.unified_memory import (
    record_experience, read_experiences, PROVENANCE_KEY)

tmp = tempfile.mkdtemp(prefix="um1_continuity_")
db = os.path.join(tmp, "engine.db")

# --- Loop A (acquisition) writes through the unified facade ---
store_a = EpistemicStore(db_path=db)
obs_id = record_experience(
    store_a,
    origin_loop="acquisition",
    kind="test_fact",
    content="cross-loop continuity probe: the sky is blue",
    raw={"probe": "um1_b3"})
check("um1_write_returns_id", bool(obs_id), f"id={obs_id}")

# --- Loop B (generalization) reads through the unified facade ---
# Use a SEPARATE store object (simulating a different loop's handle).
store_b = EpistemicStore(db_path=db)
facts = read_experiences(store_b, kind="test_fact")
check("um1_cross_loop_readable", len(facts) >= 1,
      f"found={len(facts)}")

if facts:
    f = facts[0]
    check("um1_content_intact",
          "the sky is blue" in f["content"])
    prov = f.get("provenance", {})
    check("um1_provenance_carries_origin",
          prov.get("origin_loop") == "acquisition",
          f"origin={prov.get('origin_loop')}")
    check("um1_provenance_has_writer",
          "unified_memory.record_experience" in prov.get("write_path", ""),
          f"write_path={prov.get('write_path')}")
else:
    check("um1_content_intact", False, "no facts")
    check("um1_provenance_carries_origin", False, "no facts")
    check("um1_provenance_has_writer", False, "no facts")

# --- A second loop's write is distinguishable by provenance ---
record_experience(
    store_a,
    origin_loop="generalization",
    kind="test_fact",
    content="second probe from generalization loop",
    raw={"probe": "um1_b3"})
acq_facts = read_experiences(store_b, origin_loop="acquisition",
                             kind="test_fact")
gen_facts = read_experiences(store_b, origin_loop="generalization",
                             kind="test_fact")
check("um1_provenance_filters_acquisition",
      len(acq_facts) == 1 and
      acq_facts[0]["provenance"].get("origin_loop") == "acquisition",
      f"acq={len(acq_facts)}")
check("um1_provenance_filters_generalization",
      len(gen_facts) == 1 and
      gen_facts[0]["provenance"].get("origin_loop") == "generalization",
      f"gen={len(gen_facts)}")

# --- Persistence: new connection sees the facts (no in-memory illusion) ---
store_c = EpistemicStore(db_path=db)
facts_c = read_experiences(store_c, kind="test_fact")
check("um1_persisted_across_connections", len(facts_c) == 2,
      f"found={len(facts_c)}")

npass = sum(1 for _, c in results if c)
print(f"\n=== continuity: {npass}/{len(results)} checks passed ===",
      flush=True)
sys.exit(0 if npass == len(results) else 1)
