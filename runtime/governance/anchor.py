"""External trust anchor for REMOR's tamper-evident stores.

Problem
-------
Hash chains inside the protected SQLite databases are internal consistency
relative to their current root, not a root of trust: a whole-DB rewrite
with recomputed chains is indistinguishable from inside. This module keeps
a signed, append-only journal of chain heads OUTSIDE the database it
protects, so a rewritten database cannot agree with the journal without
the anchor key.

Threat model (see ANCHOR_DESIGN.md)
----------------------------------
- Case A (DB only): DETECTED -- live heads differ from anchored heads and
  the attacker cannot forge a journal signature.
- Case B (journal only): DETECTED -- signature / chain break, or truncation
  breaks the strictly increasing seq.
- Case C (key only): fail-closed -- forged anchor heads never equal the
  live DB heads, so verify() refuses.
- Case D (DB + journal + key): UNDETECTED. Honest residual bound: the
  mechanism raises the bar to "compromise three independent stores
  consistently". Documented, not hand-waved.
- Case E (legitimate migration): audited transition(); a silent swap is
  refused.

Trust-on-first-use is GONE (F1-F3 repair, 2026-09-25): genesis is
created exactly once, explicitly, via initialize() (exposed as
`anchor_admin.py init`); it is never created implicitly. anchor()
raises AnchorMissing when no journal exists, and
ReviewBoard._store_verdict refuses to write without one, so truncation
to genesis is detectable whenever post-init writes exist: the genesis
commits the init-time heads, which a later database can no longer
match. The residual is a full rollback to the exact init-time state
(journal truncated to genesis AND the database rewound to the init-time
heads with recomputed chains) -- indistinguishable from "initialized
but never written", and only mitigated by OS-level append-only
(chattr +a, recorded in append_only_enforced).

Authority model: the journal records the *producer* authority behind each
anchored write (e.g. "remor:engine" for verification verdicts). The anchor
does not invent an agent hierarchy: agent identities remain the producers'
own claims, recorded in the tamper-evident logs as before.

Deliberate omissions
--------------------
- There is NO rewrite / delete / truncate API. The only writers are
  anchor(), transition(), rotate_key() and initialize(), all append-only.
- verify() never raises on corrupt input: it returns (False, reason) and
  the caller fails closed.
- No hard-coded roots: genesis is created explicitly at deployment
  (initialize / `anchor_admin.py init`), never embedded, never implicit.

Stdlib only: hashlib, hmac, json, os, secrets, subprocess, time.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

GENESIS_DIGEST = "GENESIS"

#: Reasons a journal record may carry.
REASONS = frozenset({
    "genesis", "verdict", "transition",
    "migration", "rollback", "rotation", "recovery",
})

#: Reasons allowed for transition(): audited head changes without a
#: preceding anchored write (migration / rollback / rotation / recovery).
#: This is the ONLY legitimate path for heads to change that way -- an
#: honest rollback is distinguishable from malicious rewriting because
#: the transition itself is in the journal.
TRANSITION_REASONS = frozenset({"migration", "rollback", "rotation", "recovery"})

_KEY_BYTES = 32
_KEY_ID_LEN = 16

# Fields every journal record must carry (besides the always-present
# signature / record_digest computed over the rest).
_REQUIRED_FIELDS = (
    "seq", "scope_heads", "prev_anchor_digest", "ts", "reason",
    "authority", "key_id", "signature", "record_digest",
)


class AnchorError(Exception):
    """Base class for trust-anchor failures."""


class AnchorConfigError(AnchorError):
    """Anchor/key/journal placement is unsafe (database<->root<->database)."""


class AnchorMismatch(AnchorError):
    """Live chain heads do not match the anchored heads. Fail closed."""


class AnchorMissing(AnchorError):
    """No anchor journal exists where the trust anchor requires one.

    Fail closed: genesis is explicit-only (initialize()), so a missing
    journal can never be silently recreated over a live database."""


def _canonical(obj: Any) -> str:
    """Deterministic JSON encoding shared by signing and verification."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:_KEY_ID_LEN]


