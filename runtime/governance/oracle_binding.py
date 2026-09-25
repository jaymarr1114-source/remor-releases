"""
swarm_engine/governance/oracle_binding.py

Oracle binding: the narrow causal fix for "REMOR enforces that checks run;
it does not enforce who supplies what counts as passing."

Every caller-supplied mechanism that can influence a pass/fail, admit/reject,
trust/untrust, or scoring decision (predicates, expected-value rules,
scoring callbacks, evidence claims, test commands, grant decisions, trust
transitions) is an ORACLE. Before an oracle's result may influence a REMOR
decision, the oracle must be:

  1. explicitly identified   (oracle_id + version, content-addressed)
  2. persistently recorded   (sqlite, immutable rows)
  3. bound to the evaluation that consumed it
         (oracle_id + version + oracle digest + input digest + result digest)
  4. tamper-evident          (hash-chained rows; mutation breaks the chain)
  5. independently attributable (generic producer identity + token auth)
  6. independently verifiable before its result influences adjudication

What this does NOT do: it does not authenticate the human or process behind
a producer token beyond the token itself (in-process callers share memory
with the engine; process-level caller authentication is the documented next
boundary). Producer identity is a self-asserted claim recorded in a
tamper-evident log. What the binding DOES guarantee, and what the adversarial
matrix proves, is:

  - a fabricated Evidence object (no bindings) cannot influence Arbiter.decide
  - an unbound predicate cannot influence AdmissionController.admit
  - a mutated oracle row is detected (chain break) and refused
  - a result from oracle B is never accepted as a result from oracle A
  - a result from a different input is never accepted for the original input
  - a stale oracle version is never accepted as the current one
  - a forged producer identity (wrong/missing token) is refused
  - an unauthorized trust transition or grant is refused
  - all of the above survive persistence and fresh-process restart

Chain model: every mutating table is append-only with per-row
``prev_digest`` / ``row_digest``. ``row_digest = sha256(canonical(all other
fields))``. Any UPDATE or DELETE breaks every later digest; ``audit()``
recomputes and reports the first break. Rows are never updated in place by
this module -- new versions are new rows.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Decision classes an oracle may be authorized for. An oracle authorized for
# one class is NOT authorized for another.
DECISION_ADMISSION_SMOKE = "admission_smoke"
DECISION_VERIFICATION = "verification"
DECISION_GRANT = "grant"               # authority to issue effect grants
DECISION_TRUST_TRANSITION = "trust:transition"
DECISION_REPAIR_CANDIDATE = "repair_candidate"

ENGINE_PRODUCER_ID = "remor:engine"
ROOT_SOURCE = "engine bootstrap"


def _canonical(obj: Any) -> str:
    """Deterministic JSON encoding for digesting."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if isinstance(o, bytes):
        return {"__bytes__": o.hex()}
    return repr(o)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _stable_code_id(code: Any) -> str:
    """Process-stable identity for a code object (F1 repair, 2026-09-25).

    ``repr(fn)`` embeds the CPython memory address, which made callable
    definition digests differ across processes for the identical callable
    (re-registration after a restart minted a spurious new version).
    This hashes only stable components: raw bytecode, co_name,
    co_names/co_varnames, and constants rendered structurally (nested code
    objects by their own stable id, never by repr), so no addresses leak in.
    """
    import hashlib
    import types as _types

    def _hash_const(h: Any, const: Any) -> None:
        if isinstance(const, _types.CodeType):
            h.update(b"code:" + bytes.fromhex(_stable_code_id(const)))
        elif isinstance(const, bytes):
            h.update(b"s:" + const)
        elif isinstance(const, str):
            h.update(b"s:" + const.encode("utf-8", "replace"))
        elif isinstance(const, (int, float, complex, bool)) or const is None:
            # repr() of these scalar types is deterministic across processes.
            h.update(b"v:" + repr(const).encode("utf-8"))
        elif isinstance(const, tuple):
            h.update(b"t:")
            for item in const:
                _hash_const(h, item)
        elif isinstance(const, frozenset):
            h.update(b"f:")
            for item in sorted(const, key=lambda x: repr(x)):
                _hash_const(h, item)
        else:
            # Unknown constant type: bind only its type name, never its repr
            # (repr may embed an address). Deliberately coarse.
            h.update(b"u:" + type(const).__name__.encode("utf-8"))

    h = hashlib.sha256()
    h.update(code.co_code)
    h.update(b"\x00" + code.co_name.encode("utf-8", "replace"))
    for _n in code.co_names:
        h.update(b"\x00n:" + _n.encode("utf-8", "replace"))
    for _n in code.co_varnames:
        h.update(b"\x00v:" + _n.encode("utf-8", "replace"))
    for _c in code.co_consts:
        _hash_const(h, _c)
    return h.hexdigest()


def _callable_definition(fn: Any) -> Dict[str, Any]:
    """Best-effort canonical definition of a callable oracle.

    Process-stable by construction: no repr() of the function object
    (it embeds the memory address). Identity = __name__ + source text
    when available + stable bytecode id as a second factor.
    """
    name = getattr(fn, "__name__", type(fn).__name__)
    try:
        source = inspect.getsource(fn)
    except (OSError, TypeError):
        source = None
    code = getattr(fn, "__code__", None)
    code_id = _stable_code_id(code) if code is not None else None
    return {"kind": "callable", "name": name,
            "source": source, "code_id": code_id,
            "module": getattr(fn, "__module__", None)}


