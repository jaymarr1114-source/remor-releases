#!/usr/bin/env python3
"""INTEGRATE-1 S: the shakedown — cross-module interface coherence.

Every track module is exercised through the executive's REAL wiring
(not around it), asserting the producer/consumer contracts the
pipeline depends on: signature contracts (James's gates, trust
boundaries) and field contracts on real objects. If any module's
interface drifted from what its consumer expects, this fails —
that is the point.

Exit 0 iff every check passes.
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as F  # noqa: E402
import runtime.creativity as pkg  # noqa: E402
from runtime.creativity import (  # noqa: E402
    CompositionVerdict,
    LedgerEntry,
    admit,
    critique_candidate,
)
from runtime.creativity.budget import COMPUTE, CreativityBudget  # noqa: E402
from runtime.creativity.executive import (  # noqa: E402
    CreativityExecutiveController,
)
from runtime.creativity.run_controller import (  # noqa: E402
    CreativityRunController,
)
from runtime.creativity.stages import (  # noqa: E402
    STAGE_ORDER,
    CreativeStage,
    admissible_next,
    is_terminal,
)

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


def s1_package_surface():
    check("s1 package exports all eight modules' surface",
          len(pkg.__all__) == 66, str(len(pkg.__all__)))
    missing = [n for n in pkg.__all__ if not hasattr(pkg, n)]
    check("s1 every exported name resolves", not missing, str(missing))
    for mod, cls in [("stages", "CreativeStage"), ("intent", "CreativeIntent"),
                     ("ledger", "Ledger"), ("critique", "CritiqueReport"),
                     ("release", "AdmissionRecord"),
                     ("budget", "CreativityBudget"),
                     ("executive", "CreativityExecutiveController"),
                     ("run_controller", "CreativityRunController")]:
        check(f"s1 module {mod} exports {cls}",
              hasattr(getattr(pkg, mod, None), cls) or hasattr(pkg, cls),
              mod)


def s2_signature_contracts():
    sig = inspect.signature(CreativityExecutiveController.commission)
    rb = sig.parameters.get("refinement_bound")
    check("s2 commission: refinement_bound required keyword (James's gate)",
          rb is not None and rb.kind == inspect.Parameter.KEYWORD_ONLY
          and rb.default is inspect.Parameter.empty, str(sig))

    sig = inspect.signature(CreativityRunController.__init__)
    rb = sig.parameters.get("refinement_bound")
    check("s2 run controller: refinement_bound required keyword",
          rb is not None and rb.kind == inspect.Parameter.KEYWORD_ONLY
          and rb.default is inspect.Parameter.empty, str(sig))

    sig = inspect.signature(admit)
    params = set(sig.parameters)
    check("s2 admit takes pipeline inputs (ledger/intent/candidate/...)",
          {"ledger", "primitive_ids", "intent", "candidate",
           "provenance_store", "composer", "primitives",
           "repo_root", "registry"} <= params, str(params))
    check("s2 admit has NO verdict/report parameter (trust boundary)",
          not ({"verdict", "report", "critique_report"} & params),
          str(params))

    sig = inspect.signature(critique_candidate)
    check("s2 critique_candidate requires the ledger verdict",
          "verdict" in sig.parameters
          and sig.parameters["verdict"].kind
          == inspect.Parameter.KEYWORD_ONLY, str(sig))

    sig = inspect.signature(CreativityBudget.__init__)
    check("s2 budget needs its spend store path (no implicit store)",
          "spend_db_path" in sig.parameters
          and sig.parameters["spend_db_path"].kind
          == inspect.Parameter.KEYWORD_ONLY, str(sig))


def s3_field_contracts():
    w = F.make_world(("add", "mul"), compute_bound=60.0,
                     work_id="s3_work", prefix="integ1s3_")
    ex = w["ex"]

    # Ledger -> critique: the legality gate's expected verdict shape.
    verdict = ex.ledger.check_composition(["add"])
    check("s3 verdict is a CompositionVerdict",
          isinstance(verdict, CompositionVerdict))
    for field in ("legal", "illegal", "checked", "gaps", "absent"):
        check(f"s3 verdict carries .{field} (critique's contract)",
              hasattr(verdict, field))
    check("s3 verdict.checked covers exactly the composition",
          list(verdict.checked) == ["add"] and verdict.legal is True,
          f"{verdict.checked} {verdict.legal}")

    # Ledger entry round-trip through the real ledger.
    check("s3 ledger indexes the fixture entries",
          set(ex.ledger.indexed()) == {"add", "mul"},
          str(ex.ledger.indexed()))

    # Budget -> run controller: the envelope the grant path used.
    bound = w["budget"].envelope.bound_for(COMPUTE).bound
    check("s3 envelope bound is what the controller enforced",
          bound == 60.0, str(bound))

    # Executive -> run controller: the outcome's contract.
    intent = F.make_intent()
    commission = ex.commission(intent, refinement_bound=2)
    outcome = ex.run(commission)
    for field in ("status", "stage", "detail", "admission",
                  "gaps", "absent", "candidates_considered"):
        check(f"s3 outcome carries .{field}", hasattr(outcome, field))
    check("s3 outcome admission is the release module's record",
          outcome.admission is not None
          and type(outcome.admission).__name__ == "AdmissionRecord",
          str(type(outcome.admission)))
    check("s3 outcome counts the candidates considered",
          outcome.candidates_considered > 0,
          str(outcome.candidates_considered))

    # Run controller -> caller: the RunRecord contract on a real run.
    rec = w["ctl"].run(intent, estimated_compute_s=2.0)
    names = [p.name for p in rec.predicates]
    check("s3 run record carries the four named predicates",
          names == ["no_kill_active", "intent_present",
                    "executive_runnable", "budget_granted"], str(names))
    for field in ("run_id", "work_id", "intent_summary",
                  "refinement_bound", "grant_issued", "grant_size_s",
                  "status", "outcome_status", "outcome_detail",
                  "spend_s", "ceiling_stop", "kill_seen"):
        check(f"s3 run record carries .{field}", hasattr(rec, field))

    # Ledger -> gap registry: the named-gap contract.
    w2 = F.make_world(("add",), compute_bound=60.0, work_id="s3b_work",
                      prefix="integ1s3b_")
    ex2 = w2["ex"]
    o2 = ex2.run(ex2.commission(
        F.make_intent(), refinement_bound=2,
        constraints=[{"kind": "requires_primitive",
                      "primitive": "unproven_psi"}]))
    check("s3 gap demand recorded on the outcome", len(o2.gaps) > 0,
          str(o2.gaps))
    g = F.gap_registry_for(w2["tmp"]).get(o2.gaps[0]) if o2.gaps else None
    check("s3 registry gap names the demanded primitive",
          g is not None and "unproven_psi" in (g.summary or ""),
          g.summary if g else None)


def s4_stage_wiring():
    w = F.make_world(("add",), compute_bound=60.0, work_id="s4_work",
                     prefix="integ1s4_")
    loops = w["ex"]._loops
    check("s4 executive wires all six stage controllers",
          [s for s in STAGE_ORDER] == list(loops.keys()),
          str(list(loops.keys())))
    check("s4 stage order is the paper's D-5 order",
          [s.name for s in STAGE_ORDER] ==
          ["INTENT", "GENERATION", "VARIATION", "CRITIQUE",
           "REFINEMENT", "RELEASE"],
          str([s.name for s in STAGE_ORDER]))
    check("s4 release is terminal",
          is_terminal(CreativeStage.RELEASE) is True
          and admissible_next(CreativeStage.RELEASE) == (),
          f"is_terminal={is_terminal(CreativeStage.RELEASE)} "
          f"admissible_next={admissible_next(CreativeStage.RELEASE)}")


def main():
    s1_package_surface()
    s2_signature_contracts()
    s3_field_contracts()
    s4_stage_wiring()
    print(f"\n==== s_shakedown: {PASSED}/{PASSED + FAILED} checks passed ====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
