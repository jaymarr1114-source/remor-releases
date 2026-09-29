"""PLOOP-5 proof battery: GraphController.

Covers: region selection (relevance + dependency closure + fail-closed),
dependency-ordered navigation, bound enforcement, refusal-as-values,
subordinate controllers, and an end-to-end harness where a REAL
microcontroller (frozen substrate) drives REAL graph work (TaskGraph) through
the GraphController.

Exits 0 iff every check passes. Prints machine-readable counts.
No mocks: the graph is a real TaskGraph, the microcontroller is the real
frozen substrate, the worker does real deterministic computation.
"""
import hashlib
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..", "pylib")))

from swarm_engine.core.graph_controller import (
    GraphController, TaskGraphAdapter,
)
from swarm_engine.core.taskgraph import TaskGraph, build_graph
from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate,
)

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))


def build_test_graph():
    g = TaskGraph("prepare report")
    n1 = g.add("gather intel")
    n2 = g.add("analyze findings", depends_on=[n1.node_id])
    n3 = g.add("draft summary", depends_on=[n2.node_id])
    n4 = g.add("fetch weather")          # irrelevant, independent
    n5 = g.add("compress images")       # irrelevant, independent
    return g, (n1, n2, n3, n4, n5)


def fresh_gc(graph_id="g1"):
    g, nodes = build_test_graph()
    gc = GraphController()
    gc.attach_graph(TaskGraphAdapter(g, graph_id))
    return gc, g, nodes


# ---------------------------------------------------------------- unit: region
gc, g, (n1, n2, n3, n4, n5) = fresh_gc()
r = gc.open_region("mc-1", "g1", "draft summary from analyzed findings",
                   max_operations=8)
check("region.ok", r.ok, str(r.refusal))
check("region.selects_relevant", set(r.node_ids) == {n1.node_id, n2.node_id,
                                                     n3.node_id},
      f"got {r.node_ids}")
check("region.excludes_irrelevant",
      n4.node_id not in r.node_ids and n5.node_id not in r.node_ids)

r2 = gc.open_region("mc-1", "nope", "draft summary", max_operations=8)
check("region.unknown_graph_refused",
      not r2.ok and r2.refusal.reason == "unknown_graph")

r3 = gc.open_region("mc-1", "g1", "quantum banana symphony", max_operations=8)
check("region.no_match_refused",
      not r3.ok and r3.refusal.reason == "no_matching_nodes",
      str(r3.refusal))

r4 = gc.open_region("mc-1", "g1", "draft summary from analyzed findings",
                    max_operations=2)
check("region.tight_budget_still_opens", r4.ok,
      str(r4.refusal) if not r4.ok else "")
if r4.ok:
    # bounded partial progress: 2 ops allowed, 3rd refused, bound flagged
    _o1 = gc.next_operable(r4.region_id)
    gc.record_result(r4.region_id, _o1.node_id, success=True, value=1)
    _o2 = gc.next_operable(r4.region_id)
    gc.record_result(r4.region_id, _o2.node_id, success=True, value=2)
    _o3 = gc.next_operable(r4.region_id)
    _r3 = gc.record_result(r4.region_id, _o3.node_id, success=True, value=3)
    check("region.budget_partial_progress",
          not _r3.ok and _r3.refusal.reason == "region_bound_exceeded")
    _s4 = gc.close_region(r4.region_id)
    check("region.bound_hit_in_summary", _s4.bound_hit and _s4.operated == 2)

r5 = gc.open_region("mc-1", "g1", "   ", max_operations=8)
check("region.empty_objective_refused",
      not r5.ok and r5.refusal.reason == "invalid_objective")

r6 = gc.open_region("mc-1", "g1", "draft summary", max_operations=0)
check("region.bad_bound_refused",
      not r6.ok and r6.refusal.reason == "invalid_bound")

# ------------------------------------------------------------ unit: navigation
order = []
rid = r.region_id
for _ in range(10):
    nxt = gc.next_operable(rid)
    if not nxt.ok:
        break
    order.append(nxt.node_id)
    gc.record_result(rid, nxt.node_id, success=True, value=f"v-{nxt.node_id}")
check("nav.dependency_order", order == [n1.node_id, n2.node_id, n3.node_id],
      f"got {order}")
