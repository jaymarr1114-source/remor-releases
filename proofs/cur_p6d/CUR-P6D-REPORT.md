# CUR-P6D Mission Report — D-3 Re-enable Paths

**Branch:** `cur-p6d` · **Base:** `86d5376` (clean; canonical never moved during the run)
**Status:** report-don't-land — committed, not landed. Felix gates in main chat.

## Objective
Every row of the D-3 re-enable table exercised through the real enforcement
machinery against live ban/suspension/warning states: the authorized issuer
succeeds with the required conditions, every unauthorized issuer is refused —
and the Primary executive cannot override a safety/ban state (invariant 9).

## D-3 rows in their exact wording (kill policy, "Per-level re-enable rules —
DECIDED 2026-09-29 (James)")

| Level | Re-enable / re-entry (verbatim) | Battery rows |
|---|---|---|
| L1 — Resource ceiling | "Automatic eligibility at next valid allocation epoch; FRM must grant resources again" | T01, T02 |
| L2 — Safety suspension | "Explicit external re-enable, not Curiosity; default authority is James unless a standing delegation explicitly covers this class" | T03, T04 |
| L3 — Six-month ban | "James-controlled re-entry after six months, plus required verification before execution resumes" | T05 |

## Invariant 9 (verbatim, kill policy §"Governance Invariants")
> "Primary does not acquire authority to override an external safety or ban
> state merely because Curiosity is operationally useful."

Implementation: `ISSUER_PRIMARY` is authorized for zero transitions in the
D-3 transition table (`runtime/governance/curiosity_enforcement/states.py`).

## Per-mandate results

**M1 — Fresh structural re-map: PROVEN.** Pin verified fail-closed before any
work; the D-3 table, the transition table, the engine, the guard, and the
read API re-read at the pin.

**M2 — D-3 matrix: PROVEN** (`proofs/cur_p6d/cur_p6d_proof.py`, driver as
`__main__` on the governance plane; 31 attempts, 18 refused / 13 success):
- L1: FRM shutdown (james-issuer shutdown refused first); FRM re-enable
  without `grant_ref` → ReenableRefused; james/primary with grant →
  IssuerRefused; FRM + `grant_ref` → RUNNING. State unchanged after every
  refusal (fresh-subprocess reads).
- L2 warning: curiosity / primary / safety-authority / enforcement-mechanism
  → IssuerRefused (the entry authority is not the re-enable authority);
  james → RUNNING.
- L2 suspension: primary / safety-authority / curiosity → IssuerRefused;
  james → RUNNING. Rollback directive recorded on suspension (1, pending).
- L3 ban (via the real `transition` with issuer `enforcement-mechanism` —
  the exact call the L3 combiner makes; fact-combination upstream proven in
  CUR-P6C): james before expiry → ReenableRefused ("ban has not expired");
  clock advanced 200 days, james without `verification_ref` →
  ReenableRefused; primary with everything → IssuerRefused; james + expiry +
  `verification_ref=ver-p6d-001` → RUNNING.

**M3 — Invariant-9 adversarial: PROVEN.**
- Primary's only enforcement path is the same engine API (the package
  namespace exposes no mutation names — structurally asserted: no
  transition/re_enable/clear/reset/override reachable); issuer "primary" is
  authorized for zero of the ten D-3 rows → IssuerRefused on every level.
- No standing delegation is registered anywhere in the implementation:
  impostor "james-delegate" → IssuerRefused. James-only is the default and
  the fact.
- Curiosity-frame probe (real `swarm_engine.curiosity.*` frame): even the
  AUTHORIZED issuer string ("james") from a curiosity frame →
  DomainSeparationError at the persistence write; the rollback-ack write path
  likewise → DomainSeparationError. The guard is caller-based, not just
  issuer-based.

**M4 — James-only default / delegation: PROVEN.** Where the D-3 input is
silent, james-only holds; the one place delegation could exist (L2's
"unless a standing delegation explicitly covers this class") has no
delegation registered — proven by the impostor-delegate refusal.

**M5 — Records: PROVEN.** Kill ledger: 4 KILLED entries, chain valid
(fresh-process verify). Final record: RUNNING, prev BANNED_6M, issuer james,
`verification_ref=ver-p6d-001`, cross-referencing the lifted ban record's
identifying fields. Every attempt (31) in `p6d_attempt_log.json` with issuer,
level, conditions, outcome, refusal class.
**Honest bound (named, not faked):** the engine records successful
transitions only; refusals raise and are evidenced by the exception + the
unchanged state (fresh-process read) + the battery's attempt log. There is no
engine-side attempt-audit log for refusals — the battery's log is the audit
trail for refusals.

**M6 — Heavy-battery discipline: followed.** Gate queue clear (no VERIFYING
battery); load 0.47 at start; niced; halt-on-first-fail.

**M7 — gate_run.sh: shipped, exit 0** (ancestry pin check, ownership guard,
load gate, fresh sequential battery + verifier).

**M8 — committed** with explicit pathspec, neutral message, no verification
claims.

## Incidents (all battery-expectation bugs, none tree defects)
- I1: expected 3 kill-ledger entries; the drill legitimately produces 4
  (suspension is entered twice — T04 and T05). Fixed expectation, reran.
- I2: T06's trailing WARNING_1→RUNNING record overwrote the ban-re-entry
  record as the current record; reordered T06 before T05 so the final record
  is the ban re-entry. Reran.

## Exact next boundary
**CUR-P6E** — full regression across all prior Curiosity phase gates
(PLOOP-2/8/9/10/11/12 re-run sequentially; Curiosity must not disturb the
Primary seam). Separately queued; not absorbed here.
