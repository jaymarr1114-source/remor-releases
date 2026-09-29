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
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from . import _guard
from .states import (
    KILL_LEDGER_RECORD_TYPE,
    EnforcementRecord,
    RollbackDirective,
)

_STATE_FILE = "enforcement_state.json"
_LEDGER_FILE = "kill_ledger.jsonl"


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

    # -- reads (unguarded: pull-only exposure) ---------------------------

    def read_record(self, domain: str) -> Optional[EnforcementRecord]:
        entry = self._load().get(domain)
        if not entry or not entry.get("record"):
            return None
        return EnforcementRecord.from_dict(entry["record"])

    def read_directives(self, domain: str) -> List[RollbackDirective]:
        entry = self._load().get(domain, {})
        return [
            RollbackDirective.from_dict(d)
            for d in entry.get("rollback_directives", [])
        ]

    # -- writes (domain-guarded) ------------------------------------------

    def write_record(self, record: EnforcementRecord) -> None:
        _guard.ensure_external_caller()
        data = self._load()
        entry = data.get(record.domain, {})
        entry["record"] = record.to_dict()
        data[record.domain] = entry
        _atomic_write_json(self._path, data)

    def write_directives(
        self, domain: str, directives: List[RollbackDirective]
    ) -> None:
        _guard.ensure_external_caller()
        data = self._load()
        entry = data.get(domain, {})
        entry["rollback_directives"] = [d.to_dict() for d in directives]
        data[domain] = entry
        _atomic_write_json(self._path, data)


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
