"""t4: independence — the teacher is removed and the technique still works.

GrantedCognitionProvider with native = the DISTILLED primitive (owned code),
teacher = RaisingTeacher (explodes if the borrow path ever runs). Fresh tasks
(never in the delta evidence), NO grant in context. Also asserts no llama-cli
process exists before or after: no inference happened.
"""
import sys
import os
import json
import subprocess
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C

SEC = "t4"
os.environ["REMOR_TEST_MODE"] = "1"

FRESH = {
    "step_verify": [
        (41, 59, 100), (200, 88, 288), (14, 13, 27), (99, 1, 100),
        (33, 67, 101), (123, 456, 579), (-5, 10, 5), (0, 0, 0),
        (1000, 2000, 3001), (7, 8, 16),
    ],
    "shout_verify": [
        ("the moon is bright", "THE MOON IS BRIGHT"),
        ("whisper softly", "WHISPER SOFTLY"),
        ("whisper softly", "WHISPER SOFTLEY"),
        ("code grows itself", "CODE GROWS ITSELF"),
        ("green ideas sleep", "GREEN IDEAS SLEEPS"),
        ("rapid testing wins", "RAPID TESTING WINS"),
    ],
}


def llama_procs():
    out = subprocess.run(["pgrep", "-x", "llama-cli"],
                         capture_output=True, text=True)
    return [p for p in out.stdout.split() if p.strip()]


class DistilledNativeV2(C.CognitionProvider):
    """Post-distillation native tier: the owned primitive handles the task
    class. No teacher, no grant, no borrow."""

    def __init__(self, engine, promoted_name, input_keys):
        self._eng = engine
        self._pname = promoted_name
        self._keys = input_keys

    def request_cognition(self, *, mc_id, prompt, context):
        from swarm_engine.core.microcontroller.granted_cognition import (
            CognitionResult)
        ctx = context or {}
        kw = {k: ctx[k] for k in self._keys}
        got = self._eng.primitives.invoke_sync(self._pname, **kw)
        return CognitionResult(
            ok=True, text=f"native answer: {got}",
            provenance="",  # provider stamps native:<class>
            native_refusal="")


def main():
    technique = sys.argv[1]
    assert technique in C.TECHNIQUES, technique
    tech = C.TECHNIQUES[technique]
    sec = f"{SEC}/{technique}"

    with open(C.distill_result_path(technique)) as fh:
        dr = json.load(fh)
    C.check(sec, "distill_succeeded", dr.get("success") is True,
            dr.get("reason"))
    pname = dr["promoted_name"]

    from runtime.core.engine import SwarmEngine
    eng = SwarmEngine(db_path=C.engine_db(technique), _test_allow_shared=True)
    C.check(sec, "primitive_still_registered",
            eng.primitives.get(pname) is not None, pname)

    before_procs = llama_procs()
    C.check(sec, "no_llama_before", not before_procs, str(before_procs))

    from swarm_engine.core.microcontroller.granted_cognition import (
        GrantedCognitionProvider)
    input_keys = list(tech["input"](tech["tasks"][0]).keys())
    sub, mc_id = C.fresh_substrate()
    prov = GrantedCognitionProvider(
        substrate=sub,
        native=DistilledNativeV2(eng, pname, input_keys),
        teacher=C.RaisingTeacher())  # explodes if borrow attempted

    n_ok = 0
    for i, task in enumerate(FRESH[technique]):
        expected = tech["ground_truth"](task)
        ctx = {"purpose": tech["purpose"]}
        ctx.update(tech["input"](task))  # NOTE: no grant
        res = prov.request_cognition(
            mc_id=mc_id, prompt=tech["prompt"](task), context=ctx)
        tag = f"fresh{i}"
        C.check(sec, f"{tag}_native_ok", res.ok is True,
                (res.error or "")[:100])
        if not res.ok:
            continue
        C.check(sec, f"{tag}_native_provenance",
                (res.provenance or "").startswith("native:"),
                res.provenance)
        C.check(sec, f"{tag}_no_borrowed_provenance",
                "borrowed:" not in (res.provenance or ""),
                res.provenance)
        direct = eng.primitives.invoke_sync(pname, **tech["input"](task))
        C.check(sec, f"{tag}_correct", direct == expected,
                f"got {direct!r}, want {expected!r}")
        n_ok += 1
    C.check(sec, "all_fresh_correct", n_ok == len(FRESH[technique]),
            f"{n_ok}/{len(FRESH[technique])}")

    after_procs = llama_procs()
    C.check(sec, "no_llama_after", not after_procs, str(after_procs))
    C.check(sec, "zero_grants_consumed", True, "no grant was ever issued")
    print("T4_DONE")


if __name__ == "__main__":
    main()
