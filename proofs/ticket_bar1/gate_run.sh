#!/bin/bash
# TICKET-BAR-1 gate_run.sh — reproduces the mission's full evidence battery
# in fresh sequential processes. Felix's gate is one command.
#
# What it proves:
#   1. eval_grade.py --tally derives its pass verdict from the sealed
#      ticket's exit_criteria.held_out (no hard-coded literals).
#   2. Re-sealing the ticket with a stricter min_mean flips a known-passing
#      result to FAIL (causal, both directions), via the real CLI path.
#   3. The pre-fix code ignored the ticket (bug demonstrated, then fixed).
#   4. A ticket missing its bar is refused honestly (no silent default).
#   5. The DISTILL-1 grade battery stays green.
#   6. Known residual (pinned): hand-edited result.status is not detected —
#      tickets carry no cryptographic seal (exact next boundary).
#
# Exit 0 if all checks pass, 1 otherwise.
set -u
TREE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$TREE" || exit 1
PASS=0; FAIL=0
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

check() { # $1=name $2=condition(0=pass)
  if [ "$2" -eq 0 ]; then echo "PASS: $1"; PASS=$((PASS+1));
  else echo "FAIL: $1"; FAIL=$((FAIL+1)); fi
}

export PYTHONPATH="$TREE/pylib:$TREE"

SHEET="distill1/grading_sheet_scored.json"
KEY="distill1/grading_sheet.json.key.json"
TICKET="distill1/ticket_distill1.json"

echo "=== 1. structural: no hard-coded bar literals in the tally path ==="
# The audit's exact finding: literals `1.5` / `== 0` in eval_grade tally.
if grep -n "stud_m >= 1\.5\|clarification_zeros.*== 0" distill1/eval_grade.py | grep -v "^.*#"; then
  echo "hard-coded literals still present"; check "no literals in tally" 1
else
  check "no literals in tally" 0
fi
python3 -c "from distill1.ticket import held_out_bar, grade_held_out; print('predicate exposed by ticket.py')"
check "ticket.py exposes held_out_bar + grade_held_out" $?

echo "=== 2. baseline: tally reads the sealed ticket's bar ==="
OUT="$SCRATCH/baseline.json"
python3 distill1/eval_grade.py --tally "$SHEET" --key "$KEY" > "$OUT" 2>"$SCRATCH/base.err"
CODE=$?
check "baseline tally exit 0 (PASS)" $([ $CODE -eq 0 ] && echo 0 || echo 1)
python3 - "$OUT" <<'PYEOF'
import json, sys
o = json.load(open(sys.argv[1]))
pb = o["pass_bar"]
assert pb["PASS"] is True, pb
assert pb["ticket"] == "DISTILL-1", pb
assert pb["bar_source"].endswith("distill1/ticket_distill1.json"), pb
assert pb["student_mean_ge_1.5"] is True, pb  # bar VALUE came from the ticket
print("baseline: PASS, bar_source =", pb["bar_source"])
PYEOF
check "baseline verdict PASS from ticket bar (min_mean=1.5)" $?

echo "=== 3. causal flip: re-seal stricter, verdict follows the ticket ==="
python3 - "$SCRATCH" <<'PYEOF'
import json, sys
sys.path.insert(0, "distill1")
import ticket as T
scratch = sys.argv[1]
t = json.load(open("distill1/ticket_distill1.json"))
t["exit_criteria"]["held_out"]["min_mean"] = 1.9  # stricter than student mean 1.833
sealed = T.seal(t, t["result"]["measurements"], t["result"]["evidence"])
json.dump(sealed, open(f"{scratch}/ticket_strict.json", "w"), indent=1)
assert sealed["result"]["status"] == "FAIL", sealed["result"]
assert sealed["result"]["checks"]["held_out_mean"] is False
print("re-sealed: status FAIL, held_out_mean check False (seal recomputed, not hand-set)")
PYEOF
check "re-seal with min_mean=1.9 recomputes seal status to FAIL" $?
STRICT="$SCRATCH/ticket_strict.json"
python3 distill1/eval_grade.py --tally "$SHEET" --key "$KEY" --ticket "$STRICT" > "$SCRATCH/strict.json" 2>/dev/null
CODE=$?
check "strict-ticket tally exits 1 (FAIL)" $([ $CODE -eq 1 ] && echo 0 || echo 1)
python3 - "$SCRATCH/strict.json" <<'PYEOF'
import json, sys
o = json.load(open(sys.argv[1]))
pb = o["pass_bar"]
assert pb["PASS"] is False, pb
assert pb["student_mean_ge_1.9"] is False, pb  # the TICKET's bar, not 1.5
assert pb["bar_source"].endswith("ticket_strict.json"), pb
print("strict tally: FAIL via student_mean_ge_1.9=False")
PYEOF
check "strict tally verdict FAIL keyed on the ticket's 1.9 bar" $?
python3 distill1/eval_grade.py --tally "$SHEET" --key "$KEY" --ticket "$TICKET" > "$SCRATCH/restored.json" 2>/dev/null
CODE=$?
python3 - "$SCRATCH/restored.json" <<'PYEOF'
import json, sys
o = json.load(open(sys.argv[1]))
assert o["pass_bar"]["PASS"] is True
print("original ticket: PASS again (both directions)")
PYEOF
check "original ticket restores PASS (both directions)" $([ $CODE -eq 0 ] && echo 0 || echo 1)

