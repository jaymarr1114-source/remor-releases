# CREATIVITY-LEDGER-1 — Mission Report

**Mission:** CREATIVITY-LEDGER-1 — the verified-substrate ledger (paper §3 mechanism)
**Coordinator:** Creativity executive track side chat (4ce1de99)
**Date:** 2026-10-01
**Branch:** `creativity-ledger-1-work` (worktree `~/workspace/worktrees/creativity-ledger-1`)
**Base:** canonical `v10-runtime` @ `44e19e4` (GAP-REASONER-WIRE-1)
**Status:** REPORTED — report, do not land. Awaiting Felix's independent gate in main chat.

## End-state vs objective

Objective: REMOR can honestly demonstrate a creativity substrate ledger that
answers, for any candidate composition of primitives, whether every element
sits on the verified side — rejecting the whole composition the moment one
element does not, raising (never silently passing) on quarantined substrate,
and routing unverified demand to the real gap machinery as a visibly named
gap. The ledger holds no verification authority of its own.

End-state achieved on the bench: `runtime/creativity/ledger.py` implements
`LedgerEntry` / `Ledger` / `CompositionVerdict` / `LedgerRefused`; the battery
at `proofs/creativity_ledger1/gate_run.sh` runs 23/23 green in fresh sequential
processes (two consecutive clean runs). All fixtures are real records in
scratch sqlite stores built via the stores' own public APIs. Bench green is
mechanism evidence only — nothing is described as working until James confirms
it on his hardware.

## Per-mandate-item evidence

1. **Fresh structural re-map** (canonical @44e19e4, read not assumed):
   - `runtime/services/evidence.py`: `EvidenceStore` — `verify_entry(id,
     verified_by)` is the attested act (named verifier, timestamp);
     `get_substrate_entry(id)` is the ONLY factual-substrate read path
     (unverified → `{"ok": False, "error": "QUARANTINED_SUBSTRATE_REFUSED:
     ..."}` — returned as a dict, never raised); `list_substrate` is the
     verified-only view with quarantined entries counted but fenced.
     `QuarantinedSubstrateRefused` is the exception for code paths.
   - `runtime/governance/provenance.py`: `ProvenanceRecord` per
     capability_id; `TrustLevel` QUARANTINED=0 < UNKNOWN=1 < SCANNED=2 <
     SANDBOXED=3 < TESTED=4 < TRUSTED=5 (ordinal comparisons);
     `ProvenanceStore.record/get/set_trust/log` are the public API;
     `set_trust` is tamper-evident when oracle-bound, legacy direct-write
     otherwise (documented as unbound — the bench harness is unbound).
   - `runtime/acquisition/gaps.py`: `GapRegistry(engine, db_path)`;
     `register(record)` validates evidence live (`_validate_evidence`),
     assigns `gap_id`, persists, status OPEN. Evidence dicts carry a "kind":
     `observation` needs `observed`+`detail` (no engine dereference),
     `measurement` needs `metric`+`value`, `quarantine_record`/`diagnosis`
     are re-verified LIVE against the engine. `TechniqueBlock` is the right
     shape for "primitive X lacks verification". Confirmed by reading
     `register()`: on the observation-evidence path the engine is never
     dereferenced, so the harness passes `engine=None` honestly.
   - **Ambiguity named, not papered over:** gate crossings are recorded in
     `gate_queue.md` (human-readable). There is NO single machine-readable
     gate-crossing registry in the current tree; the machine-readable proxy
     the ledger consumes is a verified evidence entry citing the gate
     verdict. Recorded here and in the module docstring.
2. **`runtime/creativity/ledger.py`** built:
   - `LedgerEntry` (frozen): primitive_id, verification_source
     ("evidence"|"provenance" only), verification_ref, gate_reference,
     device_proof ("unconfirmed"|"confirmed"|"not_applicable", default
     unconfirmed — carried for the RELEASE phase, does NOT decide legality).
     Construction validates the verification event LIVE; an entry with no
     traceable verification event is refused with the exact reason
     (nonexistent id, unverified entry, verified entry not citing the
     primitive — the citation discipline — empty gate reference, unknown
     source).
   - `Ledger.check_composition(primitives) -> CompositionVerdict`: LEGAL
     iff EVERY primitive resolves to the verified side. Total predicate:
     one unverified primitive fails the whole composition (no partial
     credit, no silent dropping; the exclusion is recorded in `absent`).
   - Named gaps: unverified demand registers through the REAL
     `GapRegistry.register` — `GapRecord` with `TechniqueBlock`
     (objective: establish a verification event for the primitive; note
     names the obstruction and why the ledger cannot cross it) and
     `observation` evidence (the actual failed check). Repeat checks reuse
     the already-open gap (marker `[creativity-ledger] unverified primitive
     '<id>'`) — no registry spam. Proof asserts the gap record exists via
     `list_gaps()` afterward.
   - Quarantine: any path consulting a quarantined entry raises
     `QuarantinedSubstrateRefused` — at add time and at check time, never
     a verdict, never silent.
   - No authority: the ledger exposes no admit/verify/override method;
     `__getattr__` raises `LedgerRefused` with the exact reason for the
     denylisted names, and the battery asserts the class surface leaks
     nothing.
   - No cached trust: every check re-resolves against the live stores.
