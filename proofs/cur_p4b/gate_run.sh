#!/bin/bash
# CUR-P4B gate: disabled-domain + kill paths (C-9, chunk 4b).
# Runs the mission's FULL evidence battery in fresh sequential processes.
# Batteries run SEQUENTIALLY (never in parallel) on the 2-core host.
# Exit 0 only if every check passes.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1

echo "=== CUR-P4B gate_run.sh ==="
echo "--- 0. worktree pin (fail-closed, merge-base ancestry) ---"
PIN_BASE="592b7e5f3db62e8dafd3667b9e545f97f7396d2a"
if git merge-base --is-ancestor "$PIN_BASE" HEAD; then
  echo "PASS: worktree based at 592b7e5 (HEAD $(git rev-parse --short HEAD))"
else
  echo "FAIL: worktree not based at 592b7e5 -- refusing to run"
  exit 1
fi

rm -rf proofs/cur_p4b/runs
echo "=== CUR-P4B proof battery ==="
python3 proofs/cur_p4b/cur_p4b_proof.py || exit 1
echo "=== CUR-P4B gate: ALL BATTERIES GREEN ==="