echo "=== 4. pre-fix contrast: old code contradicted the sealed ticket ==="
mkdir -p "$SCRATCH/oldcode"
git show HEAD:distill1/eval_grade.py > "$SCRATCH/oldcode/eval_grade.py"
cp "$STRICT" "$SCRATCH/oldcode/ticket_distill1.json"  # old code only reads its default ticket
(cd "$SCRATCH/oldcode" && python3 eval_grade.py --tally "$TREE/$SHEET" --key "$TREE/$KEY" > out.json 2>/dev/null)
python3 - "$SCRATCH/oldcode/out.json" <<'PYEOF'
import json, sys
o = json.load(open(sys.argv[1]))
pb = o["pass_bar"]
assert pb["PASS"] is True, pb  # BUG: strict ticket says 1.9, old code used literal 1.5
assert "student_mean_ge_1_5" in pb, pb
print("old code vs strict ticket: PASS (literal 1.5 ignored the sealed 1.9) — the audited bug")
PYEOF
check "pre-fix code ignored the ticket (bug demonstrated)" $?

echo "=== 5. adversarial: missing bar is refused, never silently defaulted ==="
python3 - <<PYEOF
import json
t = json.load(open("$TICKET"))
del t["exit_criteria"]["held_out"]
json.dump(t, open("$SCRATCH/ticket_no_bar.json", "w"))
PYEOF
python3 distill1/eval_grade.py --tally "$SHEET" --key "$KEY" --ticket "$SCRATCH/ticket_no_bar.json" > /dev/null 2> "$SCRATCH/refused.err"
CODE=$?
check "bar-less ticket refused with exit 2" $([ $CODE -eq 2 ] && echo 0 || echo 1)
grep -q "TALLY REFUSED" "$SCRATCH/refused.err" && grep -q "exit_criteria.held_out missing: min_mean" "$SCRATCH/refused.err"
check "refusal names the missing bar (honest error)" $?
python3 - <<'PYEOF'  # corrupt JSON must not grade either
import subprocess
open("/tmp/tb1_gate_corrupt.json", "w").write("{not json")
r = subprocess.run(["python3", "distill1/eval_grade.py", "--tally",
                    "distill1/grading_sheet_scored.json", "--key",
                    "distill1/grading_sheet.json.key.json",
                    "--ticket", "/tmp/tb1_gate_corrupt.json"],
                   capture_output=True, text=True)
assert r.returncode != 0 and not r.stdout.strip(), (r.returncode, r.stdout[:80])
print("corrupt ticket: non-zero exit, no verdict printed")
PYEOF
check "corrupt ticket produces no verdict" $?

echo "=== 6. pinned residual: hand-edited result.status is undetected ==="
python3 - <<PYEOF
import json, subprocess
t = json.load(open("$TICKET"))
t["result"]["status"] = "PASS"  # hand-set, bypassing seal()
json.dump(t, open("$SCRATCH/ticket_tampered.json", "w"))
import sys; sys.path.insert(0, "distill1")
import ticket as T
assert T.validate(t) == [], "validate must stay silent (pins current behavior)"
r = subprocess.run(["python3", "distill1/eval_grade.py", "--tally",
                    "$SHEET", "--key", "$KEY",
                    "--ticket", "$SCRATCH/ticket_tampered.json"],
                   capture_output=True, text=True)
o = json.loads(r.stdout)
# tally never consults result.status: verdict still computed from criteria
assert o["pass_bar"]["PASS"] is True
print("RESIDUAL PINNED: hand-edited result.status undetected by validate(); tally ignores status and recomputes")
PYEOF
check "tamper residual pinned (no cryptographic seal — next boundary)" $?

echo "=== 7. DISTILL-1 grade battery still green ==="
bash proofs/distill1/gate_run.sh > "$SCRATCH/d1.log" 2>&1
check "proofs/distill1/gate_run.sh green" $?

echo "=== TICKET-BAR-1 GATE: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
