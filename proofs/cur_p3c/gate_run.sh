#!/bin/bash
# CUR-P3C gate: Discovery/Novelty loop controller (stage build).
# Runs the mission's FULL evidence battery in fresh sequential
# processes. Batteries run SEQUENTIALLY (never in parallel) on the
# 2-core host. Exit 0 only if every check passes.
#
# The fail-closed pin check uses merge-base ancestry (never
# HEAD-equality): the worktree must be based at the pinned commit,
# and the mission's own commits on top are fine (the CUR-P3B lesson).
# No hardcoded home paths: everything derives from this script's
# location (James 2026-10-03).
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"

echo "=== CUR-P3C gate_run.sh ==="
echo "--- 0. worktree pin (fail-closed, merge-base ancestry) ---"
PIN_BASE="25cc5f53957ce1eca1744cbbb52d6ea41460e239"
if (cd "$WORKTREE" && git merge-base --is-ancestor "$PIN_BASE" HEAD); then
  echo "PASS: worktree based at 25cc5f5 (HEAD $(cd "$WORKTREE" && git rev-parse --short HEAD))"
else
  echo "FAIL: worktree not based at 25cc5f5 -- refusing to run"
  exit 1
fi

echo "=== CUR-P3C stage battery ==="
rm -rf "$ROOT/runs"
cd "$WORKTREE" || exit 1
python3 proofs/cur_p3c/cur_p3c_proof.py || exit 1
echo "=== CUR-P3C gate: ALL BATTERIES GREEN ==="
