#!/bin/bash
# UNIFIED-MEMORY-1 gate_run.sh — reproduces the mission's full evidence
# battery in fresh sequential processes. Felix's gate is one command.
#
# What it proves (v10-convergence Phase 2):
#   b1 (um1_census): zero loop-owned direct writers bypass the unified
#       facade for the four record types (experience/evidence/hypothesis/
#       experiment). Only the facade calls the EpistemicStore's raw write
#       methods in production code.
#   b2 (um1_delta_mapping): the three persisted delta schemas map to the
#       one canonical schema (M2's DeltaRecord) — M1 dict via
#       _adapt_m1_to_m2, charter technique_t via charter_to_delta_record.
#   b3 (um1_continuity): cross-loop continuity in a live run — a fact
#       written by one loop through the unified interface is provably
#       readable by another, with provenance intact, persisted across
#       fresh store connections.
#   b4 (um1_helpers): the extracted query helpers are honestly classified
#       (ADOPTED where called, DIAGNOSTIC where not) — not decoration.
#
# Batteries run SEQUENTIALLY in fresh processes (never in parallel):
# on the 2-core host concurrent proofs contend and produce spurious
# failures that look like regressions.
set -u
set -o pipefail
D="$(cd "$(dirname "$0")" && pwd)"
PASS=0; FAIL=0

run_battery() { # $1=name $2=script
  echo "=== battery: $1 ==="
  if timeout 120 python3 "$D/$2" 2>&1 | tail -8; then
    echo "PASS: $1"
    PASS=$((PASS+1))
  else
    echo "FAIL: $1"
    FAIL=$((FAIL+1))
  fi
}

run_battery "b1_bypass_census" "um1_census.py"
run_battery "b2_delta_mapping" "um1_delta_mapping.py"
run_battery "b3_cross_loop_continuity" "um1_continuity.py"
run_battery "b4_helper_classification" "um1_helpers.py"
run_battery "b5_loop_visibility" "um1_visibility.py"

echo ""
echo "=== um1 gate: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -eq 0 ]; then
  echo "GATE UNIFIED-MEMORY-1: ALL BATTERIES PASS"
else
  echo "GATE UNIFIED-MEMORY-1: FAIL"
fi
exit $FAIL
