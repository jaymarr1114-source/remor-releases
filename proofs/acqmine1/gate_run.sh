#!/usr/bin/env bash
# ACQ-MINE-1 gate battery. One command, fresh processes, exit 0 iff green.
# mine_e2e: mine -> gap -> acquire-close (12 checks) + gapless negative control.
# mine_adv: adversarial battery (5 checks) -- invalid/admitted/pairless
#           deltas skipped, empty-evidence registration refused, no duplicates.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

LOAD=$(cut -d' ' -f1 /proc/loadavg)
echo "load_1m=$LOAD"

echo "=== mine_e2e ==="
python3 proofs/acqmine1/mine_e2e.py
echo "=== mine_adv ==="
python3 proofs/acqmine1/mine_adv.py
echo "GATE_EXIT=0"