3. **`runtime/creativity/__init__.py`** updated (explicit pathspec; package
   skeleton ownership): ledger exports added; docstring updated — the §3
   ledger moved from "NOT built" to built, and the stale "dual-executive
   build hold remains in force" line corrected (hold lifted 2026-10-01).
4. **Proofs** at `proofs/creativity_ledger1/` (`proof_ledger.py`,
   `gate_run.sh`, one command, exit 0): 23/23 green, twice, in fresh
   sequential processes. Coverage: all-verified LEGAL; one-unverified
   ILLEGAL with the element named; named gap in the real registry with the
   obstruction + visible absence; gap dedup; quarantine-at-add raises;
   quarantine-at-check raises (disclosed sqlite flip — the store offers no
   unverify API; exercises the ledger's defensive path); deleted
   verification event → ILLEGAL "no longer resolvable" (disclosed sqlite
   delete — the store offers no delete API; proves re-resolution, not index
   trust); trust dropped → ILLEGAL naming the trust level; trust restored
   → LEGAL again; verify/admit attempts refused with the exact reason.
5. **Ran green twice in own runs**; will run once more at the commit HEAD
   after committing (report-don't-land).

## What was NOT built (deliberately)

- No admit/verify/override capability (refused by design — the ledger is
  read-side; verification authority stays with the gate and the stores).
- No composition search, no novelty check (§4 proposed), no critique
  machinery (§6), no peer arbitration (§5), no executive, no run
  controller, no budgets — later missions per TRACK_PLAN.md.
- No gallery admission contract (CREATIVITY-RELEASE-1's interface).
- No dispatch work of any kind (standing exclusion).
- No changes to any read-only subsystem: evidence.py, provenance.py,
  gaps.py, acceptance_panels.py, curiosity/*, core/*, media/* untouched.
  Root `gate_run.sh` dispatcher untouched.
- Protected trees (`remor_oracle_binding`, `remor_agent_org`,
  `remor_verdict_binding`, `remor_trust_anchor`) never written.

## Judgment calls (open for James to overrule)

1. **Citation discipline**: a verification event must name the primitive it
   verifies (primitive_id in the evidence entry's text or source). This
   defeats citing an unrelated verified entry as proof, but it imposes a
   convention on how gate-crossing evidence entries are written.
2. **Provenance threshold**: provenance-sourced primitives count as
   verified only at TRUSTED or above (the store's own promotion bar: 10
   uses, 0.95 success rate). TESTED-and-below is not substrate.
3. **Gap shape**: unverified demand registers as a `TechniqueBlock` gap
   ("establish a verification event for primitive X"). Alternative shapes
   (LimitationBlock, DataBlock) were considered; TechniqueBlock names the
   missing verified technique most directly.
4. **Device-proof carried, not judged**: `device_proof` does not affect the
   legality verdict at this phase; RELEASE-1 defines admission criteria.
5. **`engine=None` for the bench GapRegistry**: honest on the
   observation-evidence path (verified by reading `register()`); the real
   engine passes through in production phases.

## Incidents

None. No protected-tree touches, no mid-run failures, no defects found in
other subsystems, no repairs to foreign code. The two sqlite flips in the
battery are disclosed test-harness drives of the ledger's defensive paths,
not repairs.

## Exact next boundary

The ledger is built and REPORTED. The next boundary is the gate: Felix's
independent re-run of `proofs/creativity_ledger1/gate_run.sh` in main chat,
then landing to canonical. After landing, the track's next mission is
CREATIVITY-CRITIQUE-1 (novelty by mechanism, value vs intent, reusing the
Acceptance panels) — its packet must note the ledger's frozen read-side
interface: `Ledger`, `LedgerEntry`, `CompositionVerdict`, `LedgerRefused`,
`check_composition`, `add_entry`.

## What remains unproven

- The gate's independent re-run (only the gate makes a crossing).
- Landing to canonical (report-don't-land; landing happens in main chat).
- End-to-end use: no creative composition has yet flowed through the
  ledger against the REAL production stores (that is INTEGRATE-1's
  boundary, Phase 2).
- The citation-discipline convention has no producer yet: nothing in the
  tree currently writes gate-crossing evidence entries that name
  primitives. The first real LedgerEntry population (EXEC-1) must either
  adopt the convention or record the deviation.
- Device-proof status is carried but unpopulated by any real device
  confirmation path (James's hardware acceptance is the only real source).
