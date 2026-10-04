# CUR-HARDEN-1 Report — record-level integrity for the three silent stores

Branch `cur-harden-1` · base `aa7089c` · report-don't-land (Felix gates in main chat).

## Threat-model decision: SHA-256 tamper-evidence, not HMAC

James said "hash/MAC" — the decision, in writing: **SHA-256 per-record
hashes (tamper-evidence)**. Justification:

- The proven threat (P6F) is silent corruption: bit rot, torn writes,
  single-byte flips. What is needed is tamper-EVIDENCE — a check that
  screams when the bytes on disk stop matching what was written.
- The P5C bound already established that raw same-process SQLite
  tampering is outside the threat model: a process that can rewrite the
  DB file can also recompute any hash — or steal any HMAC key stored on
  the same host. There is no adversary in this architecture who can write
  the store files but cannot read a key file next to them; HMAC key
  management would be theater against the proven threat and adds nothing
  against the out-of-scope one.
- The checkpoint store (`runtime/core/executive/checkpoint.py`) already
  uses exactly this SHA-256 tamper-evidence pattern — the mission follows
  the established in-tree precedent (no-duplication mandate).

Stated explicitly: this is tamper-evidence against corruption, not
authenticity against a filesystem adversary.

## What changed (frozen files, all minimal + additive, James-authorized)

1. **`runtime/curiosity/evidence/store.py`** (+~200 lines):
   - New nullable column `integrity_sha256` on `curiosity_evidence`
     (ALTER TABLE for pre-existing DBs); seal marker via SQLite
     `PRAGMA user_version` (file-header integer — the fenced table
     inventory asserted by P1A is untouched).
   - `_insert()` computes the hash over the stored content fields and
     seals the store on first write (backfills legacy rows).
   - `get()` / `all()` verify every row IN THE READ PATH: mismatch raises
     `EvidenceIntegrityError`; hash-less row in a sealed store raises
     (downgrade closed); hash-less row in an unsealed store is accepted
     with explicit legacy trust.
   - `quarantine_corrupt()` (governance plane; refuses curiosity-domain
     callers) moves a failed record to a separate `<db>.quarantine` file.
   - `seal_legacy()` explicit migration.
2. **`runtime/curiosity/frm/ledger.py`** (+~180 lines): same pattern —
   `integrity_sha256` column, `PRAGMA user_version` seal, `_append()`
   hashes, `rounds_for_epoch()` / `closes_for_epoch()` / `all_records()`
   verify in the read path (`LedgerIntegrityError`), `count()` unchanged
   (returns no records), `quarantine_seq()` (governance plane).
3. **`runtime/governance/curiosity_enforcement/_persistence.py`** (+~150 lines):
   - Each domain entry carries `integrity_sha256` over the canonical JSON
     of `{"record": ..., "rollback_directives": [...]}`; top-level
     `integrity_version: 1` marker; every write re-seals.
   - `read_record()` / `read_directives()` verify BEFORE trusting: mismatch
     raises `EnforcementIntegrityError` — the engine NEVER falls back to
     an implicit RUNNING (a corrupt file must not become a ban-evasion
     vector). Missing domain still returns None (implicit RUNNING
     preserved — behavior unchanged for fresh deployments).
   - `quarantine_corrupt_file()` (guarded) renames the corrupt file aside.
   - `recover_from_kill_ledger()` reconstructs the domain's terminal state
     from the hash-chained kill ledger (known-good): ledger's
     entered_state/entered_at/issuer/reason_refs reused; for BANNED_6M the
     decided six-month rule is re-applied to the ledger's entered_at
     (applying the decided rule, not inventing data); labeled
     `preserved_refs["recovered_from_ledger"] = True`; prev_state=None
     (honest: the ledger records terminal states, not history). Raises
     when the ledger holds no terminal record — no known-good, no recovery.
   - `seal_legacy()` explicit migration (guarded).
