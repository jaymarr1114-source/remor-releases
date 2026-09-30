#!/bin/bash
# CREATIVITY-SLICE-1 gate: reproduces the mission's full evidence
# battery in fresh sequential processes. Felix's independent re-run
# is this one command from the worktree root:
#
#   ./gate_run.sh
#
# Batteries run SEQUENTIALLY (never in parallel): on the 2-core host
# concurrent proofs contend and produce spurious failures that look
# like regressions. Order matters: rd1_fresh_process re-opens the
# DBs rd1_adversarial leaves behind, so it runs immediately after it.
#
# The mission battery is proofs/creativity_slice_proof.py (the D-5
# stages + D-4 intent register). The six rd1_* batteries are carried
# regressions from RD-TARGET-REMOTE-1 (base 08fad24): the new
# runtime/creativity/ package is additive, but the tree must stay
# green as one integrated system.
#
# Exit 0 only if every battery passes. Per-battery logs are kept
# under /tmp/rd1_gate_logs/.
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="/tmp/rd1_gate_logs"
mkdir -p "$LOGDIR"
export DISPLAY=":99"

XVFB_PID=""
cleanup_xvfb() {
    if [ -n "$XVFB_PID" ] && kill -0 "$XVFB_PID" 2>/dev/null; then
        kill "$XVFB_PID" 2>/dev/null
    fi
}

# Display check via python3/Xlib (xdpyinfo is not installed on this
# host). The pgrep patterns below can never match this script's own
# command line (bracket trick), and we prefer the captured PID over
# any pattern kill.
display_up() {
    python3 -c "from Xlib.display import Display
d = Display(':99'); d.close()" 2>/dev/null
}
if display_up; then
    echo "[gate] X display :99 already up"
else
    echo "[gate] starting Xvfb :99"
    Xvfb :99 -screen 0 1280x1024x24 >"$LOGDIR/xvfb.log" 2>&1 &
    XVFB_PID=$!
    trap cleanup_xvfb EXIT
    for i in $(seq 1 20); do
        display_up && break
        sleep 0.5
    done
    display_up \
        || { echo "[gate] FATAL: Xvfb :99 did not come up"; exit 2; }
fi

declare -a BATTERIES=(
    "proofs/creativity_slice_proof.py"
    "proofs/rd1_endpoint_proof.py"
    "proofs/rd1_proof.py"
    "proofs/rd1_adversarial.py"
    "proofs/rd1_fresh_process.py"
    "proofs/rd1_remote_interop.py"
    "proofs/rd1_inproc_product_path.py"
)

OVERALL=0
cd "$ROOT"
for b in "${BATTERIES[@]}"; do
    name="$(basename "$b" .py)"
    log="$LOGDIR/${name}.log"
    echo "=== [gate] running $b ==="
    if python3 -u "$b" >"$log" 2>&1; then
        echo "[gate] $name: EXIT 0"
    else
        echo "[gate] $name: EXIT $?  <-- FAILED (see $log)"
        OVERALL=1
    fi
    # Per-battery pass/fail summary line, whatever the battery prints.
    grep -E "checks, .* passed|green$|/.*green" "$log" | tail -2
    grep -E "^\[FAIL\]|FAILED" "$log" | head -5
    echo
done

# The shared display must be clean: no battery may leave an indicator
# window behind for the next one.
LEFTOVER="$(python3 - "$DISPLAY" <<'EOF' 2>/dev/null
import sys
from Xlib.display import Display
d = Display(sys.argv[1] if len(sys.argv) > 1 else ":99")
found = []
def walk(w):
    try:
        name = w.get_wm_name()
    except Exception:
        name = None
    if name == "REMOR Remote Session LIVE":
        found.append(name)
    try:
        kids = w.query_tree().children
    except Exception:
        return
    for k in kids:
        walk(k)
walk(d.screen().root)
d.close()
print(len(found))
EOF
)"
if [ "$LEFTOVER" != "0" ]; then
    echo "[gate] DISPLAY POLLUTION: $LEFTOVER leftover indicator window(s) on :99"
    OVERALL=1
else
    echo "[gate] display :99 clean (no leftover indicator windows)"
fi

if [ "$OVERALL" -eq 0 ]; then
    echo "[gate] ALL BATTERIES GREEN"
else
    echo "[gate] GATE FAILED"
fi
exit "$OVERALL"
