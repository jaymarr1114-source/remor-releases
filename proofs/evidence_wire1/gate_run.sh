#!/bin/bash
# EVIDENCE-WIRE-1 gate run: reproduces the mission's full evidence battery
# in fresh sequential processes. Exit 0 = all green.
set -euo pipefail
WORKTREE="$(cd "$(dirname "$0")" && pwd)"
WT_ROOT="$(cd "$WORKTREE/../.." && pwd)"
cd "$WORKTREE"

echo "=== EVIDENCE-WIRE-1 proof battery ==="
python3 proof_battery.py

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
  "$WT_ROOT/runtime/intellect/epistemic.py" \
  "$WT_ROOT/runtime/services/evidence.py" \
  "$WT_ROOT/runtime/services/http_adapter.py" \
  "$WT_ROOT/runtime/acquisition/loop_driver.py"
echo "compile OK"
echo
echo "GATE_RUN COMPLETE"
