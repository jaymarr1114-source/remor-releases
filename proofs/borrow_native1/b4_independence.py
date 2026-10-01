"""b4: independence — the teacher is removed and the technique still works.

Builds GrantedCognitionProvider with:
  native  = StepVerifyNativeV2 (the DISTILLED primitive, owned code)
  teacher = RaisingTeacher (explodes if the borrow path ever runs)

On FRESH tasks (never in the delta evidence), request_cognition is issued
with NO grant in context. Expected: the native tier answers from the owned
primitive — no Qwen3 process, no borrowed provenance, no grant consumed.

Proves mandate 6: the technique survives WITHOUT the teacher.
"""
import sys
import os
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SEC = "b4"

# Fresh tasks: none of these (a,b) pairs appear in b2's demos.
FRESH = [
    (41, 59), (200, 88), (14, 13), (99, 1), (33, 67), (123, 456),
]


def llama_procs():
    out = subprocess.run(["pgrep", "-x", "llama-cli"],
                         capture_output=True, text=True)
    return [p for p in out.stdout.split() if p.strip()]


class StepVerifyNativeV2(C.CognitionProvider):
    """Post-distillation native tier: the owned primitive handles the
    task class. No teacher, no grant, no borrow."""

    def __init__(self, engine, promoted_name):
        self._eng = engine
        self._pname = promoted_name

    def request_cognition(self, *, mc_id, prompt, context):
        from swarm_engine.core.microcontroller.granted_cognition import (
            CognitionResult)
        if (context or {}).get("purpose") != C.PURPOSE:
            raise C.NativeRefusal("unknown_purpose", "v2 handles compute_sum")
        kw = {k: context[k] for k in ("a", "b")}
        total = self._eng.primitives.invoke_sync(self._pname, **kw)
        return CognitionResult(
            ok=True, text=f"native sum: {total}",
            provenance="",  # provider stamps native:<class>
            native_refusal="")


def main():
    from swarm_engine.core.microcontroller.granted_cognition import (
        GrantedCognitionProvider)

    with open(C.DISTILL_PATH) as fh:
        dr = json.load(fh)
    assert dr.get("success"), f"distillation did not succeed: {dr}"
    pname = dr["promoted_name"]

    os.environ["REMOR_TEST_MODE"] = "1"
    from runtime.core.engine import SwarmEngine
    eng = SwarmEngine(db_path=C.ENGINE_DB, _test_allow_shared=True)
    C.check(SEC, "primitive_still_registered",
            eng.primitives.get(pname) is not None, pname)

    before_procs = llama_procs()
    C.check(SEC, "no_llama_before", not before_procs,
            str(before_procs))

    sub, mc_id = C.fresh_substrate()
    prov = GrantedCognitionProvider(
        substrate=sub,
        native=StepVerifyNativeV2(eng, pname),
        teacher=C.RaisingTeacher())  # explodes if borrow attempted

    n_ok = 0
    for i, (a, b) in enumerate(FRESH):
        expected = C.ground_truth(a, b)
        res = prov.request_cognition(
            mc_id=mc_id, prompt=C.step_prompt(a, b),
            context={"purpose": C.PURPOSE, "a": a, "b": b})  # NOTE: no grant
        tag = f"fresh{i}"
        C.check(SEC, f"{tag}_native_ok", res.ok is True,
                (res.error or "")[:100])
        if not res.ok:
            continue
        C.check(SEC, f"{tag}_native_provenance",
                (res.provenance or "").startswith("native:"),
                res.provenance)
        C.check(SEC, f"{tag}_no_borrowed_provenance",
                "borrowed:" not in (res.provenance or ""),
                res.provenance)
        # strict check: ask the promoted primitive directly
        direct = eng.primitives.invoke_sync(pname, a=a, b=b)
        C.check(SEC, f"{tag}_correct", direct == expected,
                f"direct={direct} expected={expected}")
        if direct == expected:
            n_ok += 1

    after_procs = llama_procs()
    C.check(SEC, "no_llama_after", not after_procs, str(after_procs))
    C.check(SEC, "teacher_never_called", True,
            "")  # RaisingTeacher would have raised AssertionError
    C.check(SEC, "all_fresh_correct", n_ok == len(FRESH),
            f"{n_ok}/{len(FRESH)}")

    ok = C.summarize(SEC)
    print(("BATTERY b4 " + "PASS") if ok else "BATTERY b4 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
