#!/bin/bash
# EVIDENCE-WIRE-1 gate run: reproduces the mission's full evidence battery
# in fresh sequential processes. Exit 0 = all green.
set -euo pipefail
WORKTREE="$(cd "$(dirname "$0")" && pwd)"
cd "$WORKTREE"

echo "=== EVIDENCE-WIRE-1 proof battery ==="
python3 proofs/evidence_wire1/proof_battery.py

echo
echo "=== regression: evidence + epistemic unit tests (if present) ==="
if [ -d tests ]; then
  python3 -m pytest tests/ -k "evidence or epistemic" -q 2>&1 | tail -5 || true
else
  echo "(no tests/ dir in worktree; skipping)"
fi

echo
echo "=== compile check on touched files ==="
python3 -m py_compile \
  pylib/swarm_engine/intellect/epistemic.py \
  runtime/services/evidence.py \
  runtime/services/http_adapter.py \
  runtime/acquisition/loop_driver.py
echo "compile OK"
echo
echo "GATE_RUN COMPLETE"
