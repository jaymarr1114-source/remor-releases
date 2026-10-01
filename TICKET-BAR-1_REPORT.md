# TICKET-BAR-1 — Report (coordinator → Felix)

## Recovery context
The previous coordinator died in a daemon restart leaving uncommitted edits to
`distill1/eval_grade.py` and `distill1/ticket.py`. I verified every inherited
line against the mission packet before adopting it, and found + repaired one
real defect in the inheritance: the inserted functions had mangled `seal()`'s
def line (`def seal(...):    """docstring...` spanning lines), which made
`ticket.py` unimportable (IndentationError). Fixed by restoring a normal
def + docstring layout. No gate script or report existed; both are mine.

## Objective met
`eval_grade.py --tally` now derives its pass verdict from
`exit_criteria.held_out` in the sealed ticket via a predicate exposed by
`ticket.py`. Re-sealing the ticket with a stricter `min_mean` provably flips a
known-passing result to FAIL, through the real CLI path, in both directions.

## Exact mechanism change
- `distill1/ticket.py`:
  - `held_out_bar(ticket)` — reads `(min_mean, max_clarification_zeros,
    must_beat_base)` from the ticket's sealed exit criteria. Runs
    `validate()` first; raises `ValueError` on any schema violation. A ticket
    missing its bar refuses to grade rather than falling back to a silent
    default.
  - `grade_held_out(ticket, stud_mean, clar_zeros, base_mean)` — the ticket's
    held-out pass predicate, the single source of truth. Returns
    `(passes, detail_dict)` with per-check keys named after the ticket's bar
    values (e.g. `student_mean_ge_1.9`).
  - `seal()` unchanged in behavior (already read the bar from the ticket, not
    literals); only its def-line formatting was repaired.
- `distill1/eval_grade.py`:
  - `tally(sheet_path, key_path, ticket_path=None)` — loads the ticket
    (default `distill1/ticket_distill1.json`), calls `grade_held_out`; the
    hard-coded `1.5` / `== 0` literals are gone. `pass_bar` now carries the
    ticket's `ticket_id` and the absolute `bar_source` path for auditability.
  - `ValueError` from the predicate → clean `TALLY REFUSED: ...` on stderr,
    exit 2 (no traceback, no silent default).
  - New `--ticket` CLI flag so re-sealed tickets are graded through the real
    call path (previously the tally could only read the default ticket file).
  - Import shim: `sys.path` insert + `from ticket import grade_held_out`,
    fallback `from distill1.ticket import grade_held_out` for
    `python -m distill1.eval_grade` from the repo root.

## Gate evidence (`proofs/ticket_bar1/gate_run.sh` — 14/14 PASS, exit 0)
Real runs, fresh sequential processes, this host (worktree
`~/workspace/worktrees/ticket-bar-1`, branch `ticket-bar-1-work`):

1. **Structural** — grep confirms no `1.5`/`== 0` literals remain in the
   tally path; `ticket.py` exposes the predicate. (2 checks)
2. **Baseline** — tally of the real DISTILL-1 blinded sheet
   (`distill1/grading_sheet_scored.json`): base_mean 1.667, student_mean
   1.833, clar_zeros 0 → PASS, exit 0, `bar_source` =
   `.../distill1/ticket_distill1.json`, key `student_mean_ge_1.5`. (2 checks)
3. **Causal flip** — ticket copied, `min_mean` set to 1.9 (> student mean
   1.833), genuinely re-sealed via `ticket.seal()` (status recomputed from
   criteria: FAIL, `held_out_mean` check False — never hand-set). Tally with
   `--ticket` copy → FAIL, exit 1, key `student_mean_ge_1.9: false`.
   Original ticket → PASS, exit 0 again. Both directions. (4 checks)
4. **Pre-fix contrast** — `HEAD`'s `eval_grade.py` run against the strict
   ticket (as its default ticket) → PASS with key `student_mean_ge_1_5`:
   the audited bug reproduced (silently contradicted the sealed 1.9 bar),
   then fixed. (1 check)
5. **Adversarial** — ticket with `exit_criteria.held_out` deleted → 
   `TALLY REFUSED: ticket fails validation, cannot grade:
   ['exit_criteria missing: held_out', ...]`, exit 2, no verdict printed.
   Corrupt JSON ticket → non-zero exit, no verdict. (3 checks)
6. **Pinned residual** — ticket with `result.status` hand-flipped to PASS:
   `validate()` returns `[]` (no detection) and the tally ignores
   `result.status` (verdict still computed from criteria). Pinned as the
   known integrity gap. (1 check)
7. **Regression** — `proofs/distill1/gate_run.sh` green (7 passed, 0 failed).
   (1 check)

## Per-claim classification
- Tally reads the bar from the sealed ticket: **PROVEN** (structural +
  behavioral, both directions).
- Re-seal flips the verdict: **PROVEN** (genuine `seal()` recompute + CLI
  tally, exit codes 1/0).
- Pre-fix code ignored the ticket: **PROVEN** (old binary run against strict
  ticket → PASS).
- Missing bar refuses honestly: **PROVEN** (exit 2, named error, no default).
- DISTILL-1 battery stays green: **PROVEN** (7/7).
- Cryptographic tamper-evidence on tickets: **ABSENT** — "seal" is a
  computed-status convention, not a signature; hand-edited `result.status`
  is undetectable by `validate()`. Pinned in the gate so the behavior is
  locked until addressed.

## What remains unproven
- Behavior on a ticket whose `held_out` values have wrong types (e.g.
  `min_mean: "high"`): `validate()` checks presence, not types; comparison
  would raise `TypeError` (uncaught → traceback, non-zero exit — honest but
  ugly). Not exercised.
- `python -m distill1.eval_grade` from the repo root (fallback import
  branch): code-reviewed, not executed.
- Real-device behavior; Felix's independent gate re-run.

## Exact next boundary
Cryptographic ticket seal: `result.status` (and the bar itself) can be
hand-edited without detection — `validate()` is schema-only. The next
mission is to add a hash-chained or signed seal (e.g. seal over
`sha256(canonical ticket bytes + measurements)`, verified by `validate()` /
`held_out_bar()`), with a gate that flips one byte and shows refusal.
That is outside this mission's "cheapest fix" mandate; named, not started.

## Incidents
- Inherited `ticket.py` was unimportable (IndentationError from the previous
  coordinator's edit) — repaired, verified by import + full gate.
- No protected-tree touches. Nothing landed (report only).

## Commit
Worktree `~/workspace/worktrees/ticket-bar-1`, branch `ticket-bar-1-work`.
Commit `8c9f3a2` (this report + gate script + the two source fixes). Report,
do not land.
