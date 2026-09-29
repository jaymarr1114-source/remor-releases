#!/usr/bin/env python3
"""PLOOP-7 proof battery: relevance ownership at the Primary inlet.

Charter C-3: "a fact can be true and still be irrelevant." The Primary side
owns the FINAL relevance decision. This battery feeds mixed findings at the
Primary inlet (the RelevanceGate, wired into the real ExecutiveController)
and shows:

  relevant                -> ADMITTED   (by provenance link or content score)
  true-but-irrelevant     -> RETAINED   (kept with terminal state, not promoted)
  false (refuted)         -> REJECTED   (kept as knowledge, not admitted)
  malformed               -> REJECTED   (inadmissible as evidence, C-2.2)
  relevant-to-elsewhere   -> RETAINED   (not this objective's business)
  triage="propose"        -> still RETAINED when irrelevant (triage != admission)

Plus: the threshold demonstrably bites (one term flips the verdict),
decisions persist and are re-checkable from a fresh connection, the
mechanism is deterministic, and the executive fail-closes without a gate.

Nothing here is mocked: scores come from the real tokenizer/synonym
expansion over the real objective statement; persistence is a real sqlite
store; the executive is the real ExecutiveController (all six loops
registered ABSENT -- its documented degraded-state construction -- so the
relevance path is exercised without needing a live engine).

Run: python3 proofs/ploop7_relevance_proof.py
Writes: proofs/ploop7_relevance_proof.log
"""

import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))
sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from runtime.core.executive.relevance import (  # noqa: E402
    ADMITTED,
    RETAINED,
    REJECTED,
    Finding,
    FindingProvenance,
    OperationalObjective,
    RelevanceGate,
    RelevanceRefused,
)
from runtime.core.executive.executive import ExecutiveController  # noqa: E402
from runtime.core.microcontroller import LOOPS  # noqa: E402

LOG = []


def check(name, cond, detail=""):
    LOG.append(f"[{'PASS' if cond else 'FAIL'}] {name}"
               + (f" -- {detail}" if detail else ""))
    if not cond:
        raise AssertionError(f"FAILED: {name} -- {detail}")


OBJECTIVE = OperationalObjective(
    objective_id="obj-boundary-detection",
    statement=(
        "Establish autonomous boundary detection for the Primary executive "
        "loop: observe runtime loop state, detect genuine boundary "
        "conditions, and present validated boundaries so the executive "
        "route fires and the unified loop turns."),
)


def mk(fid, content, bobj, req, term, triage=None, loop="execution"):
    return Finding(fid, content,
                   FindingProvenance(loop, bobj, req, triage), term)