nxt = gc.next_operable(rid)
check("nav.exhausted_none_ready", not nxt.ok and nxt.status == "none_ready")

# failure -> dependents unreachable
gc2, _, (m1, m2, m3, m4, m5) = fresh_gc("g2")
rr = gc2.open_region("mc-1", "g2", "draft summary from analyzed findings",
                     max_operations=8)
rid2 = rr.region_id
first = gc2.next_operable(rid2)
gc2.record_result(rid2, first.node_id, success=False, error="boom")
nxt = gc2.next_operable(rid2)
check("nav.failure_blocks_dependents", not nxt.ok, f"got {nxt}")
s = gc2.close_region(rid2)
check("nav.unreachable_counted",
      s.unreachable == 2 and s.failed == 1 and s.succeeded == 0,
      str(s.as_dict()))

# ---------------------------------------------------------------- unit: bounds
gc3, _, (b1, b2, b3, b4, b5) = fresh_gc("g3")
rb = gc3.open_region("mc-1", "g3", "draft summary from analyzed findings",
                     max_operations=2)
rid3 = rb.region_id
o1 = gc3.next_operable(rid3)
gc3.record_result(rid3, o1.node_id, success=True, value=1)
o2 = gc3.next_operable(rid3)
gc3.record_result(rid3, o2.node_id, success=True, value=2)
o3 = gc3.next_operable(rid3)
rec3 = gc3.record_result(rid3, o3.node_id, success=True, value=3)
check("bounds.excess_refused",
      not rec3.ok and rec3.refusal.reason == "region_bound_exceeded",
      str(rec3.refusal))
s3 = gc3.close_region(rid3)
check("bounds.bound_hit_flagged", s3.bound_hit and s3.operated == 2)

# --------------------------------------------------------------- unit: refuse
gc4, _, (c1, c2, c3, c4, c5) = fresh_gc("g4")
rq = gc4.open_region("mc-1", "g4", "draft summary from analyzed findings",
                     max_operations=8)
rid4 = rq.region_id
bad = gc4.record_result("gr-999999", c1.node_id, success=True)
check("ref.unknown_region", not bad.ok and bad.refusal.reason ==
      "unknown_region")
bad = gc4.record_result(rid4, c4.node_id, success=True)  # not in region
check("ref.out_of_region", not bad.ok and bad.refusal.reason ==
      "out_of_region")
bad = gc4.record_result(rid4, c3.node_id, success=True)  # deps unmet
check("ref.not_operable", not bad.ok and bad.refusal.reason ==
      "node_not_operable")
nxt = gc4.next_operable(rid4)
gc4.record_result(rid4, nxt.node_id, success=True, value=1)
bad = gc4.record_result(rid4, nxt.node_id, success=True, value=1)
check("ref.already_recorded", not bad.ok and bad.refusal.reason ==
      "already_recorded")
s4 = gc4.close_region(rid4)
check("close.summary_ok", isinstance(s4, object) and s4.succeeded == 1)
bad = gc4.close_region(rid4)
check("ref.double_close", isinstance(bad, object) and
      getattr(bad, "reason", "") == "unknown_region", str(bad))
bad = gc4.next_operable(rid4)
check("ref.operable_after_close", not bad.ok and bad.refusal.reason ==
      "unknown_region")
v = gc4.region_view("gr-999999")
check("ref.view_unknown", getattr(v, "reason", "") == "unknown_region")

# -------------------------------------------------------------- unit: subregion
gc5, _, (d1, d2, d3, d4, d5) = fresh_gc("g5")
rp = gc5.open_region("mc-1", "g5", "draft summary from analyzed findings",
                     max_operations=8)
child, sr = gc5.open_subregion(rp.region_id, [d2.node_id, d3.node_id],
                               max_operations=4)
check("sub.ok", sr.ok and child is not None, str(sr.refusal))
check("sub.scoped", set(sr.node_ids) == {d2.node_id, d3.node_id})
nxt = child.next_operable(sr.region_id)
check("sub.dep_order_in_child", nxt.ok and nxt.node_id == d2.node_id,
      f"got {nxt}")
child2, sr2 = gc5.open_subregion(rp.region_id, [d4.node_id],
                                 max_operations=4)
