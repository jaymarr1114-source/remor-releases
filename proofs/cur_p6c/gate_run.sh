#!/usr/bin/env bash
# CUR-P6C gate entry: reproduces the mission's entire evidence battery in
# fresh sequential processes. One invocation, exit 0 iff everything passes.
#
#   1. fail-closed pin check: the tree must descend from the dispatch pin
#      86d5376 (merge-base ancestry -- never HEAD-equality, so the battery
#      also runs clean post-commit)
#   2. cur_p6c_proof.py  -- the full drill battery (fresh process)
#   3. verify_p6c.py     -- fresh-process re-read of the durable records
#
# Heavy-battery discipline: run niced, uncontended (1-min load < 2.0),
# halt-on-first-fail (both drivers exit non-zero at the first failure).
set -euo pipefail

PROOF_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$PROOF_DIR/../.." && pwd)"
PIN="86d5376592fca330666729bbd05b8bd7f83da26b"

echo "== CUR-P6C gate_run: pin check (ancestry of $PIN)"
if ! git -C "$REPO_ROOT" merge-base --is-ancestor "$PIN" HEAD; then
    echo "FAIL: HEAD ($(git -C "$REPO_ROOT" rev-parse --short HEAD)) does not descend from pin $PIN"
    exit 1
fi
echo "pin ok: HEAD descends from $PIN"

echo "== CUR-P6C gate_run: proof battery (fresh process)"
nice -n 10 python3 "$PROOF_DIR/cur_p6c_proof.py"

echo "== CUR-P6C gate_run: fresh-process verifier (fresh process)"
nice -n 10 python3 "$PROOF_DIR/verify_p6c.py"

echo "== CUR-P6C gate_run: ALL GREEN"
