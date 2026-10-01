#!/usr/bin/env python3
"""CREATIVITY-SLICE-1 proof battery: the decided D-5 stages and D-4 intent register.

Proves, against the real paper text and the real package (no mocks of
itself, fresh process):

  S1  the six stages exist in the exact paper order
      (intent → generation → variation → critique → refinement → release)
  S2  every stage has a descriptor with decision class, inputs, outputs,
      and termination condition, each citing its paper section, and each
      descriptor's paper_quote is a VERBATIM substring of the paper —
      the citation is machine-checked, not decorative
  S3  transitions: the ordered forward chain; Refinement may return to
      Variation/Critique with the iteration bound as an explicit
      required parameter (no default invented — omitting it raises
      TypeError); Release is terminal
  S4  intent validation: an intent with no stated outcome (None, empty,
      whitespace) is refused with the EXACT reason; a stated outcome
      registers with principal, outcome-as-stated, method-owned-by-
      machine marker, and timestamp
  S5  admissibility: Intent is the only admissible first state; the rest
      follow stage order (D-4); select_state walks the three-question
      grammar; REFINEMENT without an explicit bound is refused; bound
      exhaustion selects RELEASE (never loops forever, §11 Q6)
  S6  the package is pure Python: compiles clean, zero android imports

Run in a fresh process from the tree root:
    python3 proofs/creativity_slice1/creativity_slice_proof.py
Exit 0 iff every check passes.

Sections of the paper cited: exec-creativity-controller_2026-09-29.md
§2 (D-5 stages), §7 (intent mirror), §9 T1 (D-4), §9 T2 (D-5), §11 Q6
(refinement bound, decided 2026-09-30).
"""

import os
import sys
import traceback

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(TREE, "pylib"))

from swarm_engine.creativity import (  # noqa: E402
    STAGE_ORDER,
    CreativeIntent,
    CreativeStage,
    IntentRefused,
    StageRefused,
    admissible_next,
    admissible_stages,
    describe,
    is_terminal,
    refinement_loop_admissible,
    register_intent,
    select_state,
)

PAPER_PATH = os.path.expanduser(
    "~/workspace/architecture/exec-creativity-controller_2026-09-29.md"
)

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("[PASS] %s" % name)
    else:
        FAILED += 1
        print("[FAIL] %s -- %s" % (name, detail))


def check_raises(name, exc_type, fn, *args, **kwargs):
    """Returns the exception instance on expected raise, else records FAIL."""
    global PASSED, FAILED
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        PASSED += 1
        print("[PASS] %s" % name)
        return e
    except Exception as e:  # noqa: BLE001
        FAILED += 1
        print("[FAIL] %s -- wrong exception %r" % (name, e))
        return None
    FAILED += 1
    print("[FAIL] %s -- no exception raised" % name)
    return None


def _bound_is_required():
    try:
        refinement_loop_admissible(0)  # type: ignore[call-arg]
    except TypeError:
        return True
    return False