4. **`runtime/curiosity/hardening/harden1_drill.py`** (new): curiosity-side
   drill helpers — real writer-path submission (passes the writer's fence)
   and quarantine-attempt probes (must be refused).

Out of scope honored: kill ledger and checkpoint store untouched.

## Per-mandate results

- **M1 re-map — PROVEN.** Pin verified fail-closed; all three stores'
  implementations, read/write paths, and enforcement points re-located at
  the pin. The re-map caught the P1A `tables()` exact-inventory assertion,
  which drove the `PRAGMA user_version` seal design (no new table).
- **M2 threat model — PROVEN** (this report §1).
- **M3 implementation — PROVEN.** All three stores hardened, additive,
  read-path enforced.
- **M4 read-path bar — PROVEN.** Verification lives in `get()`/`all()`,
  `rounds_for_epoch()`/`closes_for_epoch()`/`all_records()`,
  `read_record()`/`read_directives()` — the paths that promote records
  into authoritative state. T02 drives REAL byte corruption through the
  REAL read APIs in fresh processes: every P6F-SILENT case now REFUSED.
- **M5 legacy policy — PROVEN**, demonstrated per store: legacy rows
  accepted pre-seal (explicit legacy trust) → first write seals (backfill)
  → post-seal hash-stripping refused (downgrade closed).
- **M6 corruption drills — PROVEN.** T02: evidence get/all refused, FRM
  reads refused, enforcement read_state refused, engine construction
  refused (fail-closed). T05: SIGKILL battery re-run — valid records
  verify after kills (hardening didn't break crash recovery).
- **M7 regressions — PROVEN.** P6E orchestration 19 batteries green on the
  hardened tree; P6F 27/27 green UNMODIFIED — and its T04-evidence probe
  now classifies the outcome 'detected' in its own taxonomy (the
  hardening flipped silent→detected inside the battery's own categories).
  P6F T04-frm still says byte-level 'silent' — its probe reads raw SQL,
  bypassing the read path by construction; read-path refusal proven in T02.
- **M8 discipline — followed.** Gate queue clear; uncontended; niced;
  halt-on-first-fail.
- **M9/M10 gate + commit — PROVEN** (below).

## Incidents (all battery/implementation bugs, none tree defects)

- I1: `CuriosityFinding` origin enum — 'inquiry' not in
  `('PRIMARY_REQUESTED', 'CURIOUSITY_INITIATED')`; drill fixed.
- I2: `transition()` takes `EnforcementState`, not strings; battery fixed.
- I3: `seal_legacy` mutated the dict during iteration (RuntimeError);
  fixed with `list(data.items())`.
- I4: FRM T02 needle landed in the epoch_close row (SQLite file order ≠
  row order) — the test corrupted a row the read under test didn't
  return; pinned the needle to round 1's payload.
- I5: quarantine fence raised NameError — `DomainFenceError` import lost
  in an edit; restored.
- I6 (design): first seal design used a `_meta` table; P1A asserts
  `tables() == ["curiosity_evidence"]` exactly → switched both SQLite
  stores to `PRAGMA user_version` (no table-inventory side effects).
- I7 (test artifact, not implementation): FRM's long-lived connection
  served a stale page after an out-of-band byte flip — fresh-process
  reads (the battery's discipline, mirroring P6F) detect correctly.

## James's acceptance criterion

"A corrupted record must NEVER be silently promoted back into
authoritative REMOR state." — PROVEN: every read path that promotes a
record raises on mismatch; quarantine moves corrupt bytes out of
authoritative state (preserved for forensics); enforcement recovery only
from the hash-chained ledger, labeled as reconstruction; no path returns
corrupted bytes as trusted, and no path defaults to a permissive state.

## Exact next boundary

None — mission complete. The remaining hardening questions from P6F are
closed by this mission (per-record integrity is now in place). The
track's open items live with the main-chat gate: the two-part
`novel_pattern`/`discovery_novelty` decision and hardware acceptance.
