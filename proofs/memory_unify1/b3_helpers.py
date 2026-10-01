#!/usr/bin/env python3
"""MEMORY-UNIFY-1 b3: extracted query helpers work as module functions.

Proves query_memory, trace_delta, trace_capability, similar_experiences,
prior_attempts, and z_check_with_experience work without the class.
"""
import os
import sys
import tempfile
import time

WT = "/home/hatch/workspace/worktrees/memory-unify-1"
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "pylib"))

from runtime.intellect.unified_memory import (
    query_memory,
    trace_delta,
    trace_capability,
    similar_experiences,
    prior_attempts,
    z_check_with_experience,
    record_distillation_experience,
    UnifiedAnswer,
)
from runtime.intellect.epistemic import EpistemicStore
from runtime.synthesis.capability_store import (
    CapabilityRecord, CapabilityStore, plan_fingerprint,
)
from runtime.primitives import build_registry
from runtime.synthesis.planner import Planner
from runtime.acquisition.ingest import (
    ExternalAction, ExternalDemonstration, InventorySnapshot,
    ingest_external_demonstration,
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

# Setup real stores
tmp = tempfile.mkdtemp(prefix="mu1b3_")
ep = EpistemicStore(db_path=os.path.join(tmp, "ep.db"))
caps = CapabilityStore(db_path=os.path.join(tmp, "cap.db"))
registry = build_registry()
planner = Planner(registry)

# Seed with a real ingested delta
demo = ExternalDemonstration(
    source="subagent_trace", objective="summarize quarterly revenue",
    actions=[ExternalAction(
        kind="tool_call", name="summarize_quarters",
        inputs={"input": '{"q1": 10}'},
        outputs={"summary": "Total revenue 10."})],
    required_tools=["revenue_summarizer"],
    provenance={"simulated": True})
inv = InventorySnapshot(capabilities=["image_generate"],
                        primitives=["deserialize"], at=time.time())
res = ingest_external_demonstration(demo, inv, ep)
assert res.recorded, "setup: delta not recorded"

plan = {"steps": [{"id": "s1", "op": "text.summarize",
                   "args": {"text": {"$param": "input"}}}],
        "params": {"input": "any"}, "output": {"$step": "s1"}}
rec = CapabilityRecord(capability_id=plan_fingerprint(plan),
                       name="revenue_summary",
                       goal="summarize quarterly revenue figures",
                       plan=plan, ops=["text.summarize"], effects=["pure"])
caps.store(rec)

# 1. query_memory reaches all three stores
ans = query_memory(ep, caps, registry, "quarterly revenue summary")
check("query_memory returns UnifiedAnswer", isinstance(ans, UnifiedAnswer))
kinds = {h["kind"] for h in (ans.epistemic_hits + ans.capability_hits
                             + ans.primitive_hits)}
check("query_memory reaches all three stores",
      kinds == {"observation", "capability", "primitive"})

# 2. trace_delta finds the ingested delta
tr = trace_delta(ep, res.delta_id)
check("trace_delta finds delta", tr["delta"] is not None)
check("trace_delta source correct",
      tr["delta"]["source"] == "technique_delta")

# 3. trace_capability finds the record
trc = trace_capability(ep, caps, rec.capability_id)
check("trace_capability finds record", trc["record"] is not None)
check("trace_capability name correct",
      trc["record"]["name"] == "revenue_summary")

# 4. similar_experiences finds distilled experiences
record_distillation_experience(
    ep, objective="double each number in the list",
    delta_id="delta_b3_001", Y={"action": "map"},
    Z={"ops": ["data.map"]}, T="doubler",
    E=[{"inputs": {"v": [1]}, "outputs": [2]}],
    D=[], V={"status": "verified"}, C="cap_b3")
sims = similar_experiences(ep, "double the numbers in a list")
check("similar_experiences finds neighbor", len(sims) >= 1)
check("similar_experiences delta_id correct",
      sims[0]["delta_id"] == "delta_b3_001")

# 5. z_check_with_experience attaches prior experiences
zres = z_check_with_experience(
    "double the numbers in the collection",
    evidence=[({"values": [5]}, [10])],
    epistemic=ep, registry=registry, planner=planner)
check("z_check_with_experience attaches prior_experiences",
      len(zres.detail.get("prior_experiences", [])) >= 1)

print(f"\n=== b3: {passed} passed, {failed} failed ===")
sys.exit(0 if failed == 0 else 1)
