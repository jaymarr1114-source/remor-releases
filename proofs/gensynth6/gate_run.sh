#!/bin/bash
# GEN-SYNTH-6 gate battery: reproduces the mission's full evidence in
# fresh sequential processes. Run from the mission worktree root.
# Exits 0 only if every driver exits 0. Abort on first failure.
set -u
WT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$WT" || exit 1

pass=0; fail=0
run_driver() {
  local name="$1"; local script="$2"
  echo "=== DRIVER $name ==="
  if python3 "$script"; then
    echo "=== $name exit=0 ==="
    pass=$((pass+1))
  else
    echo "=== $name FAILED (exit=$?) ==="
    fail=$((fail+1))
    return 1
  fi
}

# New GEN-SYNTH-6 crossings (mandates 1-2, 4).
run_driver "gs6_filter_nested"    "proofs/gensynth6/gs6_filter_nested.py"    || exit 1
run_driver "gs6_threelevel_runcycle" "proofs/gensynth6/gs6_threelevel_runcycle.py" || exit 1

# GEN-SYNTH-5 mechanism preservation (mandate 3).
run_driver "gs5_three_level"  "proofs/gensynth6/regress/gs5_three_level.py"  || exit 1
run_driver "gs5_mixed"        "proofs/gensynth6/regress/gs5_mixed.py"        || exit 1
run_driver "gs5_runcycle"     "proofs/gensynth6/regress/gs5_runcycle.py"     || exit 1
run_driver "gs5_repair_focused" "proofs/gensynth6/regress/gs5_repair_focused.py" || exit 1

# Prior-battery regressions, original strong drivers (mandate 3).
run_driver "gs4_battery"   "proofs/gensynth6/regress/gs4_battery.py"   || exit 1
run_driver "gs3_battery"   "proofs/gensynth6/regress/gs3_battery.py"   || exit 1
run_driver "gs2_battery"   "proofs/gensynth6/regress/gs2_battery.py"   || exit 1
run_driver "gs1_battery"   "proofs/gensynth6/regress/gs1_battery.py"   || exit 1
run_driver "gxdom_battery" "proofs/gensynth6/regress/gxdom_battery.py" || exit 1
run_driver "genv_battery"  "proofs/gensynth6/regress/genv_battery.py"  || exit 1
run_driver "gctrl_battery" "proofs/gensynth6/regress/gctrl_battery.py" || exit 1

echo ""
echo "=== GEN-SYNTH-6 GATE: $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