_TABLES = {
    "ob_producers": (
        "producer_id TEXT PRIMARY KEY, producer_type TEXT NOT NULL, "
        "source TEXT, created_at TEXT NOT NULL, token_hash TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'active'"),
    "ob_oracles": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "oracle_id TEXT NOT NULL, version INTEGER NOT NULL, "
        "producer_id TEXT NOT NULL, producer_type TEXT, source TEXT, "
        "name TEXT NOT NULL, definition_digest TEXT NOT NULL, "
        "definition_text TEXT NOT NULL, input_contract TEXT, "
        "output_contract TEXT, created_at TEXT NOT NULL, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL, "
        "UNIQUE(oracle_id, version)"),
    "ob_authorizations": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "auth_id TEXT UNIQUE NOT NULL, oracle_id TEXT NOT NULL, "
        "oracle_version INTEGER NOT NULL, decision_class TEXT NOT NULL, "
        "granted_by TEXT NOT NULL, granted_at TEXT NOT NULL, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL"),
    "ob_producer_authorizations": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "auth_id TEXT UNIQUE NOT NULL, producer_id TEXT NOT NULL, "
        "decision_class TEXT NOT NULL, granted_by TEXT NOT NULL, "
        "granted_at TEXT NOT NULL, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL"),
    "ob_evaluations": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "eval_id TEXT UNIQUE NOT NULL, oracle_id TEXT NOT NULL, "
        "oracle_version INTEGER NOT NULL, oracle_digest TEXT NOT NULL, "
        "producer_id TEXT NOT NULL, supplier_id TEXT, "
        "input_digest TEXT NOT NULL, "
        "input_ref TEXT, result_digest TEXT NOT NULL, "
        "result_summary TEXT, evaluated_at TEXT NOT NULL, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL"),
    "ob_trust_transitions": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "trans_id TEXT UNIQUE NOT NULL, capability_id TEXT NOT NULL, "
        "from_state TEXT, to_state TEXT NOT NULL, producer_id TEXT NOT NULL, "
        "reason TEXT, at TEXT NOT NULL, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL"),
    "ob_grants": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        # grant_id is the logical identity; rows are EVENTS (issue/revoke)
        # ordered by seq, so it is deliberately not UNIQUE.
        "grant_id TEXT NOT NULL, effect TEXT NOT NULL, "
        "pattern TEXT NOT NULL, scope TEXT, granted_by TEXT NOT NULL, "
        "granted_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL"),
    "ob_producer_attestations": (
        "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
        "attest_id TEXT UNIQUE NOT NULL, producer_id TEXT NOT NULL, "
        "token_hash TEXT NOT NULL, attested_at TEXT NOT NULL, "
        "prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL"),
}

# Fields that participate in each table's row digest (everything except the
# digest itself and the autoincrement seq).
_DIGEST_FIELDS = {
    "ob_oracles": ("oracle_id", "version", "producer_id", "producer_type",
                   "source", "name", "definition_digest", "definition_text",
                   "input_contract", "output_contract", "created_at",
                   "prev_digest"),
    "ob_authorizations": ("auth_id", "oracle_id", "oracle_version",
                         "decision_class", "granted_by", "granted_at",
                         "prev_digest"),
    "ob_producer_authorizations": ("auth_id", "producer_id", "decision_class",
                                  "granted_by", "granted_at", "prev_digest"),
    "ob_evaluations": ("eval_id", "oracle_id", "oracle_version",
                      "oracle_digest", "producer_id", "supplier_id",
                      "input_digest",
                      "input_ref", "result_digest", "result_summary",
                      "evaluated_at", "prev_digest"),
    "ob_trust_transitions": ("trans_id", "capability_id", "from_state",
                            "to_state", "producer_id", "reason", "at",
                            "prev_digest"),
    "ob_grants": ("grant_id", "effect", "pattern", "scope", "granted_by",
                 "granted_at", "revoked", "prev_digest"),
    # Per-process token attestations. The engine's token is memory-only and
    # rotates every process; each process records its attestation as a
    # chained event so "this process holds the engine's authority" is never
    # a silent row rewrite.
    "ob_producer_attestations": ("attest_id", "producer_id", "token_hash",
                                 "attested_at", "prev_digest"),
}


@dataclass
class ProducerCredential:
    """(producer_id, token) pair. The token is shown once at registration
    and never persisted in plaintext -- only its sha256 is stored."""
    producer_id: str
    token: str


class OracleBindingError(Exception):
    """Raised when an oracle operation fails authentication, authorization,
    or integrity verification. Never silently downgraded."""


