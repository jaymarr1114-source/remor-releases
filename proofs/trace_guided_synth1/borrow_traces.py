"""borrow_traces: borrow demonstration TRACES from Qwen3-8B (real inference).

Usage: borrow_traces.py <technique> [--force]
  technique: step_verify | shout_verify

For each task: a real FrmGrant borrows the teacher through
GrantedCognitionProvider (native tier refuses by name first). The teacher's
full labeled trace is checkpointed. A demo is *verified* only if: the borrow
succeeded with borrowed provenance, the grant was charged, the native refusal
was named, the result line parsed and matched ground truth, the trace has
>=1 work line, and every input value is echoed in the trace work
(case-insensitive). The battery selects the first 12 verified demos sharing
the largest consistent work-line count.

Checkpointed + resumable: attempts.json records every attempt; a reboot
costs at most one inference.
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C
from runtime.synthesis import trace_guided as tg

SEC = "borrow"


def attempt_task(tech, purpose, idx, task):
    from swarm_engine.core.microcontroller.granted_cognition import (
        GrantedCognitionProvider)
    sub, mc_id = C.fresh_substrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=C.TraceGuidedNativeV1(),
        teacher=C.real_teacher())
    grant = C.make_grant(400.0, note=f"trace-guided-synth-1/borrow/{purpose}/{idx}")
    before = prov.grant_consumed_s(grant.grant_id)
    res = prov.request_cognition(
        mc_id=mc_id, prompt=tech["prompt"](task),
        context={"purpose": purpose, "frm_grant": grant})
    after = prov.grant_consumed_s(grant.grant_id)
    rec = {
        "task_index": idx,
        "input": tech["input"](task),
        "expected": tech["ground_truth"](task),
        "grant_id": grant.grant_id,
        "charged_s": round(after - before, 1),
        "verified": False,
    }
    if not res.ok:
        rec["skip_reason"] = f"borrow failed: {(res.error or '')[:150]}"
        return rec
    rec["teacher_text"] = res.text or ""
    rec["provenance"] = res.provenance
    rec["native_refusal"] = res.native_refusal
    checks = [
        (res.provenance == f"borrowed:qwen3@{C.QWEN3_REV}", "provenance"),
        (after > before, "charged"),
        (res.native_refusal == C.REFUSAL_NAME, "native_refusal_named"),
    ]
    for cond, name in checks:
        if not cond:
            rec["skip_reason"] = f"check failed: {name}"
            return rec
    pt = tg.parse_labeled_trace(res.text or "")
    rec["work_lines"] = len(pt.work)
    if not pt.work or pt.result_text is None:
        rec["skip_reason"] = "trace missing work/result lines"
        return rec
    try:
        result_val = tg.result_value(pt.result_text)
    except Exception:
        result_val = None
    rec["result_parsed"] = result_val
    if result_val != tech["ground_truth"](task):
        rec["skip_reason"] = (
            f"teacher result {result_val!r} != ground truth "
            f"{tech['ground_truth'](task)!r}")
        return rec
    # Input-echo check (generic): every input value's string form appears
    # in the trace work (case-insensitive) — the teacher worked THIS demo.
    work_text = "\n".join(c for _, c in pt.work).lower()
    for v in tech["input"](task).values():
        if str(v).lower() not in work_text:
            rec["skip_reason"] = f"input value {v!r} not echoed in trace"
            rec["echo_ok"] = False
            return rec
    rec["echo_ok"] = True
    rec["verified"] = True
    return rec


def select_demos(attempts, need=12):
    verified = [a for a in attempts if a.get("verified")]
    groups = {}
    for a in verified:
        groups.setdefault(a["work_lines"], []).append(a)
    if not groups:
        return None, 0
    k = max(groups, key=lambda kk: len(groups[kk]))
    return groups[k][:need], k


def main():
    technique = sys.argv[1]
    force = "--force" in sys.argv
    assert technique in C.TECHNIQUES, technique
    tech = C.TECHNIQUES[technique]
    purpose = tech["purpose"]
    sec = f"{SEC}/{technique}"
    att_path = C.attempts_path(technique)
    sel_path = C.selected_path(technique)

    if not force and os.path.exists(sel_path):
        with open(sel_path) as fh:
            sel = json.load(fh)
        demos = sel.get("demonstrations", [])
        if (len(demos) == 12 and
                all(d.get("verified") for d in demos)):
            print(f"{sec}: checkpoint valid "
                  f"({len(demos)} verified demos), skipping inference")
            print("BORROW_DONE")
            return

    attempts = []
    if os.path.exists(att_path) and not force:
        with open(att_path) as fh:
            attempts = json.load(fh).get("attempts", [])
        print(f"{sec}: resumed with {len(attempts)} prior attempts")
    # Transport failures (host problems: OOM-kill, timeout) are RETRIED,
    # not skipped: a prior attempt whose skip_reason starts with
    # "exception:" does not count as done. Without this, a resume after
    # a bad host window permanently loses those tasks and the 12-demo
    # bar becomes unreachable (observed 2026-10-02: 5 transport failures
    # left only 11 viable tasks of 16). Verification failures (the model
    # gave a bad trace) stay skipped — retrying those would re-roll the
    # model on the same task. The 3-consecutive-exceptions fail-fast
    # below still aborts a run on a dead lane, so retries cannot spin.
    done_idx = {a["task_index"] for a in attempts
                if not (a.get("skip_reason") or "").startswith("exception:")}
    n_retried = len(attempts) - len(done_idx)
    if n_retried:
        print(f"{sec}: retrying {n_retried} transport-failed task(s)")

    for idx, task in enumerate(tech["tasks"]):
        if idx in done_idx:
            continue
        print(f"{sec}: task {idx} {str(task)[:60]}", flush=True)
        try:
            rec = attempt_task(tech, purpose, idx, task)
        except Exception as exc:  # transport-level failure: record, continue
            rec = {"task_index": idx, "input": tech["input"](task),
                   "expected": tech["ground_truth"](task),
                   "verified": False,
                   "skip_reason": f"exception: {type(exc).__name__}: {str(exc)[:150]}"}
        # Keep one record per task_index: drop the prior transport-failed
        # record for a retried task before appending the new attempt.
        attempts = [a for a in attempts if a["task_index"] != idx]
        attempts.append(rec)
        with open(att_path, "w") as fh:
            json.dump({"attempts": attempts}, fh, indent=1)
        print(f"{sec}: task {idx} verified={rec['verified']} "
              f"{rec.get('skip_reason', '')[:80]}", flush=True)
        # Fail fast if the substrate is unusable (3 consecutive exceptions):
        # better to retry later on a free lane than burn 16 doomed calls.
        recent = attempts[-3:]
        if (len(recent) == 3 and all(
                (r.get("skip_reason") or "").startswith("exception:")
                for r in recent)):
            print(f"{sec}: 3 consecutive transport failures; aborting "
                  f"(lane not usable right now)", flush=True)
            break
        sel, _k = select_demos(attempts)
        if sel is not None and len(sel) >= 12:
            print(f"{sec}: 12 verified demos selected; stopping early")
            break

    sel, k = select_demos(attempts)
    n_verified = sum(1 for a in attempts if a.get("verified"))
    print(f"{sec}: {n_verified} verified / {len(attempts)} attempted; "
          f"largest consistent work-line group k={k}", flush=True)
    C.check(sec, "twelve_verified_selected", sel is not None and len(sel) == 12,
            f"got {len(sel) if sel else 0}")
    with open(sel_path, "w") as fh:
        json.dump({"technique": technique, "work_lines": k,
                   "demonstrations": sel}, fh, indent=1)
    print(f"{sec}: wrote {sel_path}")
    print("BORROW_DONE")


if __name__ == "__main__":
    main()
