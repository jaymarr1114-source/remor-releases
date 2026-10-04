#!/usr/bin/env python3
"""INTEGRATE-1 E3: kill mid-generation — the run stops cold.

Ten verified primitives x SearchBounds(3, 1500) makes generation take
~1.35s uncontended (measured); the shared kill event fires at 0.4s, so
the kill lands mid-generation deterministically. The run must stop at
the next step boundary with preservation complete: the RunRecord, the
grant, and the stores all intact.

Exit 0 iff every check passes.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as F  # noqa: E402
from runtime.creativity.executive import SearchBounds  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name} :: {detail}")


def main():
    pids = tuple(F.OPS)  # 10 verified primitives
    bounds = SearchBounds(max_composition_size=3, max_candidates=1500)
    w = F.make_world(pids, compute_bound=60.0, work_id="e3_work",
                     bounds=bounds, prefix="integ1e3_")
    kill = w["kill"]
    check("e3 kill event clear at open", not kill.is_set())

    def assassin():
        time.sleep(0.4)
        kill.set()

    t = threading.Thread(target=assassin, daemon=True)
    intent = F.make_intent()
    t0 = time.perf_counter()
    t.start()
    rec = w["ctl"].run(intent, estimated_compute_s=5.0)
    wall = time.perf_counter() - t0
    t.join(timeout=5)

    check("e3 run killed (not completed, not error)",
          rec.status == "killed", rec.status)
    check("e3 kill observed by the controller", rec.kill_seen is True)
    check("e3 kill detail names the cold stop",
          "kill" in (rec.outcome_detail or "").lower(),
          rec.outcome_detail)
    # Causal timing: generation alone needs ~1.35s; the run died well
    # before it could finish — the kill landed mid-generation.
    check("e3 died before generation could complete",
          rec.spend_s is not None and rec.spend_s < 1.0,
          f"spend_s={rec.spend_s} wall={wall:.2f}")

    # -- preservation: consumption, grants, provenance, checkpoints ------
    check("e3 grant was issued before the kill (consumption preserved)",
          rec.grant_issued is True)
    summary = w["budget"].spend_summary("e3_work")
    check("e3 grant record survives in the spend ledger",
          summary["granted_compute_s"] >= 5.0, str(summary))
    check("e3 actuals recorded for the partial run",
          summary["spent_compute_s"] > 0, str(summary))
    check("e3 ledger store intact (read-side uncorrupted)",
          len(w["ex"].ledger.indexed()) == len(pids),
          str(w["ex"].ledger.indexed()))
    check("e3 provenance store intact",
          os.path.exists(os.path.join(w["tmp"], "prov.db")))
    check("e3 gap registry readable after the kill",
          isinstance(F.gap_registry_for(w["tmp"]).list_gaps(), list))

    print(f"\n==== e3_kill: {PASSED}/{PASSED + FAILED} checks passed ====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