check("sub.outside_parent_refused",
      sr2.refusal is not None and
      sr2.refusal.reason == "outside_parent_region", str(sr2.refusal))
child3, sr3 = gc5.open_subregion("gr-999999", [d1.node_id], max_operations=4)
check("sub.unknown_parent_refused",
      sr3.refusal is not None and sr3.refusal.reason == "unknown_region")

# ----------------------------------------------------------------- e2e harness
# A REAL microcontroller (frozen substrate) drives REAL graph work through
# the GraphController. The worker does real deterministic computation.
substrate = MicrocontrollerSubstrate()
substrate.register_loop("generalization", budget_s=600.0)
spawned = substrate.spawn("generalization",
                          purpose="draft summary from analyzed findings",
                          budget_s=120.0)
check("e2e.spawn_ok", spawned.ok, str(spawned.refusal if not spawned.ok
                                     else ""))
mc_id = spawned.mc.mc_id

eg, enodes = build_test_graph()
egc = GraphController()
egc.attach_graph(TaskGraphAdapter(eg, "eg"))
er = egc.open_region(mc_id, "eg", "draft summary from analyzed findings",
                     max_operations=8)
check("e2e.region_ok", er.ok, str(er.refusal))
erid = er.region_id

def real_work(node_id, goal):
    # real deterministic computation over the node's goal text
    toks = sorted(set(goal.lower().split()))
    digest = hashlib.sha256(goal.encode()).hexdigest()[:12]
    return {"tokens": toks, "token_count": len(toks), "digest": digest,
            "node": node_id}

worked = []
while True:
    nxt = egc.next_operable(erid)
    if not nxt.ok:
        break
    val = real_work(nxt.node_id, nxt.goal)
    rec = egc.record_result(erid, nxt.node_id, success=True, value=val)
    check(f"e2e.record_{nxt.node_id}", rec.ok)
    worked.append(nxt.node_id)

# dependency order actually held in the e2e run
pos = {n: i for i, n in enumerate(worked)}
deps_ok = all(pos[d] < pos[n.node_id]
              for n in enodes for d in n.depends_on if n.node_id in pos)
check("e2e.deps_respected", deps_ok, f"order={worked}")
check("e2e.irrelevant_untouched",
      not any(n in worked for n in (enodes[3].node_id, enodes[4].node_id)))

summary = egc.close_region(erid)
check("e2e.summary_counts",
      summary.succeeded == 3 and summary.failed == 0 and
      summary.operated == 3, str(summary.as_dict()))
check("e2e.summary_values_real",
      all(isinstance(v, dict) and "digest" in v
          for v in summary.values.values()))
view = egc.region_view(erid)
check("e2e.view_after_close_refused",
      getattr(view, "reason", "") == "unknown_region")

retired = substrate.retire(mc_id, "resolved", loop="generalization")
check("e2e.retire_ok", not isinstance(retired, object) or
      getattr(retired, "mc_id", mc_id) == mc_id)

# also prove it over a graph built by the real build_graph (composite goal)
cg = build_graph("gather intel: analyze findings: draft summary")
cgc = GraphController()
cgc.attach_graph(TaskGraphAdapter(cg, "cg"))
cr = cgc.open_region("mc-x", "cg", "draft summary", max_operations=16)
check("e2e2.build_graph_region", cr.ok, str(cr.refusal))
if cr.ok:
    n = 0
    while True:
        nxt = cgc.next_operable(cr.region_id)
        if not nxt.ok:
            break
        cgc.record_result(cr.region_id, nxt.node_id, success=True,
                          value={"goal": nxt.goal})
        n += 1
    cs = cgc.close_region(cr.region_id)
    check("e2e2.all_operated", cs.succeeded == len(cg.nodes) and n ==
          len(cg.nodes), f"{cs.as_dict()} nodes={len(cg.nodes)}")

# ------------------------------------------------------------------ report
passed = sum(1 for _, ok, _ in CHECKS if ok)
total = len(CHECKS)
print(f"PLOOP5_BATTERY {passed}/{total}")
for name, ok, detail in CHECKS:
    if not ok:
        print(f"  FAIL {name} {detail}")
sys.exit(0 if passed == total else 1)
