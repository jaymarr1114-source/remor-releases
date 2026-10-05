#!/bin/bash
# GATE CONTINUOUS-OPS-V10-1: loops running continuously under the Run
# Controller with FRM governance (v10-convergence Phase 3, final phase).
#
# Runs the full evidence battery in fresh sequential processes. Each
# test is an independent python3 invocation (fresh interpreter, fresh
# imports, no shared state) so a pass here means the mechanisms hold
# in isolation, not just in one lucky process.
#
# T1 sustained:        Controller-owned run, unified-memory observation log
# T2 kill_resume:      genuine SIGKILL death, resume from checkpoint
# T3 frm_enforcement:  all 5 enforcement states honored (zero/restrict/run)
# T4 epoch_lending:    epoch-bounded lending, non-preemption
# T5 adversarial:      budget exhaustion refuses honestly with reasons
# T6 yield_attribution: accepted -> yield; unaccepted -> cost, zero yield
# T7 frozen_consumers: frozen inlet/distill consumed, never rebuilt
#
# Exit 0 only if every battery passes. Heavy: run SEQUENTIALLY on the
# 2-core host -- never overlap with another track's battery.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0; FAIL=0
run_battery() {
  local name="$1"
  echo "=== $name ==="
  if timeout 300 python3 "$HERE/$name.py"; then
    PASS=$((PASS+1)); echo "--- $name: PASS ---"
  else
    FAIL=$((FAIL+1)); echo "--- $name: FAIL (exit $?) ---"
  fi
  echo
}
run_battery t1_sustained
run_battery t2_kill_resume
run_battery t3_frm_enforcement
run_battery t4_epoch_lending
run_battery t5_adversarial_budget
run_battery t6_yield_attribution
run_battery t7_frozen_consumers
echo "=========================================="
echo "GATE CONTINUOUS-OPS-V10-1: $PASS/7 batteries pass, $FAIL failed"
if [ "$FAIL" -eq 0 ]; then
  echo "GATE CONTINUOUS-OPS-V10-1: ALL BATTERIES PASS"
  exit 0
else
  echo "GATE CONTINUOUS-OPS-V10-1: FAIL"
  exit 1
fi