class OracleRegistry:
    """Tamper-evident registry of oracles, producers, evaluations, grants,
    and trust transitions. All mutating tables are append-only and
    hash-chained."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._migrate_legacy_grant_schema()
        self._init_tables()
        # The engine's own producer token lives in memory only. It is never
        # written to sqlite (only its hash is). A fresh process generates a
        # fresh token; the engine re-attests at boot (see engine_bootstrap).
        self._engine_token: Optional[str] = None
        self._bootstrap()

    # -- schema ---------------------------------------------------------
    def _migrate_legacy_grant_schema(self) -> None:
        """Fail-closed migration for sidecars created before the
        UNIQUE(grant_id) constraint was removed (2026-09-25).

        Old sidecars keep their grant rows as ob_grants_legacy (preserved,
        never rehydrated into authority); the new ob_grants table is
        created with the current schema. Grants from the legacy table are
        NOT trusted -- a constraint that silently forbade multiple events
        per grant means the legacy history cannot be interpreted under the
        new semantics.

        Also adds the ob_evaluations.supplier_id column to sidecars created
        before it existed. Pre-existing evaluation rows keep supplier_id
        NULL; under the new digest fields they cannot verify -- audit fails
        closed on them (quarantined, not trusted).
        """
        cur = self._conn.cursor()
        cur.execute("SELECT name, sql FROM sqlite_master "
                    "WHERE type='table' AND name IN ('ob_grants', 'ob_grants_legacy')")
        tables = {r["name"]: (r["sql"] or "") for r in cur.fetchall()}
        sql = tables.get("ob_grants", "")
        if "ob_grants" in tables and "UNIQUE" in sql.upper() and "GRANT_ID" in sql.upper():
            # Old schema: preserve, don't trust.
            cur.execute("ALTER TABLE ob_grants RENAME TO ob_grants_legacy")
            self._conn.commit()
        # If only the legacy table exists (already migrated), nothing to do.
        cur.execute("PRAGMA table_info(ob_evaluations)")
        eval_cols = {r["name"] for r in cur.fetchall()}
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        all_tables = {r["name"] for r in cur.fetchall()}
        if "ob_evaluations" in all_tables and "supplier_id" not in eval_cols:
            cur.execute("ALTER TABLE ob_evaluations ADD COLUMN supplier_id TEXT")
            self._conn.commit()

    def _init_tables(self) -> None:
        cur = self._conn.cursor()
        for table, ddl in _TABLES.items():
            cur.execute(f"CREATE TABLE IF NOT EXISTS {table} ({ddl})")
        self._conn.commit()

    # -- chaining -------------------------------------------------------
    def _head_digest(self, table: str) -> str:
        cur = self._conn.cursor()
        cur.execute(f"SELECT row_digest FROM {table} "
                    "ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        return row["row_digest"] if row else f"GENESIS:{table}"

    def head_digest(self, table: str) -> str:
        """Full-table state digest for the external trust anchor.

        Folds EVERY stored row (all columns, in seq order) into one
        digest, so any mutation of any row -- with or without chain
        recomputation -- changes the anchored head. A tip-only digest
        would miss non-tip mutations. Empty table -> "GENESIS:<table>".
        Internal chaining semantics are untouched.
        """
        if table not in _DIGEST_FIELDS:
            raise KeyError(f"unknown table {table!r}")
        cur = self._conn.cursor()
        cur.execute(f"SELECT * FROM {table} ORDER BY seq ASC")
        n = 0
        h = hashlib.sha256()
        for row in cur.fetchall():
            n += 1
            h.update(_canonical(dict(row)).encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest() if n else f"GENESIS:{table}"

    def _row_digest(self, table: str, fields: Dict[str, Any]) -> str:
        ordered = {k: fields[k] for k in _DIGEST_FIELDS[table]}
        return _digest(self._head_digest(table) + "\x00" + _canonical(ordered))

    def _insert_chained(self, table: str, fields: Dict[str, Any]) -> str:
        fields = dict(fields)
        fields["prev_digest"] = self._head_digest(table)
        fields["row_digest"] = self._row_digest(table, fields)
        cols = ", ".join(fields.keys())
        placeholders = ", ".join("?" for _ in fields)
        cur = self._conn.cursor()
        cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                    list(fields.values()))
        self._conn.commit()
        return fields["row_digest"]

    def verify_row(self, table: str, pk_col: str, pk_val: Any
                   ) -> Tuple[bool, str]:
        """Recompute one row's digest from its stored prev_digest. Catches
        post-hoc field mutation of that row (chain-link tampering needs the
        full audit_chain)."""
        if table not in _DIGEST_FIELDS:
            return False, f"unknown table {table}"
        cur = self._conn.cursor()
        cur.execute(f"SELECT * FROM {table} WHERE {pk_col}=?", (pk_val,))
        row = cur.fetchone()
        if not row:
            return False, f"no row {pk_col}={pk_val} in {table}"
        d = dict(row)
        ordered = {k: d[k] for k in _DIGEST_FIELDS[table]}
        recomputed = _digest(d["prev_digest"] + "\x00" + _canonical(ordered))
        if not hmac.compare_digest(recomputed, d["row_digest"]):
            return False, (f"{table}: row_digest mismatch for "
                            f"{pk_col}={pk_val} (row mutated)")
        return True, f"{table}: row intact"

    def audit_chain(self, table: str) -> Tuple[bool, str]:
        """Recompute every digest in table order. Returns (ok, detail)."""
        cur = self._conn.cursor()
        cur.execute(f"SELECT * FROM {table} ORDER BY seq ASC")
        prev = f"GENESIS:{table}"
        for row in cur.fetchall():
            d = dict(row)
            ordered = {k: d[k] for k in _DIGEST_FIELDS[table]}
            # prev_digest must equal the running head
            if d["prev_digest"] != prev:
                return False, (f"{table}: prev_digest break at seq={d['seq']} "
                               f"(link tampered or row deleted)")
            recomputed = _digest(prev + "\x00" + _canonical(ordered))
            if not hmac.compare_digest(recomputed, d["row_digest"]):
                return False, (f"{table}: row_digest mismatch at seq={d['seq']} "
                                f"(row mutated)")
            prev = d["row_digest"]
        return True, f"{table}: chain intact"

    def audit_all(self) -> Dict[str, Tuple[bool, str]]:
        return {t: self.audit_chain(t) for t in _DIGEST_FIELDS}

    # -- producers ------------------------------------------------------
    def register_producer(self, producer_type: str,
                          source: str = "") -> ProducerCredential:
        """Register a generic producer (human/agent/system/tool/service).
        Returns the credential; the token is shown ONCE."""
        if producer_type not in ("human", "agent", "system", "tool",
                                 "service", "external"):
            raise OracleBindingError(
                f"unknown producer_type {producer_type!r}")
        token = secrets.token_hex(32)
        producer_id = f"prod_{_digest(token)[:16]}"
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO ob_producers (producer_id, producer_type, source, "
            "created_at, token_hash, status) VALUES (?,?,?,?,?, 'active')",
            (producer_id, producer_type, source, _now(), _digest(token)))
        # Chained registration attestation: binds (producer_id, token_hash)
        # into the tamper-evident log. authenticate() re-checks the row
        # against the attestation chain, so a direct sqlite mutation of
        # ob_producers.token_hash is detected (fail closed). Honest limit:
        # this is tamper-EVIDENT against naive row mutation, not tamper-proof
        # against an attacker who rewrites the whole database and recomputes
        # the chain -- see the mission report.
        self._insert_chained("ob_producer_attestations", {
            "attest_id": f"reg_{_digest(token)[:16]}",
            "producer_id": producer_id,
            "token_hash": _digest(token),
            "attested_at": _now()})
        self._conn.commit()
        return ProducerCredential(producer_id, token)

    def authenticate(self, producer_id: str, token: str) -> bool:
        # The engine's token is memory-only and rotates every process: it
        # authenticates by direct memory comparison, never against sqlite.
        # The per-process attestation is ALSO recorded as a chained event
        # (ob_producer_attestations) so a fresh process's authority is
        # visible in the log, not a silent row rewrite.
        if producer_id == ENGINE_PRODUCER_ID:
            return bool(self._engine_token) and hmac.compare_digest(
                token, self._engine_token)
        cur = self._conn.cursor()
        cur.execute("SELECT token_hash, status FROM ob_producers "
                    "WHERE producer_id = ?", (producer_id,))
        row = cur.fetchone()
        if not row or row["status"] != "active":
            return False
        if not hmac.compare_digest(_digest(token), row["token_hash"]):
            return False
        # Bind the persisted row against the chained registration
        # attestation: a direct sqlite mutation of ob_producers.token_hash
        # is detected here (the row passes but the chain disagrees).
        cur.execute("SELECT token_hash FROM ob_producer_attestations "
                    "WHERE producer_id=? ORDER BY seq DESC LIMIT 1",
                    (producer_id,))
        att = cur.fetchone()
        if att is not None and not hmac.compare_digest(
                _digest(token), att["token_hash"]):
            return False
        return True

    def _require_auth(self, producer_id: str, token: str) -> None:
        if not self.authenticate(producer_id, token):
            raise OracleBindingError(
                f"producer {producer_id!r}: authentication failed "
                f"(forged or missing identity refused)")

    # -- bootstrap ------------------------------------------------------
    def _bootstrap(self) -> None:
        """Create the engine producer and its root authorizations on a fresh
        DB. The engine token is memory-only; on restart the engine calls
        engine_bootstrap() to re-attest (recorded in the chain)."""
        cur = self._conn.cursor()
        cur.execute("SELECT producer_id FROM ob_producers WHERE producer_id=?",
                    (ENGINE_PRODUCER_ID,))
        fresh = cur.fetchone() is None
        self._engine_token = secrets.token_hex(32)
        if fresh:
            cur.execute(
                "INSERT INTO ob_producers (producer_id, producer_type, "
                "source, created_at, token_hash, status) "
                "VALUES (?, 'system', ?, ?, ?, 'active')",
                (ENGINE_PRODUCER_ID, ROOT_SOURCE, _now(),
                 _digest(self._engine_token)))
            self._conn.commit()
            # Root authorizations: the engine's own instrument may authorize
            # oracles for the decision classes REMOR itself decides. This is
            # the trust anchor; it is the FIRST chained content after
            # GENESIS, so any tampering with it breaks every later digest.
            for cls in (DECISION_ADMISSION_SMOKE, DECISION_VERIFICATION,
                        DECISION_GRANT, DECISION_TRUST_TRANSITION,
                        DECISION_REPAIR_CANDIDATE):
                self._insert_chained("ob_producer_authorizations", {
                    "auth_id": f"auth_root_{cls}",
                    "producer_id": ENGINE_PRODUCER_ID,
                    "decision_class": cls,
                    "granted_by": ENGINE_PRODUCER_ID,
                    "granted_at": _now()})
            # Genesis attestation for the first engine token.
            self._insert_chained("ob_producer_attestations", {
                "attest_id": f"att_{_digest(self._engine_token)[:16]}",
                "producer_id": ENGINE_PRODUCER_ID,
                "token_hash": _digest(self._engine_token),
                "attested_at": _now()})
        else:
            # Existing DB: this process holds a fresh engine token. Record
            # the re-attestation as a chained event -- the authority of a
            # fresh process is visible in the log. The ob_producers row is
            # NEVER updated: engine authentication is memory-only, so there
            # is no silent trust reconstruction via row rewrite.
            self._insert_chained("ob_producer_attestations", {
                "attest_id": f"att_{_digest(self._engine_token)[:16]}",
                "producer_id": ENGINE_PRODUCER_ID,
                "token_hash": _digest(self._engine_token),
                "attested_at": _now()})

    def engine_handle(self) -> "EngineOracleHandle":
        if not self._engine_token:
            raise OracleBindingError("registry not bootstrapped")
        return EngineOracleHandle(self, ENGINE_PRODUCER_ID,
                                  self._engine_token)

    def producer_authorized(self, producer_id: str,
                            decision_class: str) -> bool:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM ob_producer_authorizations "
            "WHERE producer_id=? AND decision_class=?",
            (producer_id, decision_class))
        return cur.fetchone() is not None

    # -- oracles --------------------------------------------------------
    @staticmethod
    def _definition_text(definition: Any) -> Tuple[str, str]:
        """-> (definition_digest, definition_text). Callables are reduced to
        their source (or repr fallback); data is canonical JSON."""
        if callable(definition):
            defn = _callable_definition(definition)
        elif isinstance(definition, dict) and "kind" in definition:
            defn = definition
        else:
            defn = {"kind": "data", "value": definition}
        text = _canonical(defn)
        return _digest(text), text

    def register_oracle(self, producer_id: str, token: str, name: str,
                        definition: Any, input_contract: str = "",
                        output_contract: str = "",
                        source: str = "") -> Tuple[str, int]:
        """Register an oracle definition. Returns (oracle_id, version).

        Idempotent for an identical (producer, name, definition): the head
        version is returned without a new row, so repeated registration of
        the same oracle does not churn versions (this is what makes
        head-version staleness checks usable within a single run). A
        CHANGED definition under the same name is a new version (append-
        only); old evaluations stay bound to their version.
        """
        self._require_auth(producer_id, token)
        if not name:
            raise OracleBindingError("oracle name is required")
        oracle_id = "orc_" + _digest(producer_id + "\x00" + name)[:20]
        definition_digest, definition_text = self._definition_text(definition)
        head = self.oracle_head(oracle_id)
        if head is not None:
            # A tampered head must fail closed, never be silently reused.
            ok, why = self.verify_oracle_row(oracle_id, head["version"])
            if not ok:
                raise OracleBindingError(
                    f"register_oracle refused: {why}")
            if hmac.compare_digest(head["definition_digest"],
                                   definition_digest):
                return oracle_id, head["version"]
        cur = self._conn.cursor()
        cur.execute("SELECT producer_type FROM ob_producers WHERE producer_id=?",
                    (producer_id,))
        prow = cur.fetchone()
        version = (head["version"] if head else 0) + 1
        self._insert_chained("ob_oracles", {
            "oracle_id": oracle_id, "version": version,
            "producer_id": producer_id,
            "producer_type": prow["producer_type"] if prow else "external",
            "source": source, "name": name,
            "definition_digest": definition_digest,
            "definition_text": definition_text,
            "input_contract": input_contract, "output_contract": output_contract,
            "created_at": _now()})
        return oracle_id, version
        self._require_auth(producer_id, token)
        if not name:
            raise OracleBindingError("oracle name is required")
        oracle_id = "orc_" + _digest(producer_id + "\x00" + name)[:20]
        definition_digest, definition_text = self._definition_text(definition)
        head = self.oracle_head(oracle_id)
        if head is not None:
            # A tampered head must fail closed, never be silently reused.
            ok, why = self.verify_oracle_row(oracle_id, head["version"])
            if not ok:
                raise OracleBindingError(
                    f"register_oracle refused: {why}")
            if hmac.compare_digest(head["definition_digest"],
                                   definition_digest):
                return oracle_id, head["version"]
        cur = self._conn.cursor()
        cur.execute("SELECT producer_type FROM ob_producers WHERE producer_id=?",
                    (producer_id,))
        prow = cur.fetchone()
        version = (head["version"] if head else 0) + 1
        self._insert_chained("ob_oracles", {
            "oracle_id": oracle_id, "version": version,
            "producer_id": producer_id,
            "producer_type": prow["producer_type"] if prow else "external",
            "source": source, "name": name,
            "definition_digest": definition_digest,
            "definition_text": definition_text,
            "input_contract": input_contract, "output_contract": output_contract,
            "created_at": _now()})
        return oracle_id, version

    def oracle_history(self, oracle_id: str) -> List[Dict[str, Any]]:
        """All registered versions of an oracle, oldest first. Append-only:
        history is never rewritten, so rollback is a visible event, not a
        silent deletion."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM ob_oracles WHERE oracle_id=? "
                    "ORDER BY version ASC", (oracle_id,))
        return [dict(r) for r in cur.fetchall()]

    def oracle_head(self, oracle_id: str) -> Optional[Dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM ob_oracles WHERE oracle_id=? "
                    "ORDER BY version DESC LIMIT 1", (oracle_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def oracle_version_row(self, oracle_id: str,
                           version: int) -> Optional[Dict[str, Any]]:
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM ob_oracles WHERE oracle_id=? AND version=?",
                    (oracle_id, version))
        row = cur.fetchone()
        return dict(row) if row else None

    def verify_oracle_row(self, oracle_id: str,
                          version: int) -> Tuple[bool, str]:
        """The oracle row evaluated is the oracle row registered: recompute
        its digest and compare, and confirm chain linkage."""
        d = self.oracle_version_row(oracle_id, version)
        if not d:
            return False, f"oracle {oracle_id} v{version}: no such registration"
        ordered = {k: d[k] for k in _DIGEST_FIELDS["ob_oracles"]}
        # recompute against the STORED prev (not the live head): this checks
        # the row itself is unmutated, independent of later appends.
        recomputed = _digest(d["prev_digest"] + "\x00" + _canonical(ordered))
        if not hmac.compare_digest(recomputed, d["row_digest"]):
            return False, (f"oracle {oracle_id} v{version}: row mutated "
                            f"(digest mismatch)")
        # definition integrity: the text must still hash to the digest
        if _digest(d["definition_text"]) != d["definition_digest"]:
            return False, (f"oracle {oracle_id} v{version}: definition text "
                            f"does not match its digest")
        return True, "oracle row intact"

    # -- authorizations -------------------------------------------------
    def authorize_oracle(self, oracle_id: str, version: int,
                         decision_class: str, granted_by_id: str,
                         granted_by_token: str) -> str:
        """Authorize a registered oracle for a decision class. The granter
        must itself hold DECISION_GRANT authority. Recorded, chained."""
        self._require_auth(granted_by_id, granted_by_token)
        if not self.producer_authorized(granted_by_id, DECISION_GRANT):
            raise OracleBindingError(
                f"{granted_by_id!r} lacks '{DECISION_GRANT}' authority: "
                f"cannot authorize oracles")
        if not self.oracle_version_row(oracle_id, version):
            raise OracleBindingError(f"no such oracle {oracle_id} v{version}")
        cur = self._conn.cursor()
        cur.execute("SELECT auth_id FROM ob_authorizations WHERE oracle_id=? "
                    "AND oracle_version=? AND decision_class=? AND granted_by=?",
                    (oracle_id, version, decision_class, granted_by_id))
        row = cur.fetchone()
        if row:
            return row["auth_id"]  # idempotent: same grant, no duplicate row
        auth_id = f"auth_{_digest(oracle_id + str(version) + decision_class + granted_by_id + str(time.time_ns()))[:16]}"
        self._insert_chained("ob_authorizations", {
            "auth_id": auth_id, "oracle_id": oracle_id,
            "oracle_version": version, "decision_class": decision_class,
            "granted_by": granted_by_id, "granted_at": _now()})
        return auth_id

    def authorize_producer(self, producer_id: str, decision_class: str,
                           granted_by_id: str,
                           granted_by_token: str) -> str:
        self._require_auth(granted_by_id, granted_by_token)
        if not self.producer_authorized(granted_by_id, DECISION_GRANT):
            raise OracleBindingError(
                f"{granted_by_id!r} lacks '{DECISION_GRANT}' authority")
        auth_id = f"pauth_{_digest(producer_id + decision_class + granted_by_id  + str(time.time_ns()))[:16]}"
        self._insert_chained("ob_producer_authorizations", {
            "auth_id": auth_id, "producer_id": producer_id,
            "decision_class": decision_class, "granted_by": granted_by_id,
            "granted_at": _now()})
        return auth_id

    def oracle_authorized(self, oracle_id: str, version: int,
                          decision_class: str) -> bool:
        cur = self._conn.cursor()
        cur.execute(
            "SELECT 1 FROM ob_authorizations WHERE oracle_id=? "
            "AND oracle_version=? AND decision_class=?",
            (oracle_id, version, decision_class))
        return cur.fetchone() is not None

    # -- evaluations ----------------------------------------------------
    def evaluate(self, producer_id: str, token: str, oracle_id: str,
                 input_obj: Any, result_obj: Any,
                 input_ref: str = "",
                 version: Optional[int] = None,
                 supplier_id: Optional[str] = None) -> str:
        """Record that oracle (oracle_id, version) was evaluated over
        input_obj producing result_obj. Binds oracle digest + input digest +
        result digest. Returns eval_id. ``version`` pins the oracle version;
        without it the head version is used (and recorded).

        ``producer_id`` is the EVALUATOR (who ran the oracle and attests the
        result); ``supplier_id`` is who SUPPLIED the oracle/result (defaults
        to the evaluator). Keeping them distinct is what lets the binding
        answer "who supplied the oracle" separately from "who ran it".
        """
        self._require_auth(producer_id, token)
        if version is None:
            head = self.oracle_head(oracle_id)
            if not head:
                raise OracleBindingError(
                    f"evaluate: unknown oracle {oracle_id}")
            version = head["version"]
        orow = self.oracle_version_row(oracle_id, version)
        if not orow:
            raise OracleBindingError(
                f"evaluate: unknown oracle {oracle_id} v{version}")
        ok, why = self.verify_oracle_row(oracle_id, version)
        if not ok:
            raise OracleBindingError(f"evaluate refused: {why}")
        input_digest = _digest(_canonical(input_obj))
        result_digest = _digest(_canonical(result_obj))
        try:
            result_summary = _canonical(result_obj)[:400]
        except Exception:
            result_summary = "<unrepresentable>"
        eval_id = "ev_" + _digest(oracle_id + str(version) +
                                 orow["row_digest"] + input_digest +
                                 result_digest + str(time.time_ns()))[:20]
        self._insert_chained("ob_evaluations", {
            "eval_id": eval_id, "oracle_id": oracle_id,
            "oracle_version": version,
            "oracle_digest": orow["row_digest"],
            "producer_id": producer_id, "supplier_id": supplier_id,
            "input_digest": input_digest,
            "input_ref": input_ref[:300], "result_digest": result_digest,
            "result_summary": result_summary, "evaluated_at": _now()})
        return eval_id

    def verify_binding(self, binding: Dict[str, Any],
                       expected_input_digest: Optional[str] = None,
                       require_head: bool = False
                       ) -> Tuple[bool, str]:
        """The core guarantee: the result being scored is the result of the
        registered oracle over the claimed input. Checks:

          - the evaluation row exists and its chain links are intact
          - the oracle row (id, version) exists and is unmutated
          - eval.oracle_digest == oracle row's digest (same version evaluated
            as registered -- kills oracle-swap and post-hoc mutation)
          - expected input digest matches when supplied (kills input-swap)
          - when require_head=True, the binding's version is the oracle's
            current head version (kills stale-version reuse: an evaluation
            bound to v1 presented after the oracle moved to v2)
        """
        for key in ("oracle_id", "version", "eval_id"):
            if key not in binding:
                return False, f"binding missing {key!r}"
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM ob_evaluations WHERE eval_id=?",
                    (binding["eval_id"],))
        erow = cur.fetchone()
        if not erow:
            return False, f"no such evaluation {binding['eval_id']}"
        ev = dict(erow)
        if ev["oracle_id"] != binding["oracle_id"] or \
                ev["oracle_version"] != binding["version"]:
            return False, "evaluation does not belong to the claimed oracle"
        # The evaluation row itself must be unmutated: kills result-swap /
        # result-tamper attacks (wrong oracle result).
        ok, why = self.verify_row("ob_evaluations", "eval_id",
                                  binding["eval_id"])
        if not ok:
            return False, f"evaluation record tampered: {why}"
        if require_head:
            head = self.oracle_head(binding["oracle_id"])
            if not head or head["version"] != binding["version"]:
                return False, ("stale oracle version: evaluation binds "
                                f"v{binding['version']} but head is "
                                f"v{head['version'] if head else '?'}")
        ok, why = self.verify_oracle_row(binding["oracle_id"],
                                         binding["version"])
        if not ok:
            return False, why
        if not hmac.compare_digest(ev["oracle_digest"],
                                   self.oracle_version_row(
                                       binding["oracle_id"],
                                       binding["version"])["row_digest"]):
            return False, ("evaluation's oracle digest does not match the "
                           "registered oracle row: oracle swapped or mutated "
                           "after evaluation")
        if expected_input_digest is not None and not hmac.compare_digest(
                ev["input_digest"], expected_input_digest):
            return False, "input digest mismatch: result is for another input"
        return True, "binding verified"

    # -- trust transitions ----------------------------------------------
    def transition_trust(self, capability_id: str, from_state: Optional[str],
                         to_state: str, producer_id: str, token: str,
                         reason: str = "") -> str:
        """Append-only trust transition. The producer must hold the
        'trust:transition' decision class. Trust state is the log head;
        direct mutation of any trust field elsewhere is detectable via
        audit (the head won't match)."""
        self._require_auth(producer_id, token)
        if not self.producer_authorized(producer_id, DECISION_TRUST_TRANSITION):
            raise OracleBindingError(
                f"{producer_id!r} lacks '{DECISION_TRUST_TRANSITION}' "
                f"authority: trust transition refused")
        trans_id = "trs_" + _digest(capability_id + str(from_state) +
                                    to_state + producer_id + str(time.time_ns()))[:16]
        self._insert_chained("ob_trust_transitions", {
            "trans_id": trans_id, "capability_id": capability_id,
            "from_state": from_state, "to_state": to_state,
            "producer_id": producer_id, "reason": reason[:500],
            "at": _now()})
        return trans_id

    def current_trust(self, capability_id: str) -> Optional[str]:
        cur = self._conn.cursor()
        cur.execute("SELECT to_state FROM ob_trust_transitions "
                    "WHERE capability_id=? ORDER BY seq DESC LIMIT 1",
                    (capability_id,))
        row = cur.fetchone()
        return row["to_state"] if row else None

    # -- grants -----------------------------------------------------------
    def issue_grant(self, effect: str, pattern: str, granted_by_id: str,
                    granted_by_token: str, scope: str = "") -> str:
        self._require_auth(granted_by_id, granted_by_token)
        if not self.producer_authorized(granted_by_id, DECISION_GRANT):
            raise OracleBindingError(
                f"{granted_by_id!r} lacks '{DECISION_GRANT}' authority: "
                f"grant refused")
        grant_id = "grt_" + _digest(effect + pattern + granted_by_id +
                                    str(time.time_ns()))[:16]
        self._insert_chained("ob_grants", {
            "grant_id": grant_id, "effect": effect, "pattern": pattern,
            "scope": scope[:300], "granted_by": granted_by_id,
            "granted_at": _now(), "revoked": 0})
        return grant_id

    def revoke_grant(self, grant_id: str, producer_id: str,
                     token: str) -> None:
        self._require_auth(producer_id, token)
        if not self.producer_authorized(producer_id, DECISION_GRANT):
            raise OracleBindingError(
                f"{producer_id!r} lacks '{DECISION_GRANT}' authority")
        # Revocation is a new chained row, not a mutation: the history stays.
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM ob_grants WHERE grant_id=? "
                    "ORDER BY seq DESC LIMIT 1", (grant_id,))
        row = cur.fetchone()
        if not row:
            raise OracleBindingError(f"no such grant {grant_id}")
        d = dict(row)
        self._insert_chained("ob_grants", {
            "grant_id": grant_id, "effect": d["effect"],
            "pattern": d["pattern"], "scope": d["scope"],
            "granted_by": producer_id, "granted_at": _now(), "revoked": 1})

    def check_grant(self, effect: str, target: str = "") -> Tuple[bool, str]:
        """Is there a live (unrevoked) grant covering effect:target? Grants
        are per-grant_id head rows; a grant covering '*' covers everything."""
        import fnmatch
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM ob_grants ORDER BY seq ASC")
        heads: Dict[str, Dict[str, Any]] = {}
        for row in cur.fetchall():
            heads[row["grant_id"]] = dict(row)
        for g in heads.values():
            if g["revoked"]:
                continue
            if str(g["effect"]).lower() == str(effect).lower() and (
                    g["pattern"] == "*" or
                    fnmatch.fnmatch(target, g["pattern"]) or
                    target == g["pattern"]):
                return True, (f"granted {g['effect']}:{g['pattern']} "
                               f"by {g['granted_by']}")
        return False, f"no live grant for {effect} on {target!r}"

    # -- lifecycle --------------------------------------------------------
    def close(self) -> None:
        self._conn.close()


