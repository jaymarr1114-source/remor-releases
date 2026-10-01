"""b2: the borrow — Qwen3 demonstrates step verification under real grants.

For each task instance: issue a REAL FrmGrant, run the borrow through
GrantedCognitionProvider (native refuses -> borrow unlocks), verify the
teacher's verdict against ground truth, and checkpoint the verified
demonstrations to demos.json.

Checkpoint: if demos.json exists with >=8 verified demonstrations that
re-validate against ground truth, inference is skipped (resumable).
Pass --force to redo all inference.
"""
import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SEC = "b2"
FORCE = "--force" in sys.argv

# 12 task instances: (a, b). Diverse magnitudes.
TASKS = [
    (2, 3), (5, 7), (10, 20), (1, 1), (15, 25), (3, 8),
    (12, 4), (9, 6), (7, 13), (25, 30), (8, 12), (40, 60),
]


def load_checkpoint():
    if os.path.isfile(C.DEMOS_PATH) and not FORCE:
        with open(C.DEMOS_PATH) as fh:
            doc = json.load(fh)
        demos = doc.get("demonstrations", [])
        ok = True
        for d in demos:
            inp = d["input"]
            if C.ground_truth(inp["a"], inp["b"]) != d["output"]:
                ok = False
        if ok and len(demos) >= 10 and doc.get("provenance_ok"):
            return doc
    return None


def main():
    from swarm_engine.core.microcontroller.granted_cognition import (
        GrantedCognitionProvider)

    doc = load_checkpoint()
    if doc is not None:
        print(f"b2: checkpoint valid "
              f"({len(doc['demonstrations'])} verified demos), "
              f"skipping inference")
        demos = doc["demonstrations"]
    else:
        demos = []
        teacher = C.real_teacher()
        C.check(SEC, "teacher_revision",
                teacher.revision == C.QWEN3_REV, teacher.revision)
        for i, (a, b) in enumerate(TASKS):
            sub, mc_id = C.fresh_substrate()
            prov = GrantedCognitionProvider(
                substrate=sub, native=C.StepVerifyNativeV1(),
                teacher=teacher)
            grant = C.make_grant(400.0, note=f"borrow-native-1/b2/{i}")
            before = prov.grant_consumed_s(grant.grant_id)
            res = prov.request_cognition(
                mc_id=mc_id, prompt=C.step_prompt(a, b),
                context={"purpose": C.PURPOSE, "a": a, "b": b,
                         "frm_grant": grant})
            after = prov.grant_consumed_s(grant.grant_id)
            tag = f"task{i}"
            C.check(SEC, f"{tag}_ok", res.ok is True,
                    (res.error or "")[:100])
            if not res.ok:
                print(f"  borrow failed on task {i}: {(res.error or '')[:150]}")
                continue
            C.check(SEC, f"{tag}_provenance",
                    res.provenance == f"borrowed:qwen3@{C.QWEN3_REV}",
                    res.provenance)
            C.check(SEC, f"{tag}_charged", after > before,
                    f"consumed {before}->{after}")
            C.check(SEC, f"{tag}_native_refusal_named",
                    res.native_refusal == C.REFUSAL_NAME,
                    str(res.native_refusal))
            result_val = C.parse_result(res.text or "")
            expected = C.ground_truth(a, b)
            if result_val is None:
                print(f"  task {i}: unparseable teacher text: "
                      f"{(res.text or '')[:200]!r}", flush=True)
                continue
            if result_val != expected:
                print(f"  task {i}: teacher WRONG "
                      f"(said {result_val}, truth {expected}); discarding",
                      flush=True)
                continue
            demos.append({
                "input": {"a": a, "b": b},
                "output": expected,
                "teacher_text": res.text,
                "grant_id": grant.grant_id,
                "charged_s": after - before,
                "provenance": res.provenance,
            })
            print(f"  task {i}: verified "
                  f"(charged {after - before:.1f}s)", flush=True)
        doc = {"demonstrations": demos, "provenance_ok": True,
               "discarded": len(TASKS) - len(demos)}
        with open(C.DEMOS_PATH, "w") as fh:
            json.dump(doc, fh, indent=1)
        print(f"b2: {len(demos)}/{len(TASKS)} verified demonstrations saved")

    C.check(SEC, "enough_evidence", len(demos) >= 10, f"{len(demos)}")
    # every demo's output matches ground truth (re-validated)
    all_ok = all(
        C.ground_truth(d["input"]["a"], d["input"]["b"]) == d["output"]
        for d in demos)
    C.check(SEC, "all_verified", all_ok, "")
    # every demo carries borrowed provenance at the pinned revision
    prov_ok = all(d.get("provenance") == f"borrowed:qwen3@{C.QWEN3_REV}"
                  for d in demos)
    C.check(SEC, "provenance_ok", prov_ok, "")
    charged_ok = all(d.get("charged_s", 0) > 0 for d in demos)
    C.check(SEC, "all_charged", charged_ok, "")

    ok = C.summarize(SEC)
    print(("BATTERY b2 " + "PASS") if ok else "BATTERY b2 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