def main():
    workdir = tempfile.mkdtemp(prefix="ploop7_")
    store = os.path.join(workdir, "relevance.db")
    gate = RelevanceGate(objective=OBJECTIVE, store_path=store)
    LOG.append(f"store: {store}")
    LOG.append(f"threshold: {gate.threshold}")
    LOG.append(f"objective terms ({len(OBJECTIVE.terms())}): "
               f"{sorted(OBJECTIVE.terms())}")

    # -- C1: Primary-requested evidence for this objective: admitted --------
    f1 = mk("f1",
            "stagnation detector fired three times on the execution loop "
            "with no forward progress observed",
            "obj-boundary-detection", True, "HYPOTHESIS_SUPPORTED")
    d1 = gate.decide(f1)
    check("C1 provenance-link admission",
          d1.verdict == ADMITTED and d1.criterion == "provenance-link",
          d1.reason)

    # -- C2: content-score admission (no provenance link) -------------------
    f2 = mk("f2",
            "the executive route fires reliably when the detector presents "
            "a validated boundary record for the loop",
            "obj-old-synthesis", False, "DISCOVERY_VERIFIED",
            triage="propose")
    d2 = gate.decide(f2)
    check("C2 content-score admission",
          d2.verdict == ADMITTED and d2.criterion == "content-score"
          and d2.score >= gate.threshold,
          f"score={d2.score:.3f} reason={d2.reason}")

    # -- C3: true but irrelevant -> retained, not promoted ------------------
    f3 = mk("f3",
            "TCP retransmission timers use exponential backoff to avoid "
            "congestion collapse",
            "obj-networking", False, "DISCOVERY_VERIFIED")
    d3 = gate.decide(f3)
    check("C3 true-but-irrelevant retained",
          d3.verdict == RETAINED and d3.criterion == "below-threshold",
          d3.reason)

    # -- C4: refuted claim -> rejected (kept as knowledge, not admitted) ----
    f4 = mk("f4",
            "the polling detector approach cannot distinguish idle loops "
            "from stuck loops",
            "obj-boundary-detection", True, "HYPOTHESIS_REFUTED")
    d4 = gate.decide(f4)
    check("C4 refuted finding rejected",
          d4.verdict == REJECTED and d4.criterion == "refuted",
          d4.reason)

    # -- C5: boundary case -- relevant to a DIFFERENT objective ------------
    f5 = mk("f5",
            "ledger entries reconcile against bank statements within two "
            "business days",
            "obj-ledger", False, "MODEL_REVISED")
    d5 = gate.decide(f5)
    check("C5 relevant-elsewhere retained at this inlet",
          d5.verdict == RETAINED,
          f"score={d5.score:.3f} reason={d5.reason}")

    # -- C6/C7: malformed findings rejected ---------------------------------
    f6 = mk("f6", "something happened", "obj-boundary-detection", True,
            "BOGUS_STATE")
    d6 = gate.decide(f6)
    check("C6 bad terminal state rejected",
          d6.verdict == REJECTED and d6.criterion == "malformed",
          d6.reason)
    f7 = Finding("f7", "something happened",
                 FindingProvenance("", "obj-boundary-detection", True),
                 "DISCOVERY_VERIFIED")
    d7 = gate.decide(f7)
    check("C7 missing provenance rejected",
          d7.verdict == REJECTED and d7.criterion == "malformed",
          d7.reason)

    # -- C8: triage="propose" does NOT admit --------------------------------
    f8 = mk("f8",
            "the office plants need watering on tuesdays",
            "obj-curiosity-wander", False, "NOVELTY_CLASSIFIED",
            triage="propose", loop="discovery")
    d8 = gate.decide(f8)
    check("C8 triage is not admission",
          d8.verdict == RETAINED and "triage" in d8.reason,
          d8.reason)

    # -- C9: the threshold bites -- one term flips the verdict -------------
    f_above = mk("f_edge_above",
                 "Runtime state sampling shows the loop boundary detector "
                 "reporting to the executive on schedule",
                 "obj-other", False, "DISCOVERY_VERIFIED")
    f_below = mk("f_edge_below",
                 "The runtime state cache for the loop executive dashboard "
                 "was flushed at midnight",
                 "obj-other", False, "DISCOVERY_VERIFIED")
    d_above = gate.decide(f_above)
    d_below = gate.decide(f_below)
    LOG.append(f"edge pair: above score={d_above.score:.3f} -> "
               f"{d_above.verdict}; below score={d_below.score:.3f} -> "
               f"{d_below.verdict}")
    check("C9 threshold discriminates",
          d_above.score >= gate.threshold > d_below.score
          and d_above.verdict == ADMITTED
          and d_below.verdict == RETAINED,
          "one overlapping term ('boundary') flips admitted/retained")

    # -- C10: persistence is re-checkable from a fresh connection -----------
    decided = ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8",
               "f_edge_above", "f_edge_below"]
    expected = {
        "f1": ADMITTED, "f2": ADMITTED, "f3": RETAINED, "f4": REJECTED,
        "f5": RETAINED, "f6": REJECTED, "f7": REJECTED, "f8": RETAINED,
        "f_edge_above": ADMITTED, "f_edge_below": RETAINED,
    }
    conn = sqlite3.connect(store)  # fresh connection, gate not involved
    try:
        n = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        check("C10a all decisions persisted", n == len(decided),
              f"{n} decision rows")
        ok = True
        for fid in decided:
            row = conn.execute(
                "SELECT verdict FROM decisions WHERE finding_id=? "
                "ORDER BY seq DESC LIMIT 1", (fid,)).fetchone()
            if row is None or row[0] != expected[fid]:
                ok = False
                LOG.append(f"  mismatch: {fid} -> "
                           f"{row[0] if row else None}")
        check("C10b verdicts re-checkable", ok,
              "every finding's persisted verdict matches")
        retained = conn.execute(
            "SELECT finding_id, terminal_state FROM findings "
            "WHERE admission='retained' ORDER BY finding_id").fetchall()
        check("C10c retained keep terminal state",
              len(retained) == 4
              and all(r[1] for r in retained),
              f"retained: {retained}")
        rej = conn.execute(
            "SELECT finding_id, terminal_state FROM findings "
            "WHERE admission='rejected' AND finding_id='f4'").fetchone()
        check("C10d refuted finding kept as knowledge",
              rej is not None and rej[1] == "HYPOTHESIS_REFUTED",
              f"f4 row: {rej}")
        promoted = conn.execute(
            "SELECT COUNT(*) FROM findings WHERE admission='retained' "
            "AND finding_id IN ('f3','f5','f8')").fetchone()[0]
        check("C10e retained never promoted", promoted == 3,
              "irrelevant findings present but flagged retained, not admitted")
    finally:
        conn.close()

    # -- C11: determinism across independent gates ---------------------------
    g_a = RelevanceGate(objective=OBJECTIVE,
                        store_path=os.path.join(workdir, "a.db"))
    g_b = RelevanceGate(objective=OBJECTIVE,
                        store_path=os.path.join(workdir, "b.db"))
    f_det = mk("f_det",
               "detecting genuine boundary conditions requires observing "
               "loop state at runtime",
               "obj-det-2", False, "QUESTION_RESOLVED")
    da, db = g_a.decide(f_det), g_b.decide(f_det)
    check("C11 deterministic decisions",
          (da.verdict, da.criterion, round(da.score, 6))
          == (db.verdict, db.criterion, round(db.score, 6)),
          f"gate A: {da.verdict}/{da.criterion}/{da.score:.3f}; "
          f"gate B: {db.verdict}/{db.criterion}/{db.score:.3f}")

    # -- C12: executive integration -- real controller, real gate ----------
    exec_gate = RelevanceGate(
        objective=OBJECTIVE,
        store_path=os.path.join(workdir, "exec.db"))
    executive = ExecutiveController(
        engine=object(), run_controller=object(), gap_registry=object(),
        acceptance_loop=object(), epistemic=object(),
        absent_loops=LOOPS, relevance_gate=exec_gate)
    for fid, want in (("f1", ADMITTED), ("f3", RETAINED), ("f4", REJECTED)):
        f = {"f1": f1, "f3": f3, "f4": f4}[fid]
        # fresh finding objects: new ids so the exec store is independent
        f2_ = Finding(fid + "_exec", f.content, f.provenance,
                       f.terminal_state)
        d = executive.submit_finding(f2_)
        check(f"C12 executive inlet {fid}",
              d.verdict == want, f"{fid} -> {d.verdict} via executive")
    gateless = ExecutiveController(
        engine=object(), run_controller=object(), gap_registry=object(),
        acceptance_loop=object(), epistemic=object(), absent_loops=LOOPS)
    try:
        gateless.submit_finding(f1)
        check("C12b gateless executive fail-closed", False,
              "submit_finding should have raised RelevanceRefused")
    except RelevanceRefused as exc:
        check("C12b gateless executive fail-closed", True, str(exc)[:80])
    try:
        gateless.set_operational_objective(OBJECTIVE)
        check("C12c gateless set_objective fail-closed", False,
              "should have raised RelevanceRefused")
    except RelevanceRefused:
        check("C12c gateless set_objective fail-closed", True, "")

    LOG.append("")
    LOG.append("ALL CHECKS PASSED")
    log_path = os.path.join(os.path.dirname(__file__),
                            "ploop7_relevance_proof.log")
    with open(log_path, "w") as fh:
        fh.write("\n".join(LOG) + "\n")
    print("\n".join(LOG))
    print(f"\nlog: {log_path}")


if __name__ == "__main__":
    main()
