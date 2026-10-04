#!/usr/bin/env bash
# CUR-HARDEN-1 gate: record-level integrity for the three stores P6F proved
# silently accepted single-byte corruption. One invocation, fresh sequential
# processes, exit 0 only if everything passes.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="$(dirname "$(dirname "$HERE")")"
PIN=aa7089c17d9ad4f983c3c5e7339688a92fdbb1f2

echo "== CUR-HARDEN-1 gate_run: pin check (ancestry of $PIN)"
if ! git -C "$WORKTREE" merge-base --is-ancestor "$PIN" HEAD; then
  echo "FAIL: HEAD does not descend from $PIN"; exit 1
fi
echo "pin ok: HEAD descends from $PIN"

echo "== CUR-HARDEN-1 gate_run: ownership check (mission paths only)"
BAD="$(git -C "$WORKTREE" diff "$PIN" --name-only \
  | grep -v '^proofs/cur_harden_1/' \
  | grep -v '^runtime/curiosity/hardening/' \
  | grep -v '^runtime/curiosity/evidence/store.py$' \
  | grep -v '^runtime/curiosity/frm/ledger.py$' \
  | grep -v '^runtime/governance/curiosity_enforcement/_persistence.py$' \
  || true)"
if [ -n "$BAD" ]; then
  echo "FAIL: files outside owned paths:"; echo "$BAD"; exit 1
fi
echo "ownership ok: only owned paths touched"

echo "== CUR-HARDEN-1 gate_run: load check (two consecutive, < 2.0)"
for i in 1 2; do
  LOAD="$(cut -d' ' -f1 /proc/loadavg)"
  echo "load sample $i: $LOAD"
  if ! python3 -c "import sys; sys.exit(0 if float('$LOAD') < 2.0 else 1)"; then
    echo "FAIL: load $LOAD >= 2.0 -- refusing to run contended"; exit 1
  fi
  [ "$i" = "1" ] && sleep 20
done

echo "== CUR-HARDEN-1 gate_run: proof battery (fresh process)"
if ! nice -n 10 python3 "$HERE/cur_harden_1_proof.py"; then
  echo "FAIL: proof battery"; exit 1
fi

echo "== CUR-HARDEN-1 gate_run: fresh-process verifier (fresh process)"
if ! nice -n 10 python3 "$HERE/verify_harden_1.py"; then
  echo "FAIL: fresh-process verifier"; exit 1
fi

echo "== CUR-HARDEN-1 gate_run: ALL GREEN"
