#!/usr/bin/env python3
"""DISTILL-1: blinded grading harness.

Step 1: eval_grade.py --make base.json student.json --out sheet.json
        Shuffles base/student responses per turn; writes a grading sheet
        with the source hidden in sheet.key.json (kept separate).
Step 2: human grades sheet.json against rubric.md (adds "score" 0-2 per
        response), saves as sheet_scored.json.
Step 3: eval_grade.py --tally sheet_scored.json --key sheet.key.json
        Unblinds, reports per-system means, clarification-class zeros,
        and the ticket pass/fail.

The rubric (distill1/rubric.md) was pre-registered before any output.
"""
import argparse
import json
import random
import sys


def make_sheet(base_path, student_path, out_path):
    with open(base_path) as fh:
        base = {r["turn_id"]: r for r in json.load(fh)}
    with open(student_path) as fh:
        student = {r["turn_id"]: r for r in json.load(fh)}
    assert set(base) == set(student), "turn id mismatch"
    rng = random.Random(20260930)  # fixed seed: shuffle is reproducible
    sheet, key = [], {}
    for tid in sorted(base):
        order = ["base", "student"]
        rng.shuffle(order)
        key[tid] = order
        sheet.append({
            "turn_id": tid,
            "turn_class": base[tid]["turn_class"],
            "turn_text": base[tid]["turn_text"],
            "responses": [
                {"label": "A", "text": {"base": base[tid]["response"],
                                        "student": student[tid]["response"]}[order[0]],
                 "score": None},
                {"label": "B", "text": {"base": base[tid]["response"],
                                        "student": student[tid]["response"]}[order[1]],
                 "score": None},
            ],
        })
    with open(out_path, "w") as fh:
        json.dump(sheet, fh, indent=1)
    with open(out_path + ".key.json", "w") as fh:
        json.dump(key, fh, indent=1)
    print(f"sheet: {out_path} ({len(sheet)} turns); key: {out_path}.key.json")
    print("GRADE each response 0-2 per rubric.md, save as <sheet>_scored.json")


def tally(sheet_path, key_path):
    with open(sheet_path) as fh:
        sheet = json.load(fh)
    with open(key_path) as fh:
        key = json.load(fh)
    agg = {"base": [], "student": []}
    clar_zero = {"base": 0, "student": 0}
    for item in sheet:
        tid = item["turn_id"]
        order = key[tid]
        for resp in item["responses"]:
            s = resp["score"]
            assert s in (0, 1, 2), f"{tid}/{resp['label']}: bad score {s}"
            src = order[0] if resp["label"] == "A" else order[1]
            agg[src].append(s)
            if item["turn_class"] == "clarification" and s == 0:
                clar_zero[src] += 1
    out = {}
    for src in ("base", "student"):
        scores = agg[src]
        mean = sum(scores) / len(scores)
        out[src] = {"n": len(scores), "mean": round(mean, 3),
                    "zeros": sum(1 for s in scores if s == 0),
                    "clarification_zeros": clar_zero[src]}
    base_m, stud_m = out["base"]["mean"], out["student"]["mean"]
    passes = (stud_m >= 1.5 and out["student"]["clarification_zeros"] == 0
              and stud_m > base_m)
    out["pass_bar"] = {
        "student_mean_ge_1_5": stud_m >= 1.5,
        "student_clarification_zeros_eq_0":
            out["student"]["clarification_zeros"] == 0,
        "student_mean_gt_base_mean": stud_m > base_m,
        "PASS": passes,
    }
    print(json.dumps(out, indent=1))
    return 0 if passes else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", nargs=2, metavar=("BASE", "STUDENT"))
    ap.add_argument("--tally", metavar="SHEET_SCORED")
    ap.add_argument("--key", metavar="KEY")
    ap.add_argument("--out", default="grading_sheet.json")
    args = ap.parse_args()
    if args.make:
        make_sheet(args.make[0], args.make[1], args.out)
    elif args.tally:
        assert args.key, "--key required with --tally"
        sys.exit(tally(args.tally, args.key))
    else:
        ap.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
