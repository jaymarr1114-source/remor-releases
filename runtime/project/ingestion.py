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


class ProjectIngestor:
    """Extracts a zip (or reads an existing directory) into a managed root and
    builds a ProjectModel. The managed root is where all further project
    operations (modification, commands) are scoped."""

    MAX_FILES = 5000
    MAX_TOTAL_BYTES = 200 * 1024 * 1024

    def __init__(self, projects_dir: str = "/tmp/swarm_projects"):
        self.projects_dir = projects_dir
        os.makedirs(projects_dir, exist_ok=True)

    def ingest_zip(self, zip_path: str, project_id: Optional[str] = None) -> ProjectModel:
        project_id = project_id or hashlib.sha256(
            f"{zip_path}{time.time()}".encode()).hexdigest()[:16]
        root = os.path.join(self.projects_dir, project_id)
        os.makedirs(root, exist_ok=True)

        with zipfile.ZipFile(zip_path) as archive:
            members = archive.infolist()
            if len(members) > self.MAX_FILES:
                raise ValueError(f"archive has {len(members)} entries, "
                                 f"exceeding the {self.MAX_FILES} file limit")
            total = sum(m.file_size for m in members)
            if total > self.MAX_TOTAL_BYTES:
                raise ValueError(f"archive is {total} bytes, exceeding the "
                                 f"{self.MAX_TOTAL_BYTES} byte limit")
            for member in members:
                target = os.path.normpath(os.path.join(root, member.filename))
                if not target.startswith(os.path.abspath(root)):
                    raise ValueError(f"archive entry {member.filename!r} "
                                     f"attempts to escape the project root")
            archive.extractall(root)

        return self.index(root, project_id)

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
