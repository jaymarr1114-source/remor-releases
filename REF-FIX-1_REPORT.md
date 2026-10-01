# REF-FIX-1 Report — dead branch in `tests_for_graph` removed

**Date:** 2026-10-01
**Worktree:** `~/workspace/worktrees/ref-fix-1` (branch `ref-fix-1-work`, from canonical `73b38a1`)
**Status:** objective met — REPORTED, awaiting Felix's independent gate.

## Decision (by evidence, not taste): REMOVE, not repair

Evidence for removal:
1. The dead branch (`if lu and lu.op == "lookup_value":`, formerly
   `runtime/acquisition/atomic_operators.py:3002-3019`) was **born dead** in the
   file's first commit (`d017672`, "G0: canonical parent"); `git log -S "tests = []"`
   shows the name `tests` was never bound anywhere in this file's history.
2. The `lu` selector (`n.op in ("lookup_pct_apply", "lookup_mul")`) can never yield
   a node with `op == "lookup_value"` — the inner predicate was permanently False.
3. Even if reached, the branch was **contract-incoherent**: `tests.append(...)` on
   an undefined name, in a function whose contract is to return a `str` (the caller
   `synthesize_from_operator_graph` writes the return value as `tests/test_app.py`);
   its generated snippets referenced a bare `run(...)` with no imports — broken test
   code regardless.
4. The intended capability (graph-derived tests for lookup ops) already exists through
   live paths: the `lookup_pct_apply`-family early-return branch (`:3020`, formerly
   `:3021`) and the M+29.20 worked-examples path / smoke-test fallback. `lookup_value`
   graphs fall through to those honestly today. Repairing the branch would have
   *changed* behavior for `lookup_value` graphs (inventing a new contract: bind a
   list, join it, return it) — speculative feature work, not a repair.

## Change

One surgical edit in `runtime/acquisition/atomic_operators.py`: deleted the 18-line
dead block (`if lu and lu.op == "lookup_value":` + its `tests.append(...)` calls).
No other line touched. The `lu = next(...)` line and the live `lookup_pct_apply`
branch are unchanged.

## Per-mandate evidence

1. **Fresh structural re-map** — PROVEN. Confirmed the audit's line numbers and the
   call chain `tests_for_graph` (`:2991`) ← `synthesize_from_operator_graph` (`:3204`,
   call at `:3214`) in the worktree before editing.
2. **Decision by evidence** — PROVEN (see above; git history + caller contract + live
   sibling paths).
3. **Causal proof of the decision** — PROVEN. Battery b1 captured the exact output of
   `tests_for_graph` on 9 graph shapes (lookup_value / lookup_pct_apply / lookup_mul /
   plain / format_scalar_field / both-lookups / empty / with-IR variants) BEFORE the
   fix; AFTER the fix all 9 outputs are byte-identical (sha256 match). The function
   fulfills its contract for every caller input, provably unchanged.
4. **Adversarial** — PROVEN. Battery b2 drives the exact former-`NameError` inputs:
   a graph with a real `lookup_value` node + table (a1), empty table (a2),
   lookup_value+lookup_mul (a3), hostile params (a4), and with worked-examples IR
   (a5). None raises; all compile; all fall through honestly to the live paths
   (no `test_lookup_value`/`test_lookup_unknown` dead-branch output anywhere).
   Caller contract proven: the caller-written `tests/test_app.py` parses and
   contains real test functions.
5. **Existing batteries** — PROVEN BUT BOUNDED. No existing test file imports
   `atomic_operators` or exercises `synthesize_from_operator_graph` (verified by
   tree-wide grep); the adjacent acquisition suites (`tests/acquisition/`) cover
   ingestion, a different module, and are unaffected by a change confined to one
   function. b1's 9-case equivalence battery is the operative coverage; the module
   imports cleanly in a fresh process.
6. **Gate script** — PROVEN. `proofs/ref_fix1/gate_run.sh` reproduces b0 (import),
   b1 (equivalence vs committed `before.json`), b2 (adversarial), b3 (no bare
   `tests` remains) in fresh sequential processes; exit 0, all green.

## Incidents

None. No protected-tree touches; no mid-run failures. One environment note: the
`grep -r` scout over the whole tree backgrounded under host load (12+ coordinators);
rescoped greps completed fine. No model server needed — this mission touches no
inference path (per the new AGENTS.md lesson, batteries that need none start none).

## Exact next boundary

None within this mission's scope — the dead branch is gone and behavior is proven
identical. The adjacent audit item in the same file region (G-10/TICKET-BAR-1 and
the unreachable `lookup_pct_surcharge`/`lookup_factor_apply`/`lookup_add` arms
inside the live branch's condition at `:3020`) are owned by other missions; the
latter is cosmetic dead-condition text with zero behavioral effect and was left
untouched as out of scope.

## What remains unproven

- Third-party import surface of `atomic_operators.py` (numpy etc.) was not
  re-verified — out of scope, unchanged by this edit.
- No end-to-end `synthesize_from_operator_graph` run with a full `SoftwareSpecIR`
  was executed — the caller was not modified and its contract (str in → file out)
  is proven by b2's caller-contract check; a full synthesis run is the owning
  missions' (M+29 track) battery, not this mission's.

## Classification summary

- Dead branch removed: PROVEN
- Behavioral equivalence (9/9 byte-identical): PROVEN
- Former-NameError inputs behave honestly: PROVEN
- No existing-test regression: PROVEN BUT BOUNDED (no existing battery covers the
  function; b1 is the coverage)
