#!/usr/bin/env bash
# CUR-P6B gate battery: Level 2 drill (warning, suspension, rollback).
# One invocation reproduces the mission's entire evidence battery in fresh
# sequential processes. Exit 0 iff everything passes.
#
# No hardcoded home paths: everything derives from this file's location.
set -euo pipefail

PROOF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="$(cd "${PROOF_DIR}/../.." && pwd)"

# --- fail-closed pin check: merge-base ancestry, never HEAD-equality ---
PIN="84b2a80"
cd "${WORKTREE}"
if ! git merge-base --is-ancestor "${PIN}" HEAD; then
    echo "GATE FAIL: pin ${PIN} is not an ancestor of worktree HEAD" >&2
    exit 1
fi
echo "pin check OK: ${PIN} is an ancestor of $(git rev-parse --short HEAD)"

# --- ownership guard: only owned paths may be new/modified ---
# Any tracked-file modification outside owned paths fails the gate.
MODIFIED="$(git status --porcelain | awk '$1=="M" || $1==" M" {print $2}')"
for f in ${MODIFIED}; do
    case "${f}" in
        runtime/curiosity/hardening/*|proofs/cur_p6b/*) ;;
        *) echo "GATE FAIL: modified file outside owned paths: ${f}" >&2
           exit 1 ;;
    esac
done
UNTRACKED="$(git status --porcelain | awk '$1=="??" {print $2}')"
for f in ${UNTRACKED}; do
    case "${f}" in
        runtime/curiosity/hardening/*|proofs/cur_p6b/*) ;;
        *) echo "GATE FAIL: untracked file outside owned paths: ${f}" >&2
           exit 1 ;;
    esac
done
echo "ownership check OK: only owned paths touched"

# --- the evidence battery: fresh sequential processes, niced ---
echo "--- battery 1/1: cur_p6b_proof.py (fresh process) ---"
nice -n 10 python3 "${PROOF_DIR}/cur_p6b_proof.py"

echo "GATE OK: CUR-P6B evidence battery green"
