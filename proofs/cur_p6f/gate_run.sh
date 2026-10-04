#!/usr/bin/env bash
# CUR-P6F gate: fresh-process persistence of enforcement, Evidence Store,
# and grants/allocation records. One invocation, fresh sequential processes,
# exit 0 only if everything passes.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="$(dirname "$(dirname "$HERE")")"
PIN=86d5376592fca330666729bbd05b8bd7f83da26b

echo "== CUR-P6F gate_run: pin check (ancestry of $PIN)"
if ! git -C "$WORKTREE" merge-base --is-ancestor "$PIN" HEAD; then
  echo "FAIL: HEAD does not descend from $PIN"; exit 1
fi
echo "pin ok: HEAD descends from $PIN"

echo "== CUR-P6F gate_run: ownership check (new files only)"
BAD="$(git -C "$WORKTREE" diff "$PIN" --name-only | grep -v '^proofs/cur_p6f/' | grep -v '^runtime/curiosity/hardening/' || true)"
if [ -n "$BAD" ]; then
  echo "FAIL: files outside owned paths:"; echo "$BAD"; exit 1
fi
echo "ownership ok: only owned paths touched"

echo "== CUR-P6F gate_run: load check (two consecutive, < 2.0)"
for i in 1 2; do
  LOAD="$(cut -d' ' -f1 /proc/loadavg)"
  echo "load sample $i: $LOAD"
  if ! python3 -c "import sys; sys.exit(0 if float('$LOAD') < 2.0 else 1)"; then
    echo "FAIL: load $LOAD >= 2.0 -- refusing to run contended"; exit 1
  fi
  [ "$i" = "1" ] && sleep 20
done

echo "== CUR-P6F gate_run: proof battery (fresh process)"
if ! nice -n 10 python3 "$HERE/cur_p6f_proof.py"; then
  echo "FAIL: proof battery"; exit 1
fi

echo "== CUR-P6F gate_run: fresh-process verifier (fresh process)"
if ! nice -n 10 python3 "$HERE/verify_p6f.py" "$HERE/run/t01_state"; then
  echo "FAIL: fresh-process verifier"; exit 1
fi

echo "== CUR-P6F gate_run: ALL GREEN"
