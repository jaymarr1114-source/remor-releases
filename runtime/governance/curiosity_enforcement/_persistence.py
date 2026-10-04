"""Persistent enforcement state and the append-only kill ledger.

Writes are domain-guarded at call time: any attempt to mutate from inside the
curiosity domain raises :class:`_guard.DomainSeparationError`, even if the
caller imports this private module directly. Reads are unguarded (pull-only
exposure is allowed from anywhere).

State layout inside ``state_dir``::

    enforcement_state.json   -- {domain: {"record": {...}, "rollback_directives": [...]}}
    kill_ledger.jsonl        -- one JSON object per line, SHA-256 hash-chained

Both files survive process exit, so enforcement state survives restart and
recreation (invariant 8). The kill ledger is a separate store with a separate
writer from the evidence store, the acceptance overlay, and the GAM
attestation ledger: it lives here, only the enforcement engine appends to it,
and records are type ``KILLED`` -- never Findings.

Record-level integrity (CUR-HARDEN-1, James 2026-10-04):
  * every domain entry in ``enforcement_state.json`` carries
    ``integrity_sha256`` — SHA-256 over the canonical JSON of the entry's
    semantic content (``{"record": ..., "rollback_directives": [...]}``),
    computed on every write. TAMPER-EVIDENCE against corruption (P6F proved
    a single flipped byte in this file was read back silently), not
    authenticity against a filesystem adversary (out of scope per the P5C
    bound). Same SHA-256 pattern as the checkpoint store.
  * the file carries a top-level ``integrity_version`` marker. Reads verify
    the entry's hash IN THE READ PATH (`read_record` /
    `read_directives`): a mismatch raises EnforcementIntegrityError — the
    corrupted state is REFUSED, never returned, and the engine NEVER falls
    back to a default RUNNING (a corrupt file must not become a
    ban-evasion vector).
  * legacy entries (no hash, unsealed file): accepted with explicit legacy
    trust; the first write seals the file, after which a hash-less entry
    is REJECTED as tampered — the downgrade (strip the hash to bypass
    verification) is closed.
  * failure behavior, explicit: REJECT on read (raise); QUARANTINE via
    `quarantine_corrupt_file()` (renames the corrupt file aside, guarded);
    RECOVER via `recover_from_kill_ledger()` — reconstructs the domain's
    terminal state from the hash-chained kill ledger (the known-good
    source): the ledger's `entered_state`/`entered_at`/`issuer`/
    `reason_refs` are reused, and for BANNED_6M the decided six-month rule
    is re-applied to the ledger's `entered_at` to recompute `expires_at`
    (applying the decided rule, not inventing data). The reconstruction is
    labeled `preserved_refs["recovered_from_ledger"] = True`; `prev_state`
    is None (honest: the ledger records terminal states, not history).
    Recovery with no ledger entry for the domain raises: there is no
    known-good to recover from.
"""

from __future__ import annotations

import calendar
import datetime
import hashlib
import json
import os
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from . import _guard
from .states import (
    EnforcementState,
    KILL_LEDGER_RECORD_TYPE,
    EnforcementRecord,
    RollbackDirective,
)

_STATE_FILE = "enforcement_state.json"
_LEDGER_FILE = "kill_ledger.jsonl"
_VERSION_KEY = "integrity_version"
INTEGRITY_VERSION = 1


class EnforcementIntegrityError(Exception):
    """An enforcement state entry failed its integrity check: the file's
    bytes do not match the write-time hash, or a sealed file holds a
    hash-less entry (downgrade). The state is REFUSED — never returned, and
    never replaced by a default."""


def _entry_sha256(record: Dict, directives: List[Dict]) -> str:
    """Tamper-evidence over a domain entry's semantic content."""
    canonical = json.dumps(
        {"record": record, "rollback_directives": directives},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _add_months(ts: float, months: int) -> float:
    """Decided six-month rule, mirrored from _engine._add_months (kept
    local to avoid an import cycle: _engine imports this module)."""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day).timestamp()


