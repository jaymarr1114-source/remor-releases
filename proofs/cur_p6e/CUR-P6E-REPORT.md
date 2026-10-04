# CUR-P6E Mission Report — Full Regression Across Landed Curiosity Phase Gates

**Branch:** `cur-p6e` · **Base:** `86d5376` (clean; canonical never moved
during the run) · **Date:** 2026-10-04

## Objective
Re-run every LANDED Curiosity phase-gate battery plus the PLOOP
Primary-seam batteries sequentially in fresh processes against the
current canonical HEAD, proving Curiosity did not disturb the Primary
seam. Report-don't-land: committed, not landed; Felix gates in main chat.

## Landed vs unlanded inventory
- **Landed, regressed here:** Phase 0 (gate record), Phase 1 (P1A–P1E),
  Phase 2 (P2), Phase 3 (P3A/P3B/P3C stage batteries), PLOOP-2/8/9/10/11/12.
- **Excluded by mandate (reported but UNLANDED — gated individually in
  the main chat):** CUR-P4A/P4B/P4C, CUR-P5A/P5B/P5C, CUR-P6A/P6B/P6C/P6D.
  Their batteries live on their unlanded branches, not in canonical.

## Per-battery results (all fresh sequential processes, uncontended)

| # | Battery | Result |
|---|---------|--------|
| 1 | cur_p0 — Phase 0 gate (at its own pin 44e19e4; see note) | 50/50 |
| 2 | cur_p1a — evidence store fence | 18/19 — 1 STALE-DOCUMENTED (A5b) |
| 3 | cur_p1b — enforcement | 28/28 |
| 4 | cur_p1c — FRM | 15/15 |
| 5 | cur_p1d — attribution | 25/25 |
| 6 | cur_p1e — roll-call | 23/23 |
| 7 | cur_p2 — Phase 2 adversarial | 113/113 |
| 8 | cur_p3a — scientific inquiry (stage + integration) | 62 + 35 |
| 9 | cur_p3b — creative exploration (stage) | 64/65 — 1 STALE-DOCUMENTED (t10 gate 3) |
| 10 | cur_p3c — discovery/novelty (stage) | 61/61 |
| 11 | ploop2 — handoff contract | 49/49 |
| 12 | ploop8 — checkpoints | 21/21 |
| 13 | ploop9 — terminal routing | 152/152 |
| 14 | ploop10 — adoption | 46/46 |
| 15 | ploop11_live — Primary live path | 33/33 |
| 16 | ploop11_killresume — kill/resume | 7/7 |
| 17 | ploop12_seam — acceptance seam | 20/20 |
| 18 | ploop12_killresume — kill/resume | 7/7 |
| 19 | seam_check — Primary-seam structural inspection (new) | 8/8 |

**Total: 837 checks green, 0 genuine failures, 2 documented stale
exemptions.** Every battery ran unmodified; logs preserved under
`proofs/cur_p6e/logs/`.

## The two stale expectations (never patched, loudly documented)
Full causal chains in `STALE_EXPECTATIONS.md`:
1. **P1A A5b** — the battery's 2026-09-29 "no non-curiosity module
   imports runtime.curiosity" scan fires on
   `runtime/creativity/budget.py`, which legitimately consumes the
   FRM's real grant path per James's D-6 decision (resources flow
   through the FRM; independently gated, landed). The fenced property
   it proxies (evidence-store write fence) still holds — A5 passes.
2. **P3B t10 gate 3** — expects the pre-P3A-INT `ValueError("unknown
   curiosity loop")`; the James-authorized, gated, landed P3A-INT
   registry refactor (7edecc6) now refuses via the dedicated
   `AdmissionRefused`. The U-1 fence still fires — loudly, naming the
   fenced registry. (P3B-INT has not landed; the loop stays
   unregistered, correctly.)

The orchestrator (`regress.sh`) verifies each exemption's EXACT
failure signature before continuing; any other failure halts.

## Primary-seam verdict — PROVEN
- All eight PLOOP Primary-seam batteries green (2, 8, 9, 10, 11 live +
  kill/resume, 12 seam + kill/resume).
- New structural inspection (`seam_check.py`, 8/8): the Curiosity-owned
  interface the Primary consumes (`FrmGrant`: `grant_id`, `epoch_id`,
  `budget_s`, `issue`) is intact; the Primary-side curiosity import set
  is exactly the known gated set (`granted_cognition.py`, from
  BRAIN-SCAFFOLD-1 — no seam drift); `HANDOFF_ROUTES` still declares
  answers for all six loops; the curiosity executive's public surface
  intact.

## Notes
- The Phase 0 battery is hard-pinned to 44e19e4 (fail-closed) and
  provisions its own worktree there — it re-proves Phase 0 at its
  original gate pin. The Phase 0 mechanisms are re-proven at the
  current HEAD by the Phase 1 batteries.
- Battery side-effects (P1B manifest overwrite, P2 regenerated run
  dirs) were reverted after the run; the final tree contains only
  `proofs/cur_p6e/`. No runtime file was written by this mission.
- Gate queue checked before the run (no active heavy battery); load
  0.55 at start; niced; sequential; halt-on-first-fail.

## What remains unproven
- The unlanded phases (P4, P5, P6A–D) — each gated individually.
- James's hardware acceptance (standing).
- The two stale batteries' upstream updates (P1A's A5b scan and P3B's
  t10 gate-3 should eventually be revised by their owning tracks to
  match the landed architecture — a track-hygiene item, not a defect).

## Exact next boundary
**CUR-P6F** — fresh-process persistence of enforcement, Evidence
Store, and grants/allocation records.
