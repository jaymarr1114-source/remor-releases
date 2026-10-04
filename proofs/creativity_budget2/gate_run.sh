#!/bin/bash
# CREATIVITY-BUDGET-2 gate battery: James's locked budget policy as real fields.
# Each test runs in a FRESH process, sequentially. One command, exit 0 iff all pass.
# No hardcoded paths (James 2026-10-03): WT_ROOT is derived, never written.
set -u
PROOF_DIR="$(cd "$(dirname "$0")" && pwd)"
WT_ROOT="$(cd "$PROOF_DIR/../.." && pwd)"
# Both import roots: WT_ROOT for `runtime.*`, WT_ROOT/pylib for the
# `swarm_engine -> ../runtime` symlink (runtime/curiosity/frm imports it).
export PYTHONPATH="$WT_ROOT:$WT_ROOT/pylib"

PASS=0
FAIL=0
FAILED_TESTS=""

run_test() {
  local t="$1"
  if python3 "$PROOF_DIR/proof_budget2.py" "$t"; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    FAILED_TESTS="$FAILED_TESTS $t"
  fi
}

run_test construction_refuses_absurd
run_test construction_accepts_sane_and_shows_derivation
run_test normal_slot_one_then_refused
run_test burst_granted_with_capacity_third_always_refused
run_test burst_refused_under_load_and_default_probe_real
run_test spend_accounting_reconciles_incl_error
run_test no_burn_down
run_test aggregate_ceiling_refuses
run_test idle_domain_zero
run_test stale_slot_and_double_release

echo "==================================="
echo "CREATIVITY-BUDGET-2 battery: PASS=$PASS FAIL=$FAIL"
if [ -n "$FAILED_TESTS" ]; then
  echo "failed:$FAILED_TESTS"
fi
if [ "$FAIL" -ne 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
echo "RESULT: PASS"
exit 0
