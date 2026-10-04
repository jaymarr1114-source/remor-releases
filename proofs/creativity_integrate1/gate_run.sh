#!/usr/bin/env bash
# CREATIVITY-INTEGRATE-1 gate battery: the end-to-end integration proof.
#
# One command, exit 0 on green. Every section runs in a FRESH sequential
# python3 process (no shared state); then the FULL track battery re-runs
# at the same HEAD. The track is one integrated system only if all of
# them are green together.
#
#   ./proofs/creativity_integrate1/gate_run.sh
#
# Sections:
#   i0  compile check (all eight track modules + package + proof scripts)
#   i1  E1 full path: commission -> run controller (real grant) ->
#       executive run() -> generation from the verified side ->
#       critique -> admission with complete AdmissionRecord
#   i2  E2 named gap: seeded unverified demand routes to the REAL gap
#       registry, visible and named; the task proceeds without it
#   i3  E3 kill mid-generation: cold stop at the next step boundary,
#       preservation complete
#   i4  E4 budget ceiling mid-run: CeilingStop, frozen totals,
#       zero further spend
#   i5  S shakedown: cross-module interface coherence through the
#       executive's real wiring
#   i6  full track battery at one HEAD: slice1 (repaired), ledger1,
#       critique1, release1, budget1, exec1, runctrl1
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT_ROOT="${WT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export WT_ROOT
cd "$SCRIPT_DIR"

PASS=0; FAIL=0
section() { # $1=name $2=exit code
  if [ "$2" -eq 0 ]; then echo "[PASS] $1"; PASS=$((PASS+1));
  else echo "[FAIL] $1"; FAIL=$((FAIL+1)); fi
}

echo "== i0: compile check =="
python3 -m py_compile \
  "$WT_ROOT/runtime/creativity/__init__.py" \
  "$WT_ROOT/runtime/creativity/stages.py" \
  "$WT_ROOT/runtime/creativity/intent.py" \
  "$WT_ROOT/runtime/creativity/ledger.py" \
  "$WT_ROOT/runtime/creativity/critique.py" \
  "$WT_ROOT/runtime/creativity/release.py" \
  "$WT_ROOT/runtime/creativity/budget.py" \
  "$WT_ROOT/runtime/creativity/executive.py" \
  "$WT_ROOT/runtime/creativity/run_controller.py" \
  fixtures.py e1_full_path.py e2_named_gap.py e3_kill.py \
  e4_ceiling.py s_shakedown.py \
  || { echo "[FAIL] compile"; exit 1; }
echo "[PASS] compile"

echo "== i1: E1 full path (fresh process) =="
python3 e1_full_path.py; section "e1_full_path" $?

echo "== i2: E2 named gap (fresh process) =="
python3 e2_named_gap.py; section "e2_named_gap" $?

echo "== i3: E3 kill mid-generation (fresh process) =="
python3 e3_kill.py; section "e3_kill" $?

echo "== i4: E4 budget ceiling (fresh process) =="
python3 e4_ceiling.py; section "e4_ceiling" $?

echo "== i5: S shakedown (fresh process) =="
python3 s_shakedown.py; section "s_shakedown" $?

echo "== i6: full track battery at one HEAD ($WT_ROOT) =="
for b in creativity_slice1 creativity_ledger1 creativity_critique1 \
         creativity_release1 creativity_budget1 creativity_exec1 \
         creativity_runctrl1; do
  echo "--- track battery: $b ---"
  ( cd "$WT_ROOT/proofs/$b" && ./gate_run.sh )
  section "track_battery_$b" $?
done

echo "==="
echo "integrate1 gate: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
