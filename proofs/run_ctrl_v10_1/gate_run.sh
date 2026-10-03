#!/bin/bash
# gate_run.sh -- RUN-CTRL-V10-1 gate battery.
#
# Runs the mission's full evidence battery in fresh sequential processes
# (never parallel: this is a 2-core host and concurrent proof runs
# contend and produce spurious failures). Each driver's pass/fail is
# visible in stdout. Exit 0 only if every driver passes.
#
# Usage: ./gate_run.sh   (run from this directory)

set -u
cd "$(dirname "$0")"
# Derive the tree under test from this script's location (canonical/proofs/run_ctrl_v10_1/),
# never a hardcoded worktree path — the battery must run against whatever tree it's in.
SCRIPT_DIR="$(pwd)"
TREE="$(cd "$SCRIPT_DIR/../.." && pwd)"
WT="${REMOR_TEST_TREE:-$TREE}"

pass=0
fail=0

run_driver() {
    local name="$1"; shift
    echo "=== DRIVER: $name ==="
    if "$@" 2>&1; then
        echo "--- $name: PASS ---"
        pass=$((pass + 1))
    else
        echo "--- $name: FAIL (exit $?) ---"
        fail=$((fail + 1))
    fi
    echo
}

run_driver "causal_01_double_drive (one cadence, unified consumption)" \
    python3 -u causal_01_double_drive.py

run_driver "causal_02_scheduler (scheduler subordination)" \
    python3 -u causal_02_scheduler.py

run_driver "causal_03_bare_caller (authorized caller for repair)" \
    python3 -u causal_03_bare_caller.py

run_driver "functional_light (tick, pause/resume/stop, kill -9 resume, budget)" \
    python3 -u functional_light.py

run_driver "regression: tests/backend/test_run_control.py" \
    python3 -m pytest "$WT/tests/backend/test_run_control.py" \
    -q -p no:cacheprovider

echo "==============================="
echo "GATE RESULT: $pass passed, $fail failed"
if [ "$fail" -ne 0 ]; then
    echo "GATE: FAIL"
    exit 1
fi
echo "GATE: PASS"
