#!/usr/bin/env bash
# GRANT-MIGRATE-1 gate: reproduces the mission's entire evidence battery in
# fresh sequential processes. Run from anywhere; the worktree is derived
# from this script's location. NEVER run proof batteries in parallel on the
# 2-core host.
set -u
GATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$GATE_DIR/../.." && pwd)"
PROOFS="$TREE/proofs"
export PYTHONPATH="$TREE:$TREE/pylib"

pass=0; fail=0
run_battery() {
  local name="$1"; shift
  echo "### $name"
  if timeout 900 python3 "$@" > /tmp/gm_gate_last.log 2>&1; then
    tail -2 /tmp/gm_gate_last.log | head -1
    pass=$((pass+1))
  else
    echo "BATTERY FAILED: $name"
    tail -15 /tmp/gm_gate_last.log
    fail=$((fail+1))
  fi
}

run_battery b1_one_contract        "$GATE_DIR/b1_one_contract.py"
run_battery b2_issue_consume       "$GATE_DIR/b2_issue_consume_exhaust.py"
run_battery b4_adversarial         "$GATE_DIR/b4_adversarial.py"
run_battery b5_cross_namespace     "$GATE_DIR/b5_cross_namespace.py"
run_battery b6_legacy_db_migration "$GATE_DIR/b6_legacy_db_migration.py"
run_battery cur_p1d_attribution    "$PROOFS/cur_p1d_attribution_proof_2026-09-29.py"
run_battery brainscaffold1         "$PROOFS/brainscaffold1_proof.py"
run_battery cur_p1c_frm            "$PROOFS/cur_p1c_frm_proof.py"

echo "=================================================="
echo "GATE RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