class EngineOracleHandle:
    """The engine's instrument for oracle operations. Holds the engine
    producer's memory-only token. Engine code paths use this; external
    callers use the public registry API with their own credentials."""

    def __init__(self, registry: OracleRegistry, producer_id: str,
                 token: str):
        self._reg = registry
        self.producer_id = producer_id
        self._token = token

    def register_oracle(self, name: str, definition: Any,
                        input_contract: str = "",
                        output_contract: str = "",
                        source: str = "") -> Tuple[str, int]:
        return self._reg.register_oracle(self.producer_id, self._token, name,
                                         definition, input_contract,
                                         output_contract, source)

    def authorize_oracle(self, oracle_id: str, version: int,
                         decision_class: str) -> str:
        return self._reg.authorize_oracle(oracle_id, version, decision_class,
                                          self.producer_id, self._token)

    def authorize_producer(self, producer_id: str,
                           decision_class: str) -> str:
        return self._reg.authorize_producer(producer_id, decision_class,
                                            self.producer_id, self._token)

    def evaluate(self, oracle_id: str, input_obj: Any, result_obj: Any,
                 input_ref: str = "",
                 version: Optional[int] = None,
                 supplier_id: Optional[str] = None) -> str:
        return self._reg.evaluate(self.producer_id, self._token, oracle_id,
                                  input_obj, result_obj, input_ref,
                                  version=version, supplier_id=supplier_id)

    def transition_trust(self, capability_id: str,
                         from_state: Optional[str], to_state: str,
                         reason: str = "") -> str:
        return self._reg.transition_trust(capability_id, from_state, to_state,
                                          self.producer_id, self._token,
                                          reason)

    def issue_grant(self, effect: str, pattern: str,
                    scope: str = "") -> str:
        return self._reg.issue_grant(effect, pattern, self.producer_id,
                                     self._token, scope)

    def revoke_grant(self, grant_id: str) -> None:
        return self._reg.revoke_grant(grant_id, self.producer_id, self._token)
