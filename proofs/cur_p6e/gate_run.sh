#!/bin/bash
# CUR-P6E gate_run.sh — reproduces the full Phase 6e regression in
# fresh sequential processes. One invocation, exit 0 = every landed
# phase-gate battery green. No hardcoded home paths (derived from
# $0); the pre-existing Phase 0 battery it invokes carries its own
# pin and home-anchored paths by design.
set -u

PIN_FULL="86d5376592fca330666729bbd05b8bd7f83da26b"
ROOT="$(cd "$(dirname "$0")" && pwd)"
WORKTREE="$(cd "$ROOT/../.." && pwd)"
cd "$WORKTREE" || exit 1

fail_closed() { echo "FATAL: $1" >&2; exit 2; }

echo "== CUR-P6E gate_run: pin check (ancestry of $PIN_FULL)"
git merge-base --is-ancestor "$PIN_FULL" HEAD \
  || fail_closed "HEAD does not descend from $PIN_FULL"
echo "pin ok: HEAD descends from $PIN_FULL"

echo "== CUR-P6E gate_run: ownership check (no runtime writes)"
RUNTIME_DIRT="$(git status --short | grep -E ' runtime/' || true)"
if [ -n "$RUNTIME_DIRT" ]; then
  fail_closed "runtime/ tree dirtied: $RUNTIME_DIRT"
fi
echo "ownership ok: runtime/ untouched (mission writes only proofs/cur_p6e/)"

echo "== CUR-P6E gate_run: load check (two consecutive, < 2.0)"
for i in 1 2; do
  LOAD="$(uptime | awk -F'load average: ' '{print $2}' | cut -d, -f1 | tr -d ' ')"
  echo "load sample $i: $LOAD"
  awk -v l="$LOAD" 'BEGIN { exit (l < 2.0) ? 0 : 1 }' \
    || fail_closed "machine too loaded ($LOAD) — refusing to run"
  [ "$i" = 1 ] && sleep 10
done

echo "== CUR-P6E gate_run: regression battery (fresh sequential processes)"
bash "$ROOT/regress.sh" || exit 1

echo "== CUR-P6E gate_run: ALL GREEN"
exit 0
