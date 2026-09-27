"""
swarm_engine/services/artifacts.py

Artifact store for the REMOR third track: persisted, editable artifacts
with sandboxed execution. Replaces the GUI's current one-shot throwaway
execution with saved revisions that can be edited and re-run.

Model:
  * sqlite table artifacts(id INTEGER PK AUTOINCREMENT, prev_digest,
    row_digest, artifact_key TEXT, name, language, rev INTEGER,
    code TEXT, metadata_json TEXT, created_at REAL,
    UNIQUE(artifact_key, rev)).
  * artifact_key is a stable opaque uuid per artifact name; every row
    sharing a key is a revision of the same artifact. The public
    artifact_id is the row id of revision 1 (stable across revisions).
  * Editing = a new revision row; history is never overwritten.
  * Every row is hash-chained (prev_digest/row_digest, genesis-anchored)
    in id order; the whole table carries a head digest covered by the
    service anchor journal (CrossDbAnchor). Out-of-band tampering is
    DETECTED (audit()/verify fail closed) -- the Batch 11 cross-DB
    trust scope, 2026-09-25. get() still returns stored bytes verbatim;
    the detection surface is audit()/verify, mirroring the org store.
  * Legitimate delete appends a tombstone row (rev=0); the lineage
    stays archived and auditable, never physically removed.

Execution (merged 2026-09-26: B's sandbox-module design + C's
inline-preexec specifics):
  * python only, in a real subprocess; output captured and capped at
    4000 trailing chars.
  * Each run gets a FRESH per-run directory under the sandbox dir
    holding ONLY the artifact's own code file: sibling artifacts'
    staged files (save_many jobs, other runs' temp files) are never in
    the child's working directory, so bare relative paths cannot read
    or clobber them. After the run, files the artifact GENERATED are
    promoted to the sandbox root (everything except the script file,
    which lives in the DB) so execute_api's real before/after snapshot
    still collects them -- L's "generated files land in the sandbox
    dir" contract. The per-run dir itself is then removed
    (best-effort).
  * OS-level hardening comes from the services/sandbox.py MODULE
    (SandboxContext): rlimits (RLIMIT_CPU/AS/FSIZE/NPROC, env-tunable
    via REMOR_SANDBOX_*) applied in the child via preexec_fn, plus the
    opt-in uid drop (REMOR_SANDBOX_PRIVDROP=1). Per-run overrides are
    accepted as cpu_limit_s=/ram_limit_mb= (None = the module's
    env-driven defaults).
  * The child is launched with start_new_session=True (its own process
    group) and inherits a SCRUBBED minimal environment (see
    _child_env): no parent env leaks in blindly; HOME/TMPDIR point at
    the per-run dir so stray writes stay inside it.
  * On wall-clock timeout -- or any abnormal termination -- the parent
    SIGKILLs the entire process group with os.killpg and reaps the
    direct child, so spawned grandchildren cannot survive (no zombie:
    communicate() after the kill reaps it).
  * Non-python languages get the GUI's existing honest refusal:
    "execution for <lang> is not supported".
  * Empty code is refused ("empty code"), matching the current GUI.

Honest bounds:
  * Same user (unless the opt-in privdrop is active), no chroot / mount
    namespace / seccomp / network filtering. A deliberately malicious
    artifact CAN still: read or write anywhere the OS user can
    (absolute paths and ".." traversal are NOT blocked -- the per-run
    dir only defeats the *shared-cwd* sibling-file hazard), exfiltrate
    over the network, fork-bomb within its CPU/RAM budget, attack the
    kernel, or read the parent's files. RLIMIT_CPU counts CPU time
    only -- a sleeping process tree is bounded by the wall-clock
    timeout + killpg.
  * RLIMIT_AS is virtual-address-space, not resident set: it bites on
    allocation, and overcommit accounting varies by kernel.
  * If the HTTP layer ever exposes this to untrusted users, it needs a
    real sandbox (UID/container/VM); this is a same-user guard against
    buggy or greedy artifacts, not a security boundary against a
    malicious same-UID adversary.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.governance.anchor import (
    AnchorVerifyError,
    ChainAuditError,
    CrossDbAnchor,
    LegacySchemaError,
)

_OUTPUT_CAP = 4000

#: Reference per-run resource defaults (C's inline-preexec values).
#: The merged run() accepts cpu_limit_s=/ram_limit_mb= overrides;
#: None (the default) keeps the sandbox module's env-driven limits
#: (REMOR_SANDBOX_*: 120s CPU / 1024 MB AS generous defaults).
_DEFAULT_CPU_LIMIT_S = 30
_DEFAULT_RAM_LIMIT_MB = 256


def _child_env(run_dir: str) -> Dict[str, str]:
    """Minimal scrubbed environment for the artifact child process.

    Nothing from the parent's environment is inherited: secrets,
    tokens, and host configuration in os.environ never reach the
    artifact blindly. What IS passed, and why:
      PATH=/usr/bin:/bin:/usr/local/bin -- the artifact's interpreter
        is launched by absolute path already; a sane PATH keeps
        child-spawned tools predictable without leaking custom dirs.
      HOME=<run_dir>, TMPDIR=<run_dir> -- libraries that write caches
        or temp files (pip, matplotlib, tempfile) stay inside the
        per-run dir, which is removed afterwards.
      LANG/LC_ALL=C.UTF-8 -- deterministic UTF-8 stdio decoding.
      PYTHONDONTWRITEBYTECODE=1 -- no __pycache__ litter in the dir.
      PYTHONNOUSERSITE=1 -- the artifact sees the system interpreter
        environment only, not the operator's user site-packages.
    Deliberately NOT passed: everything else (HOME of the real user,
    API keys, proxy config, PYTHONPATH, ...).
    """
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": run_dir,
        "TMPDIR": run_dir,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _kill_process_group(pid: int) -> None:
    """SIGKILL every process in pid's process group (best-effort).

    The artifact child is started with start_new_session=True, so it
    is the group leader: killpg reaches the direct child AND any
    grandchildren it spawned (which survive a plain child kill and
    would otherwise be reparented to init). ProcessLookupError means
    the group is already gone -- the desired end state.
    """
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass



# ---------------------------------------------------------------------------
# Hash-chain machinery (mirrors runtime/agent_org/store.py OrgStore and
# runtime/services/scheduler.py). Chain order is the row id (append
# order; AUTOINCREMENT so a deleted tip can never have its id reused,
# which would hide a deletion from the strict sequence check).


_ARTIFACT_CHAIN_FIELDS = [
    "artifact_key", "name", "language", "rev",
    "code", "metadata_json", "created_at",
]

_ARTIFACT_GENESIS_DIGEST = hashlib.sha256(
    b"REMOR|artifacts|genesis").hexdigest()

#: rev value of a tombstone row (legitimate delete marker).
_TOMBSTONE_REV = 0

#: A table whose artifacts table lacks these columns is pre-chain legacy
#: and is refused at open (see LegacySchemaError).
_LEGACY_REQUIRED_ABSENT = ("prev_digest", "row_digest")

#: Staging dirs older than this with no committed rows are treated as
#: orphaned (crashed writer) and removed by open-time recovery.
_STAGING_GRACE_S = 60

#: Windows-reserved device names (James's G0-4 targets Windows): a job
#: file with one of these names would not materialize on Windows.
_WINDOWS_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_filename(name: Any) -> Optional[str]:
    """None if name is a safe plain filename, else the refusal reason.

    save_many() materializes files on disk, so names must be plain
    filenames: no separators, no traversal, no Windows device names.
    """
    if not isinstance(name, str) or not name.strip():
        return "name is required"
    name = name.strip()
    if len(name) > 255:
        return "name too long (max 255)"
    if "/" in name or "\\" in name or "\x00" in name:
        return "name must be a plain filename (no path separators)"
    if name in (".", ".."):
        return "name must be a plain filename"
    if os.path.isabs(name):
        return "name must be a plain filename (no absolute paths)"
    stem = name.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        return f"name {name!r} is a reserved device name"
    return None


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _row_digest(fields: Dict[str, Any], prev_digest: str) -> str:
    return hashlib.sha256(
        (_canonical(fields) + prev_digest).encode("utf-8")).hexdigest()


class ArtifactStore:
    """Persisted editable artifacts + sandboxed execution."""

    def __init__(self, db_path: str, sandbox_dir: str) -> None:
        self._db_path = os.path.abspath(db_path)
        self._sandbox_dir = sandbox_dir
        os.makedirs(sandbox_dir, exist_ok=True)
        self._lock = threading.Lock()
        # Service anchor attachment (CrossDbAnchor). None in unit tests /
        # anchor-free drivers; chaining is always on regardless.
        self._anchor = None
        self._authority = "artifact"
        self._init_db()
        # Crash recovery for atomic multi-file jobs: a writer killed
        # between staging and publish leaves a .staging/<job_id> dir
        # that no finally block could clean. See _recover_staging().
        self._recover_staging()

    @property
    def db_path(self) -> str:
        return self._db_path

    # -- persistence -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(artifacts)")]
            if cols and all(c not in cols
                            for c in _LEGACY_REQUIRED_ABSENT):
                raise LegacySchemaError(
                    "artifacts has the pre-chain schema (no chain "
                    "columns). Refusing to open: run "
                    "migrate_legacy_artifact_db() explicitly to adopt "
                    "this database (trust-on-first-use).")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prev_digest TEXT NOT NULL,
                    row_digest TEXT NOT NULL,
                    artifact_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    rev INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    UNIQUE(artifact_key, rev)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_key_rev "
                "ON artifacts(artifact_key, rev)"
            )
            n = conn.execute(
                "SELECT COUNT(*) FROM artifacts").fetchone()[0]
        # A non-empty chain table must verify on open -- a tampered
        # chain is never silently adopted, anchor or not. audit() takes
        # no store lock (provider contract); the table cannot change
        # under us here (we hold no writer yet, but this is open-time).
        if n:
            ok, msg = self.audit()
            if not ok:
                raise ChainAuditError(
                    f"artifacts chain audit failed on open: {msg}")

    def _key_for_name(self, conn: sqlite3.Connection, name: str) -> Optional[str]:
        row = conn.execute(
            "SELECT artifact_key FROM artifacts WHERE name = ? "
            "ORDER BY id DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            return None
        key = row["artifact_key"]
        latest = conn.execute(
            "SELECT rev FROM artifacts WHERE artifact_key = ? "
            "ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        if latest is not None and int(latest["rev"]) == _TOMBSTONE_REV:
            return None  # tombstoned: the name is free for a fresh key
        return key

    # -- hash chain ------------------------------------------------------
    def _tip_digest(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT row_digest FROM artifacts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["row_digest"] if row else _ARTIFACT_GENESIS_DIGEST

    def _append_row(self, conn: sqlite3.Connection,
                    data: Dict[str, Any]) -> int:
        """Append one chained row; returns the new row id."""
        prev = self._tip_digest(conn)
        ordered = {k: data.get(k) for k in _ARTIFACT_CHAIN_FIELDS}
        digest = _row_digest(ordered, prev)
        cur = conn.execute(
            "INSERT INTO artifacts (prev_digest, row_digest, "
            "artifact_key, name, language, rev, code, metadata_json, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (prev, digest, ordered["artifact_key"], ordered["name"],
             ordered["language"], ordered["rev"], ordered["code"],
             ordered["metadata_json"], ordered["created_at"]),
        )
        return cur.lastrowid

    def _is_tombstoned(self, conn: sqlite3.Connection,
                       key: str) -> bool:
        row = conn.execute(
            "SELECT rev FROM artifacts WHERE artifact_key = ? "
            "ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        return row is not None and int(row["rev"]) == _TOMBSTONE_REV

    # -- anchor ----------------------------------------------------------
    def attach_anchor(self, anchor, authority: str = "artifact") -> None:
        """Attach a CrossDbAnchor covering this store's chain.

        Verifies the existing chain first: a tampered table is never
        attached to a journal. The journal init/verify/refuse decision
        itself lives in build_services(), not here.
        """
        ok, msg = self.audit()
        if not ok:
            raise ChainAuditError(
                f"refusing to attach anchor: artifacts chain audit "
                f"failed: {msg}")
        with self._lock:
            self._anchor = anchor
            self._authority = authority or "artifact"

    def _attested_write(self, write_fn, op: str) -> None:
        """Run write_fn() (chained appends) under the anchor discipline.

        Caller must hold self._lock. With no anchor attached this is a
        plain chained write (unit-test path). With an anchor: pre-write
        verify of live heads against the journal tip (fail closed --
        never silently re-anchor already-tampered data), then the
        appends, then a post-write internal chain audit, then
        anchor_all(reason="attest"). Lock order is always
        self._lock -> CrossDbAnchor._ATTEST_LOCK.
        """
        anchor = self._anchor
        if anchor is None:
            write_fn()
            return
        with CrossDbAnchor._ATTEST_LOCK:
            ok, msg = anchor.verify_all()
            if not ok:
                raise AnchorVerifyError(
                    f"artifact {op}: pre-write anchor verify failed: "
                    f"{msg}")
            write_fn()
            audits = anchor.audit_providers()
            bad = {s: m for s, (ok2, m) in audits.items() if not ok2}
            if bad:
                raise AnchorVerifyError(
                    f"artifact {op}: post-write chain audit failed: "
                    f"{bad}")
            anchor.anchor_all(reason="attest",
                              authority=self._authority)

    # -- provider surface for CrossDbAnchor (no store locks inside:
    #    collection runs under CrossDbAnchor._ATTEST_LOCK; taking
    #    self._lock here would invert the lock order) -------------------
    def audit(self) -> Tuple[bool, str]:
        """Recompute the whole artifacts chain. (ok, msg)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, prev_digest, row_digest, artifact_key, "
                "name, language, rev, code, metadata_json, created_at "
                "FROM artifacts ORDER BY id"
            ).fetchall()
        expect_prev = _ARTIFACT_GENESIS_DIGEST
        expect_id = None
        for row in rows:
            (rid, prev_digest, row_digest, key, name, language, rev,
             code, meta_json, created_at) = row
            if expect_id is None:
                # First row: any id (a migrated DB preserves legacy
                # ids, so the chain need not start at 1). What matters
                # is no gaps afterwards; front-truncation against a
                # journal is caught by the head comparison, and the
                # migration itself is an explicit trust decision.
                expect_id = rid
            if rid != expect_id:
                return False, (
                    f"chain broken: expected id {expect_id}, found "
                    f"{rid} (deletion or splice)")
            if prev_digest != expect_prev:
                return False, (
                    f"chain broken at id {rid}: prev_digest mismatch")
            ordered = {"artifact_key": key, "name": name,
                       "language": language, "rev": rev, "code": code,
                       "metadata_json": meta_json,
                       "created_at": created_at}
            if _row_digest(ordered, prev_digest) != row_digest:
                return False, (
                    f"chain broken at id {rid}: row_digest mismatch "
                    f"(row tampered)")
            expect_prev = row_digest
            expect_id = rid + 1
        return True, f"artifacts chain ok ({len(rows)} rows)"

    def audit_all(self) -> Dict[str, Tuple[bool, str]]:
        return {"artifacts": self.audit()}

    def head_digest(self, table: str = "artifacts") -> str:
        """Full-table head digest (GENESIS when empty)."""
        if table != "artifacts":
            raise KeyError(f"unknown table {table!r}")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, prev_digest, row_digest, artifact_key, "
                "name, language, rev, code, metadata_json, created_at "
                "FROM artifacts ORDER BY id"
            ).fetchall()
        if not rows:
            return _ARTIFACT_GENESIS_DIGEST
        h = hashlib.sha256()
        for (rid, prev_digest, row_digest, key, name, language, rev,
             code, meta_json, created_at) in rows:
            h.update(_canonical(
                [rid, prev_digest, row_digest,
                 {"artifact_key": key, "name": name,
                  "language": language, "rev": rev, "code": code,
                  "metadata_json": meta_json,
                  "created_at": created_at}]
            ).encode("utf-8"))
        return h.hexdigest()

    def save(
        self,
        name: str,
        language: str,
        code: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Save a new revision. Same name -> new revision, history kept."""
        if not name or not str(name).strip():
            return {"ok": False, "error": "name is required"}
        if not isinstance(code, str) or not code.strip():
            return {"ok": False, "error": "empty code"}
        language = (language or "").strip().lower() or "python"
        name = str(name).strip()
        meta_json = json.dumps(metadata or {})
        now = time.time()
        result: Dict[str, Any] = {}

        def _write() -> None:
            with self._connect() as conn:
                key = self._key_for_name(conn, name)
                if key is None:
                    key = uuid.uuid4().hex
                    rev = 1
                else:
                    row = conn.execute(
                        "SELECT MAX(rev) AS m FROM artifacts "
                        "WHERE artifact_key = ?",
                        (key,),
                    ).fetchone()
                    rev = int(row["m"]) + 1
                row_id = self._append_row(conn, {
                    "artifact_key": key, "name": name,
                    "language": language, "rev": rev,
                    "code": code, "metadata_json": meta_json,
                    "created_at": now,
                })
                if rev == 1:
                    artifact_id = row_id
                else:
                    artifact_id = conn.execute(
                        "SELECT id FROM artifacts "
                        "WHERE artifact_key = ? AND rev = 1",
                        (key,),
                    ).fetchone()["id"]
                conn.commit()
            result.update({"ok": True, "artifact_id": artifact_id,
                           "revision": rev})

        with self._lock:
            self._attested_write(_write, "save")
        return result

    # -- atomic multi-file jobs ----------------------------------------
    def _committed_job_ids(self) -> set:
        """Job ids with at least one committed row (crash-recovery aid)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT "
                "json_extract(metadata_json, '$.job.job_id') AS jid "
                "FROM artifacts WHERE "
                "json_extract(metadata_json, '$.job.job_id') IS NOT NULL"
            ).fetchall()
        return {r["jid"] for r in rows}

    def _recover_staging(self) -> None:
        """Open-time recovery for atomic multi-file jobs.

        A writer killed mid-job (SIGKILL: no finally block runs) can
        leave `<sandbox>/.staging/<job_id>/` behind. Two cases:
        * the DB has committed rows for the job -> the crash happened
          after the DB commit but before the publish rename: complete
          the job by renaming the staging dir into place;
        * otherwise -> orphan staging: remove it, but only if it is
          older than _STAGING_GRACE_S, so a racing live writer's fresh
          staging dir is never reaped.
        Residual (documented): a writer that dies within the grace
        period leaves staging debris until the next open past it.
        """
        staging_root = os.path.join(self._sandbox_dir, ".staging")
        if not os.path.isdir(staging_root):
            return
        jobs_root = os.path.join(self._sandbox_dir, "jobs")
        os.makedirs(jobs_root, exist_ok=True)
        committed = self._committed_job_ids()
        for job_id in sorted(os.listdir(staging_root)):
            src = os.path.join(staging_root, job_id)
            if not os.path.isdir(src):
                continue
            dst = os.path.join(jobs_root, job_id)
            if job_id in committed:
                if os.path.isdir(dst):
                    shutil.rmtree(src, ignore_errors=True)
                else:
                    try:
                        os.rename(src, dst)
                    except OSError:
                        pass
            else:
                try:
                    age = time.time() - os.path.getmtime(src)
                except OSError:
                    continue
                if age > _STAGING_GRACE_S:
                    shutil.rmtree(src, ignore_errors=True)

    def save_many(
        self,
        files: List[Dict[str, Any]],
        job: Optional[Dict[str, Any]] = None,
        _fail_after: int = 0,
        _file_delay: float = 0.0,
    ) -> Dict[str, Any]:
        """Create N files as ONE atomic job (all-or-nothing).

        files: list of {name, language, code, metadata?}. Every file is
        validated BEFORE anything is written (fail-fast: a validation
        failure writes nothing anywhere). Names must be safe plain
        filenames (no separators/traversal/device names) because the
        job materializes working copies on disk.

        The job: stage all files to `<sandbox>/.staging/<job_id>/`,
        append all N revision rows in ONE sqlite transaction, COMMIT,
        then atomically rename the staging dir to
        `<sandbox>/jobs/<job_id>/`. Any failure before the commit rolls
        back the transaction AND removes the staging dir: zero rows,
        zero files. A process killed mid-job is cleaned by
        open-time recovery (_recover_staging).

        Every row's metadata_json carries an authoritative job record
        {job_id, job_seq, job_created_at, producer, purpose} --
        first-class provenance for James's locked constraint. The whole
        job runs under one _attested_write: one anchor attestation per
        job, concurrent jobs serialize on the store lock.

        _fail_after / _file_delay are TEST-ONLY fault-injection hooks
        (real exceptions/sleeps in the real path, default off).
        """
        # --- fail-fast validation: zero writes on any problem ---
        if not isinstance(files, (list, tuple)) or not files:
            return {"ok": False, "error": "files must be a non-empty list"}
        seen = set()
        cleaned: List[Dict[str, Any]] = []
        for i, f in enumerate(files):
            if not isinstance(f, dict):
                return {"ok": False,
                        "error": f"file {i}: must be a mapping"}
            err = _safe_filename(f.get("name"))
            if err:
                return {"ok": False, "error": f"file {i}: {err}"}
            name = str(f["name"]).strip()
            if name in seen:
                return {"ok": False,
                        "error": f"file {i}: duplicate name {name!r} "
                                 f"in one job"}
            seen.add(name)
            code = f.get("code")
            if not isinstance(code, str) or not code.strip():
                return {"ok": False,
                        "error": f"file {i} ({name}): empty code"}
            language = (f.get("language") or "").strip().lower() \
                or "python"
            meta = f.get("metadata")
            cleaned.append({
                "name": name, "language": language, "code": code,
                "metadata": dict(meta) if isinstance(meta, dict) else {},
            })
        job = job or {}
        producer = job.get("producer")
        purpose = job.get("purpose")
        job_id = uuid.uuid4().hex
        now = time.time()
        result: Dict[str, Any] = {}

        def _write() -> None:
            staging = os.path.join(self._sandbox_dir, ".staging", job_id)
            jobs_root = os.path.join(self._sandbox_dir, "jobs")
            os.makedirs(staging, exist_ok=False)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                ids: List[Dict[str, Any]] = []
                try:
                    for i, f in enumerate(cleaned):
                        fpath = os.path.join(staging, f["name"])
                        with open(fpath, "w", encoding="utf-8") as fh:
                            fh.write(f["code"])
                        if _file_delay:
                            time.sleep(_file_delay)
                        meta = dict(f["metadata"])
                        meta["job"] = {
                            "job_id": job_id, "job_seq": i,
                            "job_created_at": now,
                            "producer": producer, "purpose": purpose,
                        }
                        key = self._key_for_name(conn, f["name"])
                        if key is None:
                            key = uuid.uuid4().hex
                            rev = 1
                        else:
                            row = conn.execute(
                                "SELECT MAX(rev) AS m FROM artifacts "
                                "WHERE artifact_key = ?",
                                (key,),
                            ).fetchone()
                            rev = int(row["m"]) + 1
                        row_id = self._append_row(conn, {
                            "artifact_key": key, "name": f["name"],
                            "language": f["language"], "rev": rev,
                            "code": f["code"],
                            "metadata_json": json.dumps(meta),
                            "created_at": now,
                        })
                        if rev == 1:
                            artifact_id = row_id
                        else:
                            artifact_id = conn.execute(
                                "SELECT id FROM artifacts "
                                "WHERE artifact_key = ? AND rev = 1",
                                (key,),
                            ).fetchone()["id"]
                        ids.append({"artifact_id": artifact_id,
                                    "name": f["name"],
                                    "revision": rev})
                        if _fail_after and (i + 1) == _fail_after:
                            raise RuntimeError(
                                "injected failure after "
                                f"{_fail_after} file(s)")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
                # DB committed: publish the staged files atomically.
                os.makedirs(jobs_root, exist_ok=True)
                os.rename(staging,
                          os.path.join(jobs_root, job_id))
                result.update({"ok": True, "job_id": job_id,
                               "artifacts": ids})
            except Exception:
                # In-process failure: nothing survives (DB rolled back
                # above; staging removed here). A killed process skips
                # this path -- _recover_staging handles that on open.
                shutil.rmtree(staging, ignore_errors=True)
                raise

        with self._lock:
            self._attested_write(_write, "save_many")
        return result

    def artifacts_for_job(self, job_id: str) -> List[Dict[str, Any]]:
        """List a job's files from their provenance records."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT a.id AS row_id, a.name, a.rev, "
                "json_extract(a.metadata_json, '$.job.job_seq') "
                "  AS job_seq, "
                "(SELECT b.id FROM artifacts b "
                " WHERE b.artifact_key = a.artifact_key AND b.rev = 1"
                ") AS artifact_id "
                "FROM artifacts a WHERE "
                "json_extract(a.metadata_json, '$.job.job_id') = ? "
                "ORDER BY job_seq",
                (job_id,),
            ).fetchall()
        return [
            {"row_id": r["row_id"], "artifact_id": r["artifact_id"],
             "name": r["name"], "revision": r["rev"],
             "job_seq": r["job_seq"]}
            for r in rows
        ]

    def _resolve(self, artifact_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row else None

    def _latest_row(self, key: str) -> Optional[Dict[str, Any]]:
        # Latest by id (append order), NOT by rev: the tombstone row has
        # rev=0 and must be visible as the latest row of its key.
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_key = ?"
                " ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()
        return dict(row) if row else None

    def get(
        self, artifact_id: int, revision: Optional[int] = None
    ) -> Dict[str, Any]:
        base = self._resolve(artifact_id)
        if base is None:
            return {"ok": False, "error": "not found"}
        key = base["artifact_key"]
        latest = self._latest_row(key)
        if latest is None or int(latest["rev"]) == _TOMBSTONE_REV:
            # Tombstoned artifacts read as not found for every revision
            # (the lineage stays archived; see revisions()).
            return {"ok": False, "error": "not found"}
        if revision is None:
            row = latest
        else:
            with self._lock, self._connect() as conn:
                dbrow = conn.execute(
                    "SELECT * FROM artifacts WHERE artifact_key = ? AND rev = ?",
                    (key, revision),
                ).fetchone()
            row = dict(dbrow) if dbrow else None
        if row is None or int(row["rev"]) == _TOMBSTONE_REV:
            return {"ok": False, "error": "not found"}
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "artifact_key": row["artifact_key"],
            "name": row["name"],
            "language": row["language"],
            "revision": row["rev"],
            "code": row["code"],
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
        }

    def list_artifacts(self) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT l.artifact_key,
                       (SELECT id FROM artifacts a2
                        WHERE a2.artifact_key = l.artifact_key AND a2.rev = 1) AS artifact_id,
                       l.name, l.language, l.rev AS latest_revision,
                       l.created_at AS updated_at
                FROM artifacts l
                JOIN (SELECT artifact_key, MAX(id) AS m FROM artifacts
                      GROUP BY artifact_key) t
                  ON l.artifact_key = t.artifact_key AND l.id = t.m
                WHERE l.rev != 0
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            {
                "artifact_id": r["artifact_id"],
                "name": r["name"],
                "language": r["language"],
                "latest_revision": r["latest_revision"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def revisions(self, artifact_id: int) -> Dict[str, Any]:
        base = self._resolve(artifact_id)
        if base is None:
            return {"ok": False, "error": "not found"}
        key = base["artifact_key"]
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT rev, code, created_at FROM artifacts"
                " WHERE artifact_key = ? ORDER BY id ASC",
                (key,),
            ).fetchall()
            latest_rev = conn.execute(
                "SELECT rev FROM artifacts WHERE artifact_key = ?"
                " ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()["rev"]
        return {
            "ok": True,
            "artifact_id": artifact_id,
            "deleted": int(latest_rev) == _TOMBSTONE_REV,
            "revisions": [
                {"revision": r["rev"], "code": r["code"], "created_at": r["created_at"]}
                for r in rows
            ],
        }

    def delete(self, artifact_id: int) -> Dict[str, Any]:
        """Legitimate delete: append a tombstone row (rev=0), chained.

        The lineage is archived, never physically removed: audit() still
        covers every row including the tombstone, and revisions() shows
        the full history with deleted=True. A tombstoned name is free
        for a fresh key on the next save().
        """
        base = self._resolve(artifact_id)
        if base is None:
            return {"ok": False, "error": "not found"}
        key = base["artifact_key"]
        now = time.time()

        def _write() -> None:
            with self._connect() as conn:
                if self._is_tombstoned(conn, key):
                    return  # already deleted: idempotent no-op row-wise
                latest = conn.execute(
                    "SELECT name, language FROM artifacts "
                    "WHERE artifact_key = ? ORDER BY id DESC LIMIT 1",
                    (key,),
                ).fetchone()
                self._append_row(conn, {
                    "artifact_key": key,
                    "name": latest["name"],
                    "language": latest["language"],
                    "rev": _TOMBSTONE_REV,
                    "code": "",
                    "metadata_json": "{}",
                    "created_at": now,
                })
                conn.commit()

        with self._lock:
            # Read the tombstone state first so the return contract
            # ("not found" for already-deleted) is honest.
            with self._connect() as conn:
                already = self._is_tombstoned(conn, key)
            if already:
                return {"ok": False, "error": "not found"}
            self._attested_write(_write, "delete")
        return {"ok": True, "artifact_id": artifact_id}

    # -- execution -------------------------------------------------------
    def run(
        self,
        artifact_id: int,
        revision: Optional[int] = None,
        timeout: float = 30,
        cpu_limit_s: Optional[float] = None,
        ram_limit_mb: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Execute a saved python artifact in the hardened sandbox.

        cpu_limit_s / ram_limit_mb override the sandbox module's
        env-driven rlimits for THIS run only (None = keep the module
        defaults). Reference values: cpu_limit_s=30, ram_limit_mb=256
        (C's inline-preexec defaults); the module's generous env
        defaults (120s / 1024MB) apply when both are None.
        """
        got = self.get(artifact_id, revision)
        if not got["ok"]:
            return got
        language = got["language"]
        code = got["code"]
        if language != "python":
            return {
                "ok": False,
                "error": f"execution for {language} is not supported",
            }
        if not code.strip():
            return {"ok": False, "error": "empty code"}
        # [S13] OS-level hardening comes from the sandbox MODULE:
        # rlimits (+ uid drop when viable) are applied in the child via
        # preexec_fn. The context is built per run so REMOR_SANDBOX_*
        # env overrides take effect immediately; construction is
        # idempotent (chown only when the owner differs).
        from swarm_engine.services import sandbox as _sandbox
        ctx = _sandbox.SandboxContext(self._sandbox_dir)
        if cpu_limit_s is not None or ram_limit_mb is not None:
            # C's per-run configurability: override the env-read limits
            # on this context only; the module defaults stay untouched.
            cur = ctx.limits
            ctx.limits = _sandbox.SandboxLimits(
                cpu_seconds=int(cpu_limit_s)
                if cpu_limit_s is not None else cur.cpu_seconds,
                as_mb=int(ram_limit_mb)
                if ram_limit_mb is not None else cur.as_mb,
                fsize_mb=cur.fsize_mb,
                nproc=cur.nproc,
            )
        # C's fresh per-run directory: the child sees ONLY its own code
        # file in its cwd. Sibling artifacts' staged files (save_many
        # jobs, other runs' temp files) are never in the child's
        # working directory, so bare relative paths cannot read or
        # clobber them. Removed afterwards, best-effort.
        run_dir = tempfile.mkdtemp(
            prefix=f"run_{artifact_id}_r{got['revision']}_",
            dir=self._sandbox_dir,
        )
        if ctx.drop is not None:
            # Privdrop mode: the per-run dir is root-created; the
            # dropped child must be able to use it as its cwd.
            _name, uid, gid = ctx.drop
            os.chown(run_dir, uid, gid)
        fpath = os.path.join(
            run_dir, f"artifact_{artifact_id}_r{got['revision']}.py")
        try:
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(code)
            # Privdrop mode: the script is root-written 0600; the
            # dropped child must be able to read it.
            ctx.script_readable_by_child(fpath)
            # start_new_session=True: the child becomes a process-group
            # leader, so a timeout can SIGKILL the whole tree (child +
            # grandchildren), not just the direct child. The child
            # inherits a scrubbed minimal environment (_child_env).
            p = subprocess.Popen(
                [sys.executable, fpath],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=run_dir,
                env=_child_env(run_dir),
                start_new_session=True,
                preexec_fn=ctx.make_preexec(),
            )
            try:
                stdout, stderr = p.communicate(timeout=timeout)
                timed_out = False
                exit_code: Optional[int] = p.returncode
            except subprocess.TimeoutExpired:
                # Wall-clock exceeded: kill the entire process group
                # (grandchildren included), then reap the direct child.
                # communicate() after the kill reaps it -- no zombie.
                _kill_process_group(p.pid)
                stdout, stderr = p.communicate()
                timed_out = True
                exit_code = None
            stdout = (stdout or "")[-_OUTPUT_CAP:]
            if timed_out:
                note = f"timed out after {timeout}s"
                stderr = ((stderr + "\n" + note) if stderr else note)[
                    -_OUTPUT_CAP:]
            else:
                stderr = (stderr or "")[-_OUTPUT_CAP:]
            return {
                "ok": True,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "sandbox": ctx.describe(),
            }
        finally:
            # Promote generated files to the sandbox root BEFORE the
            # per-run dir is removed: L's collectible-output contract
            # (execute_api snapshots the sandbox dir before/after the
            # run with os.walk). The script file itself is excluded --
            # it is persisted in the artifact DB, not the sandbox.
            # Best-effort: a promotion failure must never lose the run
            # result above.
            try:
                _script_name = os.path.basename(fpath)
                for _entry in os.listdir(run_dir):
                    if _entry == _script_name:
                        continue
                    _src = os.path.join(run_dir, _entry)
                    _dst = os.path.join(self._sandbox_dir, _entry)
                    try:
                        if os.path.isdir(_src) and not os.path.islink(_src):
                            if os.path.isdir(_dst):
                                shutil.copytree(
                                    _src, _dst, dirs_exist_ok=True)
                                shutil.rmtree(_src, ignore_errors=True)
                            else:
                                shutil.move(_src, _dst)
                        else:
                            shutil.move(_src, _dst)
                    except OSError:
                        pass
            except OSError:
                pass
            shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Explicit legacy migration (operator step, never silent)
