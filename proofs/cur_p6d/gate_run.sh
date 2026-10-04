#!/usr/bin/env bash
# CUR-P6D gate_run: reproduce the entire evidence battery in fresh sequential
# processes. One invocation, exit 0 on green. Fail-closed pin check via
# merge-base ancestry (never HEAD-equality). No hardcoded home paths.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WT="$(cd "$HERE/../.." && pwd)"
PIN="86d5376592fca330666729bbd05b8bd7f83da26b"

echo "== CUR-P6D gate_run: pin check (ancestry of $PIN)"
if ! git -C "$WT" merge-base --is-ancestor "$PIN" HEAD; then
  echo "PIN-FAIL: HEAD does not descend from $PIN"
  exit 3
fi
echo "pin ok: HEAD descends from $PIN"

echo "== CUR-P6D gate_run: ownership check (new files only)"
BAD="$(git -C "$WT" diff --name-only "$PIN" -- . | grep -v '^proofs/cur_p6d/' | grep -v '^runtime/curiosity/hardening/' || true)"
if [ -n "$BAD" ]; then
  echo "OWNERSHIP-FAIL: touched paths outside owned dirs:"
  echo "$BAD"
  exit 4
fi
echo "ownership ok: only owned paths touched"

echo "== CUR-P6D gate_run: load check (two consecutive, < 2.0)"
for i in 1 2; do
  LOAD="$(awk '{print $1}' /proc/loadavg)"
  echo "load sample $i: $LOAD"
  if ! awk -v l="$LOAD" 'BEGIN{exit !(l < 2.0)}'; then
    echo "LOAD-HIGH: $LOAD >= 2.0 -- battery deferred"
    exit 5
  fi
  [ "$i" = "1" ] && sleep 20
done

echo "== CUR-P6D gate_run: proof battery (fresh process)"
nice -n 10 python3 "$HERE/cur_p6d_proof.py" || exit 6

echo "== CUR-P6D gate_run: fresh-process verifier (fresh process)"
nice -n 10 python3 "$HERE/verify_p6d.py" || exit 7

echo "== CUR-P6D gate_run: ALL GREEN"