def _atomic_write_json(path: str, obj: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _chain_hash(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


class StateStore:
    """Durable per-domain enforcement records and rollback directives."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._path = os.path.join(state_dir, _STATE_FILE)

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        return _read_json(self._path)

    def _sealed(self, data: dict) -> bool:
        return data.get(_VERSION_KEY) == INTEGRITY_VERSION

    def _verify_entry(self, data: dict, domain: str) -> dict:
        """Verify a domain entry's integrity BEFORE it is trusted. Returns
        the entry. Raises EnforcementIntegrityError on mismatch or on a
        hash-less entry in a sealed file (downgrade)."""
        entry = data.get(domain)
        stored = entry.get("integrity_sha256")
        if stored is None:
            if self._sealed(data):
                raise EnforcementIntegrityError(
                    f"domain {domain!r}: sealed state file holds a "
                    f"hash-less entry: treated as tampered (downgrade "
                    f"refused): the state is NOT returned")
            # Legacy trust (explicit): pre-integrity entry in an unsealed
            # file. Accepted, but NOT integrity-verified.
            return entry
        expected = _entry_sha256(entry.get("record", {}),
                                 entry.get("rollback_directives", []))
        if stored != expected:
            raise EnforcementIntegrityError(
                f"domain {domain!r}: integrity mismatch: the file's bytes "
                f"do not match the write-time hash: REFUSED — the state is "
                f"never returned and never replaced by a default")
        return entry

    def _seal_entry(self, data: dict, domain: str) -> None:
        entry = data.get(domain, {})
        entry["integrity_sha256"] = _entry_sha256(
            entry.get("record", {}), entry.get("rollback_directives", []))
        data[domain] = entry
        data[_VERSION_KEY] = INTEGRITY_VERSION

    # -- reads (unguarded: pull-only exposure; integrity-verified) --------

    def read_record(self, domain: str) -> Optional[EnforcementRecord]:
        data = self._load()
        if domain not in data:
            return None
        entry = self._verify_entry(data, domain)
        if not entry.get("record"):
            return None
        return EnforcementRecord.from_dict(entry["record"])

    def read_directives(self, domain: str) -> List[RollbackDirective]:
        data = self._load()
        if domain not in data:
            return []
        entry = self._verify_entry(data, domain)
        return [
            RollbackDirective.from_dict(d)
            for d in entry.get("rollback_directives", [])
        ]

    # -- writes (domain-guarded; every write re-seals the entry) -----------

    def write_record(self, record: EnforcementRecord) -> None:
        _guard.ensure_external_caller()
        data = self._load()
        entry = data.get(record.domain, {})
        entry["record"] = record.to_dict()
        data[record.domain] = entry
        self._seal_entry(data, record.domain)
        _atomic_write_json(self._path, data)

    def write_directives(
        self, domain: str, directives: List[RollbackDirective]
    ) -> None:
        _guard.ensure_external_caller()
        data = self._load()
        entry = data.get(domain, {})
        entry["rollback_directives"] = [d.to_dict() for d in directives]
        data[domain] = entry
        self._seal_entry(data, domain)
        _atomic_write_json(self._path, data)

    # -- failure disposition: quarantine + recovery ------------------------

    def quarantine_corrupt_file(self, reason: str) -> str:
        """Governance-plane disposition: rename the corrupt state file aside
        (it is preserved for forensics, never silently dropped). Guarded:
        the curiosity domain cannot quarantine enforcement state."""
        _guard.ensure_external_caller()
        if not os.path.exists(self._path):
            raise EnforcementIntegrityError("no state file to quarantine")
        qpath = (self._path + ".corrupt." +
                 datetime.datetime.now(
                     tz=datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        os.replace(self._path, qpath)
        # Record the quarantine reason alongside, for the audit trail.
        with open(qpath + ".reason", "w", encoding="utf-8") as fh:
            fh.write(reason + "\n")
        return qpath

    def recover_from_kill_ledger(self, ledger: "KillLedger",
                                 domain: str) -> EnforcementRecord:
        """Reconstruct a domain's enforcement state from the hash-chained
        kill ledger (the known-good source) after the state file was
        rejected or quarantined. The ledger's entered_state/entered_at/
        issuer/reason_refs are reused; for BANNED_6M the decided six-month
        rule is re-applied to the ledger's entered_at. The reconstruction
        is labeled in preserved_refs; prev_state is None (honest: the
        ledger records terminal states, not history). Raises when the
        ledger holds no terminal record for the domain: there is no
        known-good to recover from. The write is domain-guarded: recovery
        is a governance-plane action."""
        _guard.ensure_external_caller()
        entries = [r for r in ledger.records(domain)
                   if r.get("type") == KILL_LEDGER_RECORD_TYPE]
        if not entries:
            raise EnforcementIntegrityError(
                f"domain {domain!r}: recovery refused: the kill ledger "
                f"holds no terminal record for this domain: there is no "
                f"known-good state to recover from")
        latest = max(entries, key=lambda r: r["seq"])
        state = EnforcementState(latest["entered_state"])
        expires_at = None
        if state is EnforcementState.BANNED_6M:
            expires_at = _add_months(float(latest["entered_at"]), 6)
        record = EnforcementRecord(
            domain=domain,
            state=state,
            prev_state=None,
            issuer=latest["issuer"],
            reason_refs=dict(latest.get("reason_refs", {})),
            preserved_refs={"recovered_from_ledger": True,
                            "ledger_seq": latest["seq"],
                            "ledger_hash": latest["hash"]},
            entered_at=float(latest["entered_at"]),
            expires_at=expires_at,
        )
        self.write_record(record)
        return record

    def seal_legacy(self) -> int:
        """Explicit migration: hash every pre-integrity entry, seal the
        file. Returns the number of entries sealed. Guarded: migration is
        a governance-plane action."""
        _guard.ensure_external_caller()
        data = self._load()
        n = 0
        for domain, entry in list(data.items()):
            if domain == _VERSION_KEY or not isinstance(entry, dict):
                continue
            if entry.get("integrity_sha256") is None:
                n += 1
            self._seal_entry(data, domain)
        _atomic_write_json(self._path, data)
        return n


class KillLedger:
    """Append-only, hash-chained ledger of KILLED termination records."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._path = os.path.join(state_dir, _LEDGER_FILE)

    def append_killed(
        self,
        *,
        domain: str,
        entered_state: str,
        issuer: str,
        reason_refs: Dict,
        entered_at: float,
    ) -> dict:
        _guard.ensure_external_caller()
        prev_hash = "GENESIS"
        seq = 0
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh if ln.strip()]
            if lines:
                last = json.loads(lines[-1])
                prev_hash = last["hash"]
                seq = last["seq"] + 1
        body = {
            "seq": seq,
            "type": KILL_LEDGER_RECORD_TYPE,
            "domain": domain,
            "entered_state": entered_state,
            "issuer": issuer,
            "reason_refs": reason_refs,
            "entered_at": entered_at,
            "prev_hash": prev_hash,
        }
        body["hash"] = _chain_hash(body)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(body, sort_keys=True) + "\n")
        return body

    def records(self, domain: Optional[str] = None) -> List[dict]:
        if not os.path.exists(self._path):
            return []
        out = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if domain is None or rec.get("domain") == domain:
                    out.append(rec)
        return out

    def verify_chain(self) -> Tuple[bool, str]:
        """Recompute the hash chain. Detects any rewrite of history."""
        if not os.path.exists(self._path):
            return True, "empty ledger"
        expected_prev = "GENESIS"
        expected_seq = 0
        with open(self._path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("seq") != expected_seq:
                    return False, f"line {lineno}: seq break"
                if rec.get("prev_hash") != expected_prev:
                    return False, f"line {lineno}: prev_hash mismatch (history rewritten)"
                body = {k: v for k, v in rec.items() if k != "hash"}
                if _chain_hash(body) != rec.get("hash"):
                    return False, f"line {lineno}: hash mismatch (record tampered)"
                if rec.get("type") != KILL_LEDGER_RECORD_TYPE:
                    return False, f"line {lineno}: unexpected record type"
                expected_prev = rec["hash"]
                expected_seq += 1
        return True, f"chain ok ({expected_seq} records)"