def _is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


def _refuse_db_root_db(journal_path: str, key_path: str, db_path: str) -> None:
    """Refuse database->root->database nesting.

    The anchor must live outside the mutable database it protects: the
    resolved journal/key paths must not sit inside the resolved database
    parent directory, and the database must not sit inside the anchor
    directory. Raises AnchorConfigError.
    """
    db_real = os.path.realpath(db_path)
    db_parent = os.path.dirname(db_real)
    for label, candidate in (("journal", journal_path), ("key", key_path)):
        cand_real = os.path.realpath(candidate)
        cand_dir = os.path.dirname(cand_real)
        if cand_real == db_real:
            raise AnchorConfigError(
                f"anchor {label} path is the database file itself: "
                f"{candidate!r}")
        if _is_within(cand_real, db_parent):
            raise AnchorConfigError(
                f"anchor {label} path {candidate!r} is inside the database "
                f"directory {db_parent!r}: the anchor must live outside "
                f"the database it protects")
        if _is_within(db_real, cand_dir):
            raise AnchorConfigError(
                f"database path {db_path!r} is inside the anchor {label} "
                f"directory {cand_dir!r}: the anchor must live outside "
                f"the database it protects")


def _tighten(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # best effort; creation modes already restrict


def _write_key_file(path: str, key: bytes) -> None:
    """Create a key file atomically-ish: O_CREAT|O_EXCL, mode 0600."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    _tighten(path, 0o600)


def _check_heads(heads: Any) -> Dict[str, str]:
    if not isinstance(heads, dict):
        raise AnchorError("heads must be a dict of scope -> head digest")
    clean: Dict[str, str] = {}
    for scope, digest_value in heads.items():
        if not isinstance(scope, str) or not isinstance(digest_value, str):
            raise AnchorError("heads must map str scope -> str digest")
        clean[scope] = digest_value
    return clean


def default_anchor_paths(db_path: str) -> Tuple[str, str, str]:
    """Default (journal_path, key_path, db_path) for a database.

    The anchor store lives OUTSIDE the database directory, as a sibling:
    <db_parent>/../anchor_store/<dbname>.anchor.journal (and .key).
    """
    db_abs = os.path.abspath(db_path)
    db_parent = os.path.dirname(db_abs)
    stem = os.path.splitext(os.path.basename(db_abs))[0] or "db"
    anchor_dir = os.path.normpath(os.path.join(db_parent, "..", "anchor_store"))
    return (
        os.path.join(anchor_dir, f"{stem}.anchor.journal"),
        os.path.join(anchor_dir, f"{stem}.anchor.key"),
        db_abs,
    )


# Chained tables deliberately EXCLUDED from the anchored scopes.
EXCLUDED_SCOPES = frozenset({
    # oracle:ob_producer_attestations -- per-process engine-token
    # re-attestations. The registry appends one row per process BY DESIGN
    # (the engine token is memory-only and rotates every process), so
    # exact cross-process head equality is unachievable by construction:
    # anchoring this table would fail closed on every legitimate restart.
    # Its tamper-evidence still holds via the internal chain audit, and
    # forging its rows grants no authority without the memory-only token
    # itself (authentication never reads authority from the DB alone).
    "oracle:ob_producer_attestations",
})


def collect_anchor_heads(org_store: Any, oregistry: Any) -> Dict[str, str]:
    """Heads covered by the anchor, enumerated from the audit surfaces.

    Every chained table in OrgStore (scope "org:<table>") plus every
    chained table in the OracleRegistry (scope "oracle:<table>"). The
    tables come from each class's audit_all() keys; the digests from each
    class's public head_digest(). Tables in EXCLUDED_SCOPES are skipped
    (documented above).
    """
    heads: Dict[str, str] = {}
    for table in org_store.audit_all().keys():
        scope = f"org:{table}"
        if scope in EXCLUDED_SCOPES:
            continue
        heads[scope] = org_store.head_digest(table)
    for table in oregistry.audit_all().keys():
        scope = f"oracle:{table}"
        if scope in EXCLUDED_SCOPES:
            continue
        heads[scope] = oregistry.head_digest(table)
    return heads


class AnchorStore:
    """Signed append-only journal of external chain heads.

    Journal format: JSONL, one canonical-JSON record per line. Record::

        seq, scope_heads {scope: head_digest}, prev_anchor_digest, ts,
        reason, authority, key_id, [new_key_id], signature, record_digest

    signature      = HMAC-SHA256(key, canonical(record sans signature
                   and sans record_digest))
    record_digest  = SHA256(canonical(record with signature))
    prev_anchor_digest of record N = record_digest of record N-1
                   ("GENESIS" for seq 1).
    """

    def __init__(self, journal_path: str, key_path: str, db_path: str):
        _refuse_db_root_db(journal_path, key_path, db_path)
        self.journal_path = os.path.abspath(journal_path)
        self.key_path = os.path.abspath(key_path)
        self.db_path = os.path.abspath(db_path)

        journal_dir = os.path.dirname(self.journal_path)
        os.makedirs(journal_dir, mode=0o700, exist_ok=True)
        _tighten(journal_dir, 0o700)

        # Key file: created once, 0600, O_CREAT|O_EXCL. Never overwritten
        # here; rotation writes key.<new_id> siblings instead.
        if not os.path.exists(self.key_path):
            _write_key_file(self.key_path, secrets.token_bytes(_KEY_BYTES))
        else:
            _tighten(self.key_path, 0o600)
        self._keys: Dict[str, bytes] = {}
        self._load_keys()
        # Durability contract with rotate_key(): the PRIMARY key file
        # always holds the CURRENT key (rotation atomically replaces it),
        # while key.<id> siblings are retained archives. A fresh process
        # therefore picks up post-rotation keys with no extra state.
        with open(self.key_path, "rb") as fh:
            self._current_key_id = _key_id(fh.read())
        if self._current_key_id not in self._keys:  # pragma: no cover
            raise AnchorError("anchor key file unreadable after creation")

        # The journal file itself is created lazily on the first append so
        # that "no journal yet" stays distinguishable from "empty journal"
        # (trust-on-first-use at boot).
        self.append_only_enforced = False
        self._refresh_tip()

    # -- key management -------------------------------------------------
    def _load_keys(self) -> None:
        """Load the current key plus every retained rotated key.

        Rotated keys live as key.<key_id> siblings of the key file and are
        kept so historical journal signatures still verify.
        """
        keys: Dict[str, bytes] = {}
        try:
            with open(self.key_path, "rb") as fh:
                data = fh.read()
        except OSError:
            data = b""
        if data:
            keys[_key_id(data)] = data
        key_dir = os.path.dirname(self.key_path)
        try:
            names = os.listdir(key_dir)
        except OSError:
            names = []
        for name in names:
            if not name.startswith("key.") or len(name) <= len("key."):
                continue
            kid = name[len("key."):]
            try:
                with open(os.path.join(key_dir, name), "rb") as fh:
                    key_data = fh.read()
            except OSError:
                continue
            if key_data:
                keys[kid] = key_data
        self._keys = keys

    # -- journal IO -----------------------------------------------------
    def journal_exists(self) -> bool:
        return os.path.exists(self.journal_path)

    def _parse_journal(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with open(self.journal_path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    raise AnchorError(
                        f"journal corrupt: blank line {lineno}")
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AnchorError(
                        f"journal corrupt: line {lineno} is not JSON "
                        f"({exc})")
                if not isinstance(rec, dict):
                    raise AnchorError(
                        f"journal corrupt: line {lineno} is not an object")
                records.append(rec)
        return records

    def _refresh_tip(self) -> None:
        """Recompute next seq / tip digest / tip heads from the journal.

        Fail-closed on corruption. Called at init and before every append
        so an externally appended record is picked up rather than
        overwritten with a colliding seq.
        """
        if not self.journal_exists():
            self._next_seq = 1
            self._tip_digest = GENESIS_DIGEST
            self._tip_heads: Dict[str, str] = {}
            return
        records = self._parse_journal()
        for want, rec in enumerate(records, 1):
            if rec.get("seq") != want:
                raise AnchorError(
                    f"journal corrupt: expected seq {want}, found "
                    f"{rec.get('seq')!r}")
        self._next_seq = len(records) + 1
        if records:
            self._tip_digest = str(records[-1].get("record_digest", ""))
            heads = records[-1].get("scope_heads")
            self._tip_heads = dict(heads) if isinstance(heads, dict) else {}
        else:
            self._tip_digest = GENESIS_DIGEST
            self._tip_heads = {}

    @property
    def record_count(self) -> int:
        return self._next_seq - 1

    @property
    def current_key_id(self) -> str:
        return self._current_key_id

    def latest_heads(self) -> Dict[str, str]:
        """Scope heads committed by the latest journal record."""
        return dict(self._tip_heads)

    def _ensure_journal(self) -> None:
        journal_dir = os.path.dirname(self.journal_path)
        os.makedirs(journal_dir, mode=0o700, exist_ok=True)
        _tighten(journal_dir, 0o700)
        if not os.path.exists(self.journal_path):
            fd = os.open(self.journal_path,
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            os.close(fd)
            _tighten(self.journal_path, 0o600)
        if not self.append_only_enforced:
            # Best effort: an append-only journal resists silent truncation
            # even by the file owner. Records whether it applied.
            try:
                proc = subprocess.run(
                    ["chattr", "+a", self.journal_path],
                    capture_output=True, timeout=10)
                self.append_only_enforced = (proc.returncode == 0)
            except (OSError, subprocess.SubprocessError):
                self.append_only_enforced = False

    def _append_record(self, heads: Dict[str, str], reason: str,
                       authority: Optional[str],
                       key_id: Optional[str] = None,
                       extra: Optional[Dict[str, Any]] = None) -> str:
        """Sign and append one record. Returns its record_digest."""
        self._refresh_tip()
        self._ensure_journal()
        kid = key_id or self._current_key_id
        key = self._keys.get(kid)
        if key is None:
            raise AnchorError(f"no signing key for key_id {kid!r}")
        body: Dict[str, Any] = {
            "seq": self._next_seq,
            "scope_heads": {k: heads[k] for k in sorted(heads)},
            "prev_anchor_digest": self._tip_digest,
            "ts": _utcnow(),
            "reason": reason,
            "authority": authority,
            "key_id": kid,
        }
        if extra:
            body.update(extra)
        body["signature"] = hmac.new(
            key, _canonical(body).encode("utf-8"),
            hashlib.sha256).hexdigest()
        record = dict(body)
        record["record_digest"] = hashlib.sha256(
            _canonical(body).encode("utf-8")).hexdigest()
        line = (_canonical(record) + "\n").encode("utf-8")
        fd = os.open(self.journal_path,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._next_seq += 1
        self._tip_digest = record["record_digest"]
        self._tip_heads = dict(heads)
        return record["record_digest"]

    # -- writes ---------------------------------------------------------
    def initialize(self, heads: Dict[str, str], authority: str) -> str:
        """Explicit deployment-time genesis (first-use capture).

        Writes exactly one genesis record committing the current heads.
        Refuses if the journal already has records. This is the ONLY
        genesis path: there is no lazy/implicit genesis anywhere.
        """
        if not isinstance(authority, str) or not authority.strip():
            raise AnchorError(
                "initialize requires a non-empty authority (operator id)")
        clean = _check_heads(heads)
        self._refresh_tip()
        if self._next_seq != 1:
            raise AnchorError(
                "initialize refused: journal already has records")
        return self._append_record(clean, "genesis", authority.strip())

    def anchor(self, heads: Dict[str, str], reason: str = "verdict",
               authority: Optional[str] = None) -> str:
        """Append one signed, chained record committing `heads`.

        Fail-closed genesis rule: there is NO lazy genesis. If no
        journal exists (or it holds no records yet), this raises
        AnchorMissing -- the operator must create the genesis
        explicitly via initialize() (`anchor_admin.py init`). reason
        "genesis" is refused here for the same reason: initialize() is
        the ONLY genesis path. This closes the truncation-to-genesis
        and journal-deletion capture holes: a journal can never be
        silently (re)created over an attacker's database.
        """
        if reason not in REASONS:
            raise AnchorError(f"unknown anchor reason {reason!r}")
        if reason == "genesis":
            raise AnchorError(
                "anchor: the genesis record is created only by "
                "initialize()")
        clean = _check_heads(heads)
        self._refresh_tip()
        if not self.journal_exists() or self._next_seq == 1:
            raise AnchorMissing(
                "anchor refused: no anchor journal exists -- run "
                "`anchor_admin.py init` to create the genesis record "
                "explicitly before any anchored write")
        return self._append_record(clean, reason, authority)

    def transition(self, new_heads: Dict[str, str], reason: str,
                   authority: str) -> str:
        """Audited head change for migration/rollback/rotation/recovery.

        This is the ONLY legitimate path for anchored heads to change
        without a preceding anchored write: the transition itself is a
        signed journal record, so an honest rollback is distinguishable
        from malicious rewriting. A database restored from backup without
        a matching transition record fails verification.
        """
        if reason not in TRANSITION_REASONS:
            raise AnchorError(
                f"transition reason must be one of "
                f"{sorted(TRANSITION_REASONS)}, got {reason!r}")
        if not isinstance(authority, str) or not authority.strip():
            raise AnchorError(
                "transition requires a non-empty authority string")
        clean = _check_heads(new_heads)
        self._refresh_tip()
        if not self.journal_exists() or self._next_seq == 1:
            # No lazy genesis: a transition is an audited head change on
            # an INITIALIZED anchor, never genesis creation. The operator
            # runs `anchor_admin.py init` first.
            raise AnchorMissing(
                "transition refused: no anchor journal exists -- run "
                "`anchor_admin.py init` to create the genesis record "
                "explicitly before any audited transition")
        return self._append_record(clean, reason, authority.strip())

    def rotate_key(self, authority: Optional[str] = None) -> str:
        """Rotate the signing key, durably.

        Stages key.<new_id> (0600, O_CREAT|O_EXCL), appends a "rotation"
        record signed by the OLD key naming the new key_id, archives the
        old key bytes as key.<old_id>, then ATOMICALLY promotes the new
        key to the primary key file (os.replace). A fresh process
        therefore resolves the NEW key id as current from the primary
        file, while every key.<id> sibling stays a known key so
        pre-rotation records still verify. The old key is genuinely
        retired from active signing use. Returns the new key id.
        """
        self._refresh_tip()
        key_dir = os.path.dirname(self.key_path)
        os.makedirs(key_dir, mode=0o700, exist_ok=True)
        old_id = self._current_key_id
        old_key = self._keys.get(old_id)
        if not old_key:
            raise AnchorError(
                "key rotation failed: current key bytes unavailable")
        new_key = b""
        new_id = ""
        new_path = ""
        for _ in range(4):
            candidate = secrets.token_bytes(_KEY_BYTES)
            candidate_id = _key_id(candidate)
            candidate_path = os.path.join(key_dir, f"key.{candidate_id}")
            try:
                _write_key_file(candidate_path, candidate)
            except FileExistsError:
                continue
            new_key, new_id, new_path = candidate, candidate_id, candidate_path
            break
        if not new_key:  # pragma: no cover - 128-bit id collision
            raise AnchorError("key rotation failed: key id collision")
        # The rotation record is signed by the OLD key and names the new
        # one, so the switch itself is auditable in the journal.
        self._append_record(
            dict(self._tip_heads), "rotation", authority,
            key_id=old_id, extra={"new_key_id": new_id})
        # Archive the old key BEFORE the switch: no crash window may lose
        # the bytes needed to verify pre-rotation journal records.
        archive_path = os.path.join(key_dir, f"key.{old_id}")
        try:
            _write_key_file(archive_path, old_key)
        except FileExistsError:
            with open(archive_path, "rb") as fh:
                if fh.read() != old_key:
                    raise AnchorError(
                        "key rotation failed: key archive collision for "
                        f"{old_id!r}")
        # Durable switch: the primary key file now holds the new key, so
        # __init__ in ANY process resolves the new id as current.
        os.replace(new_path, self.key_path)
        _tighten(self.key_path, 0o600)
        self._keys[new_id] = new_key
        self._current_key_id = new_id
        return new_id

    # -- verification ---------------------------------------------------
    def verify(self, heads: Dict[str, str]) -> Tuple[bool, str]:
        """Verify the journal against live `heads`.

        Checks, in order: the journal exists and parses; seq is strictly
        increasing from 1; the anchor chain digests are intact; every
        signature verifies under a known key id (constant-time compare);
        the latest record's scope_heads equal the supplied live heads.

        Never raises on corrupt input: any failure returns
        (False, reason) and the caller fails closed.
        """
        try:
            return self._verify_inner(heads)
        except Exception as exc:  # fail-closed on anything unexpected
            return False, f"anchor verify failed: {type(exc).__name__}: {exc}"

    def _verify_inner(self, heads: Dict[str, str]) -> Tuple[bool, str]:
        if not self.journal_exists():
            return False, (
                f"anchor journal not found: {self.journal_path}")
        try:
            records = self._parse_journal()
        except AnchorError as exc:
            return False, str(exc)
        if not records:
            return False, "anchor journal is empty (no genesis record)"
        # A rotation may have happened in another process; reload keys
        # defensively (failure here only makes verification stricter).
        try:
            self._load_keys()
        except Exception:
            pass
        total = len(records)
        for want, rec in enumerate(records, 1):
            if rec.get("seq") != want:
                return False, (
                    f"anchor chain broken: expected seq {want}, found "
                    f"{rec.get('seq')!r} (truncation or splice)")
        prev = GENESIS_DIGEST
        for rec in records:
            seq = rec.get("seq")
            for field in _REQUIRED_FIELDS:
                if field not in rec:
                    return False, (
                        f"seq {seq}: journal record missing field {field!r}")
            if not hmac.compare_digest(str(rec["prev_anchor_digest"]), prev):
                return False, (
                    f"seq {seq}: prev_anchor_digest mismatch (chain break)")
            body = {k: v for k, v in rec.items() if k != "record_digest"}
            expect_digest = hashlib.sha256(
                _canonical(body).encode("utf-8")).hexdigest()
            if not hmac.compare_digest(expect_digest,
                                        str(rec["record_digest"])):
                return False, (
                    f"seq {seq}: record_digest mismatch (record tampered)")
            key = self._keys.get(rec["key_id"])
            if key is None:
                return False, (
                    f"seq {seq}: unknown key_id {rec['key_id']!r} "
                    f"(key not in anchor store)")
            sig_body = {k: v for k, v in body.items() if k != "signature"}
            expect_sig = hmac.new(
                key, _canonical(sig_body).encode("utf-8"),
                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expect_sig, str(rec["signature"])):
                return False, f"seq {seq}: signature invalid (forgery)"
            if rec.get("reason") not in REASONS:
                return False, (
                    f"seq {seq}: unknown reason {rec.get('reason')!r}")
            prev = str(rec["record_digest"])
        live = heads if isinstance(heads, dict) else None
        if live is None or any(not isinstance(k, str)
                               or not isinstance(v, str)
                               for k, v in live.items()):
            return False, "verify: heads must be a dict of str -> str"
        anchored = records[-1].get("scope_heads")
        if not isinstance(anchored, dict) or anchored != live:
            diff = sorted({s for s in set(
                (anchored if isinstance(anchored, dict) else {})) | set(live)
                if (anchored if isinstance(anchored, dict) else {}).get(s)
                != live.get(s)})[:8]
            return False, (
                f"anchor mismatch: live heads differ from anchored heads "
                f"(journal tip seq {records[-1].get('seq')}, "
                f"{len(diff)} differing scope(s): {', '.join(diff)})")
        return True, (
            f"anchor verified: {total} record(s), tip seq {total}, "
            f"{len(live)} scope(s), key {records[-1].get('key_id')}")
