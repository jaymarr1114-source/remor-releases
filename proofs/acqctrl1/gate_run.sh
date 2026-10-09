#!/usr/bin/env bash
# ACQ-CTRL-1 gate battery: the controller drives mine -> acquire -> close,
# plus the retry -> expand -> terminal policy. Fresh processes, exit 0 iff green.
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export no_proxy=localhost,127.0.0.1
FAIL=0
echo "=== ctrl_e2e: mine -> acquire -> close under the controller ==="
python3 "$DIR/ctrl_e2e.py" || FAIL=1
echo
echo "=== ctrl_policy: retry -> expand -> terminal ==="
python3 "$DIR/ctrl_policy.py" || FAIL=1
echo
if [ "$FAIL" -eq 0 ]; then echo "GATE_GREEN"; else echo "GATE_RED"; fi
exit "$FAIL"