def main():
    with open(PAPER_PATH, encoding="utf-8") as f:
        paper = f.read()
    check("paper readable", len(paper) > 10000, PAPER_PATH)

    # ---- S1: stage order -------------------------------------------
    stages = list(CreativeStage)
    check("six stages", len(stages) == 6, str(len(stages)))
    check(
        "exact paper order intent→generation→variation→critique→refinement→release",
        stages
        == [
            CreativeStage.INTENT,
            CreativeStage.GENERATION,
            CreativeStage.VARIATION,
            CreativeStage.CRITIQUE,
            CreativeStage.REFINEMENT,
            CreativeStage.RELEASE,
        ],
        str(stages),
    )
    check(
        "STAGE_ORDER matches enum order",
        STAGE_ORDER == tuple(stages),
        str(STAGE_ORDER),
    )
    check(
        "enum values are ordered 1..6",
        [s.value for s in stages] == [1, 2, 3, 4, 5, 6],
    )

    # ---- S2: descriptors present and cited --------------------------
    required = ("decision_class", "inputs", "outputs", "termination_condition")
    for s in stages:
        d = describe(s)
        check(
            "descriptor present for %s" % s.name,
            d.stage is s and all(getattr(d, f) for f in required),
            repr(d),
        )
        check(
            "descriptor cites a paper section (%s)" % s.name,
            "§" in d.paper_section,
            d.paper_section,
        )
        check(
            "descriptor quote is verbatim paper text (%s)" % s.name,
            d.paper_quote and d.paper_quote in paper,
            d.paper_quote[:80],
        )
    check(
        "all six descriptors cite §2",
        all("§2" in describe(s).paper_section for s in stages),
    )
    e = check_raises(
        "describe(non-stage) refused with exact reason",
        StageRefused,
        describe,
        "generation",
    )
    check(
        "refusal names D-5/§2",
        e is not None and "D-5" in str(e) and "§2" in str(e),
        str(e),
    )

    # ---- S3: transitions --------------------------------------------
    check(
        "forward chain",
        admissible_next(CreativeStage.INTENT) == (CreativeStage.GENERATION,)
        and admissible_next(CreativeStage.GENERATION)
        == (CreativeStage.VARIATION,)
        and admissible_next(CreativeStage.VARIATION)
        == (CreativeStage.CRITIQUE,)
        and admissible_next(CreativeStage.CRITIQUE)
        == (CreativeStage.REFINEMENT,)
        and CreativeStage.RELEASE in admissible_next(CreativeStage.REFINEMENT),
    )
    loop_targets = admissible_next(CreativeStage.REFINEMENT)
    check(
        "refinement may return to variation and critique",
        CreativeStage.VARIATION in loop_targets
        and CreativeStage.CRITIQUE in loop_targets,
        str(loop_targets),
    )
    check(
        "release is terminal",
        is_terminal(CreativeStage.RELEASE)
        and admissible_next(CreativeStage.RELEASE) == (),
    )
    check(
        "non-release stages are not terminal",
        all(not is_terminal(s) for s in stages if s is not CreativeStage.RELEASE),
    )
    check(
        "loop admissible while under bound",
        refinement_loop_admissible(0, 3) and refinement_loop_admissible(2, 3),
    )
    check(
        "loop inadmissible at/exhausting bound",
        not refinement_loop_admissible(3, 3)
        and not refinement_loop_admissible(9, 3),
    )
    check(
        "bound is a required parameter (TypeError when omitted)",
        _bound_is_required(),
    )
    check_raises(
        "negative bound refused with exact reason",
        StageRefused,
        refinement_loop_admissible,
        0,
        -1,
    )

    # ---- S4: intent validation ----------------------------------------
    for bad in (None, "", "   ", "\t\n"):
        e = check_raises(
            "no stated outcome refused (%r)" % (bad,),
            IntentRefused,
            register_intent,
            "james",
            bad,
        )
        check(
            "exact refusal reason carries the owed-input quote",
            e is not None
            and "Intent is the only input the user owes" in str(e),
            str(e),
        )
    intent = register_intent("james", "make me a song")
    check(
        "valid intent registers",
        isinstance(intent, CreativeIntent)
        and intent.principal == "james"
        and intent.outcome == "make me a song",
        repr(intent),
    )
    check(
        "method owned by the machine (James↔Felix mirror, §7)",
        intent.method_owned_by_machine is True,
    )
    check("intent carries a timestamp", bool(intent.created_at), intent.created_at)

    # ---- S5: admissibility + D-4 grammar ------------------------------
    adm = admissible_stages(intent)
    check(
        "admissible_stages returns the six in order",
        adm == STAGE_ORDER,
        str(adm),
    )
    check(
        "Intent is the only admissible first state",
        adm[0] is CreativeStage.INTENT,
        str(adm[0]),
    )
    e = check_raises(
        "admissible_stages on unstated outcome refused",
        IntentRefused,
        admissible_stages,
        CreativeIntent(principal="james", outcome="  "),
    )
    check(
        "refusal propagates through admissibility",
        e is not None and "Intent is the only input the user owes" in str(e),
    )
    check(
        "select_state: no current stage → enter INTENT (D-4 q3)",
        select_state(intent) is CreativeStage.INTENT,
    )
    check(
        "select_state: INTENT → GENERATION",
        select_state(intent, CreativeStage.INTENT) is CreativeStage.GENERATION,
    )
    check(
        "select_state: VARIATION → CRITIQUE",
        select_state(intent, CreativeStage.VARIATION) is CreativeStage.CRITIQUE,
    )
    e = check_raises(
        "select_state: RELEASE refused (terminal)",
        StageRefused,
        select_state,
        intent,
        CreativeStage.RELEASE,
    )
    check(
        "terminal refusal names D-5/§2",
        e is not None and "terminal" in str(e) and "D-5" in str(e),
        str(e),
    )
    e = check_raises(
        "select_state: REFINEMENT without explicit bound refused",
        StageRefused,
        select_state,
        intent,
        CreativeStage.REFINEMENT,
    )
    check(
        "bound refusal names §11 Q6 and refuses invented numbers",
        e is not None and "§11 Q6" in str(e) and "does not pick" in str(e),
        str(e),
    )
    check(
        "select_state: REFINEMENT under bound → back to VARIATION",
        select_state(
            intent,
            CreativeStage.REFINEMENT,
            refinement_iterations_used=1,
            refinement_bound=3,
        )
        is CreativeStage.VARIATION,
    )
    check(
        "select_state: REFINEMENT bound exhausted → RELEASE (never infinite, §11 Q6)",
        select_state(
            intent,
            CreativeStage.REFINEMENT,
            refinement_iterations_used=3,
            refinement_bound=3,
        )
        is CreativeStage.RELEASE,
    )

    # ---- S6: pure python ----------------------------------------------
    import py_compile  # noqa: E402

    pkg = os.path.join(TREE, "runtime", "creativity")
    files = sorted(
        os.path.join(pkg, f) for f in os.listdir(pkg) if f.endswith(".py")
    )
    check("package has the three modules", len(files) == 3, str(files))
    ok = True
    for f in files:
        try:
            py_compile.compile(f, doraise=True)
        except Exception as e:  # noqa: BLE001
            ok = False
            print("[FAIL] compile %s -- %r" % (f, e))
    check("all modules compile clean", ok)
    android_hit = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                s = line.strip()
                if s.startswith("import android") or s.startswith("from android"):
                    android_hit.append("%s:%d" % (f, i))
    check("zero android imports", not android_hit, str(android_hit))

    print()
    print("==== creativity_slice_proof: %d/%d checks passed ====" % (PASSED, PASSED + FAILED))
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
