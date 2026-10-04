# CUR-HARDEN-1 Boundary Notes

Genuine boundaries and named residuals. None block the mission verdict
(40/40 battery + 9/9 verifier green; P6E 19 batteries green; P6F 27/27
green unmodified on the hardened tree).

## 1. Tamper-evidence, not authenticity (decided, documented)

SHA-256 per-record hashes detect corruption; they do not authenticate
against a filesystem adversary (anyone who can rewrite the files can
recompute the hashes). This matches the proven threat (P6F: silent
corruption) and the P5C bound (same-process tampering out of scope).
If James later wants authenticity against a stronger adversary, that is a
new mechanism (key management + a trust anchor for the key), not an
extension of this one.

## 2. FRM ledger's long-lived connection and out-of-band flips

`EpochLedger` holds one persistent SQLite connection. A byte flip written
to the file out-of-band (bypassing SQLite) while that connection is open
can be masked by the connection's page cache on the next read; detection
is guaranteed on a fresh connection (new process / new instance) — the
crash-recovery and cross-process pattern the battery proves. Not a defect
in the hardening; SQLite cache behavior, documented.

## 3. Enforcement recovery is terminal-state-only

`recover_from_kill_ledger()` restores the ledger's latest terminal state
for the domain. Non-terminal episodes (e.g. a WARNING_1 with no ledger
entry) and pre-recovery `prev_state`/`reason_refs` detail are not
reconstructible — the ledger records terminal states, not history. The
reconstruction is labeled; nothing is invented. With no ledger entry,
recovery is honestly refused.

## 4. Legacy trust window

Pre-integrity rows are accepted (explicit legacy trust) until the first
write seals the store. A deployment that never writes after upgrading
keeps legacy rows unverified indefinitely. The explicit `seal_legacy()`
migration exists for operators who want the window closed immediately.

## 5. P6F T04-frm probe still says byte-level "silent"

That probe reads raw SQL, bypassing the read path by construction. The
bytes on disk are still corruptible; what the hardening guarantees is
that those bytes can never become trusted FRM state (proven in T02 via
the real read path). This is exactly James's bar: detection at the
promotion boundary, not at the byte level.
