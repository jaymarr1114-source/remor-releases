#!/bin/bash
# CUR-P3A gate: Scientific Inquiry loop controller + James-authorized
# integration (vocabulary admission + loop registry + generalized
# terminal persistence).
# Runs the mission's FULL evidence battery in fresh sequential
# processes: the 61 stage checks, then the integration/end-to-end
# proofs. Batteries run SEQUENTIALLY (never in parallel) on the
# 2-core host. Exit 0 only if every check in both batteries passes.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1
rm -rf proofs/cur_p3a/runs proofs/cur_p3a/runs_int
echo "=== CUR-P3A stage battery (61 checks) ==="
python3 proofs/cur_p3a/cur_p3a_proof.py || exit 1
echo "=== CUR-P3A-INT integration battery ==="
python3 proofs/cur_p3a/cur_p3a_int_proof.py || exit 1
echo "=== CUR-P3A gate: ALL BATTERIES GREEN ==="
