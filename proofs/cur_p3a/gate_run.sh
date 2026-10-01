#!/bin/bash
# CUR-P3A gate: Scientific Inquiry loop controller.
# Runs the mission's full evidence battery in a fresh process.
# Batteries run SEQUENTIALLY (never in parallel) on the 2-core host.
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1
rm -rf proofs/cur_p3a/runs
exec python3 proofs/cur_p3a/cur_p3a_proof.py
