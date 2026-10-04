#!/bin/bash
# CREATIVITY-EXEC-1 gate battery: the Creativity Executive Controller.
# One command, fresh sequential processes, exit 0 iff everything passes.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export WT_ROOT="${WT_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
echo "== CREATIVITY-EXEC-1 gate =="
echo "worktree: $WT_ROOT"
python3 "$SCRIPT_DIR/proof_exec.py"
rc=$?
echo "== gate exit: $rc =="
exit $rc
