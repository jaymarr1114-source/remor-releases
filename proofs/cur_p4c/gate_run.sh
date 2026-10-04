#!/bin/bash
# CUR-P4C gate: C-9 evidence-return leg (Primary-requested finding ->
# present_for_acceptance -> REAL RelevanceGate.decide -> recorded
# verdict; C-2.1/C-2.3 adversarial). Runs the mission's FULL evidence
# battery in fresh sequential processes. Batteries run SEQUENTIALLY
# (never in parallel) on the 2-core host. Exit 0 only if every check
# passes.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WT_ROOT="$(cd "$ROOT/../.." && pwd)"
echo "=== CUR-P4C gate_run.sh ==="
echo "--- 0. worktree pin (fail-closed, merge-base ancestry) ---"
PIN_BASE="68d99f1b335753071fd9ec2e4e9660faac14cabb"
if (cd "$WT_ROOT" && git merge-base --is-ancestor "$PIN_BASE" HEAD); then
  echo "PASS: worktree based at 68d99f1 (HEAD $(cd "$WT_ROOT" && git rev-parse --short HEAD))"
else
  echo "FAIL: worktree not based at 68d99f1 -- refusing to run"
  exit 1
fi
cd "$WT_ROOT" || exit 1
rm -rf proofs/cur_p4c/runs
echo "=== CUR-P4C battery ==="
python3 proofs/cur_p4c/cur_p4c_proof.py || exit 1
echo "=== CUR-P4C gate: ALL BATTERIES GREEN ==="
