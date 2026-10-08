#!/bin/bash
# GEN-SYNTH-11 gate: repair verification for the two GEN-SYNTH-7/8/9/10 regressions.
# Runs the full 13-driver regression battery sequentially, uncontended.
# Exit 0 only if every driver exits 0.
#
# Defect 1 (driver 2, gs6_threelevel_runcycle): OOM in B2 run_cycle
#   RSS 10MB -> 5.7GB in ~4min, SIGKILL. Fixed by restoring the
#   _nest_may_complete gate for <=3-param goals (GS8 fail-open was
#   too aggressive, removed pruning, caused unbounded memory growth).
# Defect 2 (driver 8, gs3_battery): filter-fusion regression 14/25.
#   Fixed by allowing ANY-kind Q's in _gs8_pair_lam_kind_ok (blocking
#   ANY was unsound; the viable plan needs coalesce:ANY).
set -u
WT="/home/hatch/workspace/worktrees/warm-generalization"
STAGE="$WT/proofs/gensynth11"
LOGDIR="$WT/proofs/gensynth11/logs"
mkdir -p "$LOGDIR"

# Verify we're on the right tree
HEAD=$(git -C "$WT" rev-parse HEAD)
if [ "$HEAD" != "60caca4698f59fa5d8cf72b8057833d9d0a01cd2" ]; then
  echo "FATAL: worktree HEAD is $HEAD, expected 60caca4 (canonical base; mission branch gen-synth-11-canonical-work)"
  exit 1
fi
echo "Tree verified: $HEAD"

# Verify no stale worktree references in drivers
if grep -l "gate-gensynth7-redo\|gensynth7-mission" "$STAGE"/*.py 2>/dev/null; then
  echo "FATAL: stale worktree reference found in drivers"
  exit 1
fi
echo "Driver targeting verified: all 13 drivers point at $WT"

pass=0; fail=0; failed_drivers=""
run_driver() {
  local name="$1"; local script="$2"
  local log="$LOGDIR/${name}.log"
  echo "=== DRIVER $name ==="
  load=$(cut -d' ' -f1 /proc/loadavg)
  echo "  load=$load"
  if python3 "$script" > "$log" 2>&1; then
    echo "  $name: PASS"
    pass=$((pass+1))
  else
    echo "  $name: FAIL (see $log)"
    fail=$((fail+1))
    failed_drivers="$failed_drivers $name"
  fi
}

# 13 drivers in order
run_driver "gs6_filter_nested" "$STAGE/gs6_filter_nested.py"
run_driver "gs6_threelevel_runcycle" "$STAGE/gs6_threelevel_runcycle.py"
run_driver "gs5_three_level" "$STAGE/gs5_three_level.py"
run_driver "gs5_mixed" "$STAGE/gs5_mixed.py"
run_driver "gs5_runcycle" "$STAGE/gs5_runcycle.py"
run_driver "gs5_repair_focused" "$STAGE/gs5_repair_focused.py"
run_driver "gs4_battery" "$STAGE/gs4_battery.py"
run_driver "gs3_battery" "$STAGE/gs3_battery.py"
run_driver "gs2_battery" "$STAGE/gs2_battery.py"
run_driver "gs1_battery" "$STAGE/gs1_battery.py"
run_driver "gxdom_battery" "$STAGE/gxdom_battery.py"
run_driver "genv_battery" "$STAGE/genv_battery.py"
run_driver "gctrl_battery" "$STAGE/gctrl_battery.py"

echo ""
echo "========================================"
echo "GEN-SYNTH-11 gate: $pass/13 PASS, $fail/13 FAIL"
if [ "$fail" -gt 0 ]; then
  echo "FAILED drivers:$failed_drivers"
  exit 1
fi
echo "ALL 13 DRIVERS PASS"
exit 0