# ---------------------------------------------------------------------------
def migrate_legacy_artifact_db(db_path: str) -> Dict[str, Any]:
    """Rebuild a pre-chain artifacts table as a hash-chained table.

    This is the ONLY adoption path for legacy databases: ArtifactStore
    refuses to open the pre-chain schema (LegacySchemaError). Calling
    this function is the operator's explicit trust-on-first-use decision
    -- pre-chain rows carry no tamper evidence, so their content is
    re-anchored as-is and the adoption is loudly reported.

    Guarded: the rebuild runs in one transaction (new table -> copy
    chained in id order, ids preserved -> drop old -> rename); any
    failure rolls back and the legacy table is untouched. Public
    artifact ids (rev-1 row ids) are stable across the migration.
    """
    db_path = os.path.abspath(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(artifacts)")]
        if not cols:
            return {"ok": False,
                    "error": "no artifacts table; nothing to migrate"}
        if any(c in cols for c in _LEGACY_REQUIRED_ABSENT):
            return {"ok": True, "migrated": False,
                    "detail": "already the chain schema"}
        legacy_cols = ["id", "artifact_key", "name", "language", "rev",
                       "code", "metadata_json", "created_at"]
        if any(c not in cols for c in legacy_cols):
            return {"ok": False,
                    "error": f"unrecognized artifacts schema: {cols}"}
        rows = conn.execute(
            "SELECT id, artifact_key, name, language, rev, code, "
            "metadata_json, created_at FROM artifacts ORDER BY id"
        ).fetchall()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE artifacts_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prev_digest TEXT NOT NULL,
                    row_digest TEXT NOT NULL,
                    artifact_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    language TEXT NOT NULL,
                    rev INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    UNIQUE(artifact_key, rev)
                )
                """
            )
            prev = _ARTIFACT_GENESIS_DIGEST
            for row in rows:
                ordered = {k: row[k] for k in _ARTIFACT_CHAIN_FIELDS}
                digest = _row_digest(ordered, prev)
                conn.execute(
                    "INSERT INTO artifacts_new (id, prev_digest, "
                    "row_digest, artifact_key, name, language, rev, "
                    "code, metadata_json, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["id"], prev, digest, ordered["artifact_key"],
                     ordered["name"], ordered["language"],
                     ordered["rev"], ordered["code"],
                     ordered["metadata_json"], ordered["created_at"]),
                )
                prev = digest
            conn.execute("DROP TABLE artifacts")
            conn.execute("ALTER TABLE artifacts_new RENAME TO artifacts")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_artifacts_key_rev "
                "ON artifacts(artifact_key, rev)")
            # Keep AUTOINCREMENT above the migrated max id.
            if rows:
                conn.execute(
                    "UPDATE sqlite_sequence SET seq = "
                    "(SELECT MAX(id) FROM artifacts) "
                    "WHERE name = 'artifacts'")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"ok": True, "migrated": True,
                "detail": f"re-chained {len(rows)} legacy row(s) in id "
                          f"order, ids preserved; content adopted as-is "
                          f"(trust-on-first-use)"}
    finally:
        conn.close()
