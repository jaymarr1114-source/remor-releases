#!/bin/bash
# RECURRENCE-1 gate battery: every probe in a FRESH sequential process.
# Exit 0 only when everything is green. Re-run by Felix = the gate.
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures that look
# like regressions.
#
# NOTE: this battery EXPECTS the route-backed coming-soon test to be
# stale on exactly the recurring_disabled entry (P9 proves it). The
# entry is retired at gate/landing time in main chat — this mission
# never edits runtime/services/availability.py or
# tests/contracts/test_coming_soon.py.
set -u
WT="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$WT/../.." && pwd)"
PROOF="$WT"
export PYTHONPATH="$ROOT/pylib"
SCRATCH="$PROOF/scratch"
rm -rf "$SCRATCH"
mkdir -p "$SCRATCH"
export RECURRENCE1_SCRATCH="$SCRATCH"

fail=0
step() {
  name="$1"; shift
  echo "=== $name $* ==="
  ( cd "$PROOF" && python3 "$name" "$@" ) || { echo "FAIL: $name $*"; fail=1; }
}

step p1_default_on.py
step p2_kill_switch.py
step p3_real_firing.py
step p4_listing.py
step p5_causal_cancel.py
step p6_restart.py
step p7_bad_specs.py
step p8_route_table.py
step p9_retirement_signal.py

echo "=== canonical suites: recurrence service + tasks_api ==="
( cd "$ROOT" && python3 tests/track2_new/test_recurrence.py ) \
  || { echo "FAIL: tests/track2_new/test_recurrence.py"; fail=1; }
( cd "$ROOT" && python3 -m pytest tests/backend/test_tasks_api.py \
    tests/contracts/test_tasks_api.py -q ) \
  || { echo "FAIL: tasks_api suites"; fail=1; }

if [ "$fail" -eq 0 ]; then
  echo "RECURRENCE-1 GATE: ALL GREEN"
else
  echo "RECURRENCE-1 GATE: RED"
  exit 1
fi
