"""
swarm_engine/project/ingestion.py

Project ingestion: extract a zip, walk the tree, and build a real, queryable
model of what is there — file inventory, a Python import graph, and
requirements extracted from markdown.

Honest scope: dependency and requirement extraction here is structural, not
semantic understanding. The import graph is built from `import`/`from`
statements via the AST (real, precise, for Python only); requirement
extraction is pattern-based (headings and bullet points), which finds
*candidate* requirements a human wrote down, not requirements SWarm has
inferred by understanding intent.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


@dataclass
class FileRecord:
    path: str
    size: int
    sha256: str
    language: str = "unknown"

    def as_dict(self) -> Dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256,
                "language": self.language}


@dataclass
class Requirement:
    text: str
    source_file: str
    line: int
    heading: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "source_file": self.source_file,
                "line": self.line, "heading": self.heading}


@dataclass
class ProjectModel:
    project_id: str
    root: str
    files: List[FileRecord] = field(default_factory=list)
    imports: Dict[str, List[str]] = field(default_factory=dict)
    requirements: List[Requirement] = field(default_factory=list)
    # Declared check commands (argv lists) the project's own definition says
    # count as verification. Loaded from remor_checks.json at ingest; empty
    # means the project declares no checks (AgentZero records that fact
    # explicitly rather than pretending checks ran).
    checks: List[List[str]] = field(default_factory=list)
    ingested_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"project_id": self.project_id, "root": self.root,
                "file_count": len(self.files),
                "files": [f.as_dict() for f in self.files],
                "imports": self.imports,
                "requirement_count": len(self.requirements),
                "requirements": [r.as_dict() for r in self.requirements],
                "checks": [list(c) for c in self.checks],
                "ingested_at": self.ingested_at}


_LANGUAGE_BY_EXT = {".py": "python", ".js": "javascript", ".ts": "typescript",
                    ".md": "markdown", ".json": "json", ".yaml": "yaml",
                    ".yml": "yaml", ".txt": "text", ".html": "html",
                    ".css": "css", ".sh": "shell"}

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)")
_REQUIREMENT_WORDS = ("must", "should", "shall", "needs to", "required",
                      "requirement")

# project_id values reach ingestion from the HTTP API (POST /api/projects),
# so they are untrusted input: 1-64 chars, [A-Za-z0-9_-], leading
# alphanumeric. Anything else (traversal, separators, NUL, blanks) is
# refused before any filesystem work happens.
_PROJECT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# Maximum bytes for a single entry's name. Mirrors the reference archive
# path's canonical-name policy.
_MAX_ENTRY_NAME_LEN = 1024

# Streaming read size for extraction. Caps are checked per chunk so a
# forged central directory cannot make us write unbounded real bytes.
_STREAM_CHUNK = 64 * 1024


class ZipEntryUnsafeError(ValueError):
    """A zip entry failed the safety policy (unsafe name, symlink,
    encrypted). Whole-archive refusal: the archive is rejected, never
    partially ingested."""


def _check_zip_entry_name(name):
    """Strict canonical-form entry-name check.

    Returns (True, "") for safe names, (False, reason) otherwise.
    Mirrors the reference ScopedFileService._check_entry_name policy:
    empty names, over-long names, NUL bytes, backslashes, absolute
    paths, drive-letter paths, UNC prefixes, and empty / "." / ".."
    components are all unsafe. A trailing "/" marks a directory entry
    and is allowed. Encoded traversal ("..%2f..") is NOT decoded — it
    stays a literal, contained filename (same semantics as the
    reference path).
    """
    if not name:
        return False, "empty entry name"
    if len(name) > _MAX_ENTRY_NAME_LEN:
        return False, "entry name too long"
    if "\x00" in name:
        return False, "NUL byte in entry name"
    if "\\" in name:
        return False, "backslash in entry name"
    if name.startswith("\\\\") or name.startswith("//"):
        return False, "UNC path in entry name"
    if re.match(r"^[A-Za-z]:", name):
        return False, "drive-letter path in entry name"
    if name.startswith("/"):
        return False, "absolute path in entry name"
    stripped = name[:-1] if name.endswith("/") else name
    for part in stripped.split("/"):
        if part == "..":
            return False, "dot-dot component in entry name"
        if part == ".":
            return False, "dot component in entry name"
        if part == "":
            return False, "empty component in entry name"
    return True, ""


def _zipinfo_is_symlink(zi):
    """True if the ZipInfo describes a symlink (unix mode bits), mirroring
    the reference _is_symlink_zi."""
    return (zi.external_attr >> 16) & 0o170000 == 0o120000


def _validate_project_id(project_id):
    """Refuse traversal/separator/NUL/blank/over-long project ids before
    they touch the filesystem."""
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        raise ValueError(f"invalid project_id: {project_id!r}")
    return project_id


class ProjectIngestor:
    """Extracts a zip (or reads an existing directory) into a managed root and
    builds a ProjectModel. The managed root is where all further project
    operations (modification, commands) are scoped."""

    MAX_FILES = 5000
    MAX_TOTAL_BYTES = 200 * 1024 * 1024
    # Per-entry streaming cap. Defaults to the total cap so every archive
    # ingestible under the old claimed-total check stays ingestible; its
    # job is bounding the REAL byte stream when the central directory lies
    # about sizes (forged-size refusal, sibling parity with the reference
    # archive path's per-entry streaming cap).
    MAX_ENTRY_BYTES = 200 * 1024 * 1024

    def __init__(self, projects_dir: str = "/tmp/swarm_projects"):
        self.projects_dir = projects_dir
        os.makedirs(projects_dir, exist_ok=True)

    def ingest_zip(self, zip_path: str, project_id: Optional[str] = None) -> ProjectModel:
        if project_id is None:
            project_id = hashlib.sha256(
                f"{zip_path}{time.time()}".encode()).hexdigest()[:16]
        _validate_project_id(project_id)
        if not os.path.isfile(zip_path):
            raise ValueError(f"archive not found: {zip_path}")
        root = os.path.join(self.projects_dir, project_id)
        root_real = os.path.realpath(root)
        created_root = not os.path.lexists(root)
        os.makedirs(root, exist_ok=True)
        written: List[str] = []
        created_dirs: List[str] = []
        try:
            with zipfile.ZipFile(zip_path) as archive:
                members = archive.infolist()
                self._prescan_zip_members(members)
                self._stream_extract(archive, members, root_real,
                                     written, created_dirs)
        except Exception as exc:
            self._cleanup_partial(root, created_root, written, created_dirs)
            if isinstance(exc, zipfile.BadZipFile):
                raise ValueError(
                    f"{zip_path} is not a zip archive ({exc})") from exc
            raise
        return self.index(root, project_id)

    def _prescan_zip_members(self, members: List[zipfile.ZipInfo]) -> None:
        """Whole-archive pre-scan BEFORE any byte is written: entry-count
        cap, claimed-total cap (catches over-claim size lies), then
        per-entry name / symlink / encrypted checks. Any unsafe entry
        refuses the whole archive, naming every offender."""
        if len(members) > self.MAX_FILES:
            raise ValueError(f"archive has {len(members)} entries, "
                             f"exceeding the {self.MAX_FILES} entry limit")
        claimed = sum(m.file_size for m in members)
        if claimed > self.MAX_TOTAL_BYTES:
            raise ValueError(f"archive claims {claimed} bytes, exceeding "
                             f"the {self.MAX_TOTAL_BYTES} byte limit")
        bad = []
        for member in members:
            name = member.filename
            ok, reason = _check_zip_entry_name(name)
            if not ok:
                bad.append(f"{name!r} ({reason})")
            elif _zipinfo_is_symlink(member):
                bad.append(f"{name!r} (symlink entry)")
            elif member.flag_bits & 0x1:
                bad.append(f"{name!r} (encrypted entry)")
        if bad:
            raise ZipEntryUnsafeError(
                "archive refused: unsafe entries: " + "; ".join(bad))

    def _stream_extract(self, archive: zipfile.ZipFile,
                        members: List[zipfile.ZipInfo],
                        root_real: str,
                        written: List[str],
                        created_dirs: List[str]) -> None:
        """Extract entry-by-entry with per-entry and running-total caps
        checked per chunk against REAL streamed bytes — the second layer
        under the claimed-size pre-scan, catching under-claim size lies.
        Symlink entries never reach here (pre-scan refusal); the
        containment re-check is defense in depth."""
        total_streamed = 0
        for member in members:
            name = member.filename
            target = self._contained_target(root_real, name)
            if name.endswith("/"):
                if not os.path.isdir(target):
                    os.makedirs(target, exist_ok=True)
                    created_dirs.append(target)
                continue
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                # record newly created parents (deepest first) for cleanup
                missing = []
                cursor = parent
                while cursor and not os.path.isdir(cursor):
                    missing.append(cursor)
                    cursor = os.path.dirname(cursor)
                os.makedirs(parent, exist_ok=True)
                created_dirs.extend(missing)
            entry_bytes = 0
            # Open the destination first so a mid-stream failure still has
            # a tracked partial file for cleanup.
            with open(target, "wb") as dst:
                written.append(target)
                with archive.open(member) as src:
                    while True:
                        chunk = src.read(_STREAM_CHUNK)
                        if not chunk:
                            break
                        entry_bytes += len(chunk)
                        total_streamed += len(chunk)
                        if entry_bytes > self.MAX_ENTRY_BYTES:
                            raise ZipEntryUnsafeError(
                                f"entry {name!r} exceeded "
                                f"{self.MAX_ENTRY_BYTES} bytes while "
                                f"streaming")
                        if total_streamed > self.MAX_TOTAL_BYTES:
                            raise ZipEntryUnsafeError(
                                f"archive exceeded {self.MAX_TOTAL_BYTES} "
                                f"bytes while streaming")
                        dst.write(chunk)

    @staticmethod
    def _contained_target(root_real: str, name: str) -> str:
        """Defense in depth under the canonical name check: resolve the
        target against the real project root and re-check containment
        with realpath + commonpath (mirrors the reference
        _resolve_under)."""
        target = os.path.realpath(os.path.join(root_real, name))
        if os.path.commonpath([root_real, target]) != root_real:
            raise ZipEntryUnsafeError(
                f"archive entry {name!r} escapes the project root")
        return target

    @staticmethod
    def _cleanup_partial(root: str, created_root: bool,
                         written: List[str],
                         created_dirs: List[str]) -> None:
        """Best-effort cleanup after a refused archive: unlink files we
        wrote, remove directories we created (deepest first, only if
        empty), and remove the project root if we created it."""
        for path in written:
            try:
                os.unlink(path)
            except OSError:
                pass
        for directory in reversed(created_dirs):
            try:
                os.rmdir(directory)
            except OSError:
                pass
        if created_root:
            try:
                os.rmdir(root)
            except OSError:
                pass

    def ingest_directory(self, source_dir: str, project_id: Optional[str] = None
                         ) -> ProjectModel:
        project_id = project_id or hashlib.sha256(
            f"{source_dir}{time.time()}".encode()).hexdigest()[:16]
        return self.index(source_dir, project_id)

    def index(self, root: str, project_id: str) -> ProjectModel:
        model = ProjectModel(project_id=project_id, root=os.path.abspath(root))
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__",
                                                            "node_modules", ".venv")]
            for filename in filenames:
                file_count += 1
                if file_count > self.MAX_FILES:
                    raise ValueError(f"project exceeds {self.MAX_FILES} files")
                full = os.path.join(dirpath, filename)
                relative = os.path.relpath(full, root)
                try:
                    size = os.path.getsize(full)
                    with open(full, "rb") as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    continue
                ext = os.path.splitext(filename)[1].lower()
                language = _LANGUAGE_BY_EXT.get(ext, "unknown")
                model.files.append(FileRecord(relative, size, digest, language))

                if language == "python":
                    model.imports[relative] = self._python_imports(full)
                if language == "markdown":
                    model.requirements.extend(self._markdown_requirements(full, relative))

        model.checks = self._load_declared_checks(root)
        return model

    @staticmethod
    def _load_declared_checks(root: str) -> List[List[str]]:
        """Declared-check contract: <root>/remor_checks.json, a JSON array
        of argv arrays. Malformed entries are ignored (never crash ingest);
        an absent file means the project declares no checks."""
        path = os.path.join(root, "remor_checks.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return []
        checks: List[List[str]] = []
        if isinstance(raw, list):
            for entry in raw:
                if (isinstance(entry, list) and entry
                        and all(isinstance(x, str) for x in entry)):
                    checks.append(list(entry))
        return checks

    def _python_imports(self, path: str) -> List[str]:
        try:
            with open(path, "r", errors="replace") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (SyntaxError, OSError):
            return []
        modules: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module.split(".")[0])
        return sorted(modules)

    def _markdown_requirements(self, path: str, relative: str) -> List[Requirement]:
        requirements = []
        heading = ""
        try:
            with open(path, "r", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        for line_number, line in enumerate(lines, 1):
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                heading = heading_match.group(2).strip()
                continue
            bullet_match = _BULLET_RE.match(line)
            text = bullet_match.group(1).strip() if bullet_match else line.strip()
            if not text:
                continue
            # A bullet point is taken as a candidate requirement regardless of
            # wording — that is what bullets under a requirements heading are
            # for. A bare paragraph is only taken as one if a requirement-
            # signalling word appears near its start ("The system must...",
            # "It should..."), not merely anywhere in the sentence — matching
            # anywhere caught "not a requirement" as a requirement, which is
            # the opposite of what the sentence said. This still cannot
            # detect negation in general; it only narrows the easy case.
            leading_words = " ".join(text.split()[:5]).lower()
            if bullet_match or any(w in leading_words for w in _REQUIREMENT_WORDS):
                if len(text) > 5:
                    requirements.append(Requirement(text, relative, line_number, heading))
        return requirements


class ProjectStore:
    """Persists ingested project models so they survive restart."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, root TEXT, model TEXT, ingested_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, model: ProjectModel) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO projects
                (project_id, root, model, ingested_at) VALUES (?,?,?,?)""",
                (model.project_id, model.root, json.dumps(model.as_dict()),
                 model.ingested_at))

    def get(self, project_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT model FROM projects WHERE project_id=?",
                              (project_id,)).fetchone()
        return json.loads(row["model"]) if row else None

    def list(self) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT project_id FROM projects").fetchall()
        return [r["project_id"] for r in rows]
