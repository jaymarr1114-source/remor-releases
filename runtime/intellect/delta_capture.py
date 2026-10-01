"""Delta-record capture at real session/work boundaries (V10-P3).

James's technique-distillation charter (standing): a work session yields
delta records ONLY where there is an actual observable capability gap
(Y-Z) with sufficient evidence of what was done. The transcript is not a
magical training set; not every instruction represents a reproducible
capability.

The charter schema existed before this module as a loose convention --
``record_distillation_experience`` in unified_memory.py writes any dict it
is given, and ``DistillationLoop._log_experience_raw`` writes Observation
rows directly through the frozen API, bypassing the unified write path
entirely. Nothing validated the schema; nothing enforced the causal
discipline. This module is the enforcement boundary:

* ``validate_delta`` -- mechanical charter-schema validation (9 fields,
  per-field rules). Invalid records are refused, never stored hopefully.
* ``emit_delta`` -- validate-then-write through V10-P1's
  ``record_experience`` (called, never reimplemented): provenance
  (origin loop, timestamp, causal chain) is intact from the first record.

The live capture path is acquisition/ingest.py
(ingest_external_demonstration, ingest_subagent_trace, ingest_chat_turn,
ingest_plugin_action) -- this module provides the validation and
adjudication machinery those paths use, not a session-boundary API.

Ownership: this file is V10-P3's. runtime/intellect/unified_memory.py is
V10-P1's -- called, never edited.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4


# ---------------------------------------------------------------------------
# Charter schema (James's delta record schema, standing)
# ---------------------------------------------------------------------------

DELTA_FIELDS = (
    "objective_x",          # Objective X
    "external_demo_y",      # what the external agent did Y
    "native_inventory_z",   # what REMOR could already do Z
    "capability_gap",       # capability gap Y-Z (must state before/after)
    "technique_t",          # technique/procedure demonstrated T {name, probe}
    "evidence_e",           # evidence E: >=1 verifiable artifact
    "dependencies_d",        # dependencies D (may be empty)
    "verification_v",       # verification V performed, with outcome
    "resulting_capability_c",  # resulting synthesized capability C (import path)
)

SCHEMA_ID = "charter-9"


def validate_delta(record: Dict[str, Any]) -> List[str]:
    """Mechanically validate a delta record against the charter schema.

    Returns a list of violation strings; empty means valid. Pure function:
    no I/O, no store access -- safe to call anywhere, including inside the
    write path itself.
    """
    errors: List[str] = []
    if not isinstance(record, dict):
        return ["record must be a dict"]
    for field in DELTA_FIELDS:
        if field not in record:
            errors.append(f"missing field: {field}")
    if errors:
        return errors

    def _str(field: str, min_len: int) -> Optional[str]:
        v = record[field]
        if not isinstance(v, str) or len(v.strip()) < min_len:
            return (f"{field} must be a string of at least {min_len} "
                    f"characters")
        return None

    for field, min_len in (("objective_x", 10), ("external_demo_y", 20),
                           ("native_inventory_z", 20),
                           ("verification_v", 20),
                           ("resulting_capability_c", 3)):
        e = _str(field, min_len)
        if e:
            errors.append(e)

    gap = record.get("capability_gap")
    if not isinstance(gap, str) or len(gap.strip()) < 20:
        errors.append("capability_gap must be a string of at least 20 "
                      "characters")
    elif "before" not in gap.lower() or "after" not in gap.lower():
        # Mechanical before/after requirement: a gap claim must state the
        # probe outcome before the session's work and after it. This is
        # crude but enforceable -- prose alone is not a gap.
        errors.append("capability_gap must state the before/after probe "
                      "outcomes (must contain 'before' and 'after')")

    t = record.get("technique_t")
    if not isinstance(t, dict):
        errors.append("technique_t must be a dict {name, probe}")
    else:
        if not isinstance(t.get("name"), str) or not t["name"].strip():
            errors.append("technique_t.name must be a non-empty string")
        probe = t.get("probe")
        if not isinstance(probe, dict):
            errors.append("technique_t.probe must be a dict "
                          "{import, attr, check}")
        else:
            for k in ("import", "attr", "check"):
                if not isinstance(probe.get(k), str) or not probe[k].strip():
                    errors.append(f"technique_t.probe.{k} must be a "
                                  f"non-empty string")

    ev = record.get("evidence_e")
    if not isinstance(ev, list) or not ev:
        errors.append("evidence_e must be a non-empty list of artifacts")
    else:
        for i, a in enumerate(ev):
            if not isinstance(a, dict) or a.get("kind") not in ("file", "run"):
                errors.append(f"evidence_e[{i}].kind must be 'file' or 'run'")
                continue
            if a["kind"] == "file" and not isinstance(a.get("path"), str):
                errors.append(f"evidence_e[{i}].path must be a string")
            if a["kind"] == "run":
                if (not isinstance(a.get("cmd"), list) or not a["cmd"]):
                    errors.append(f"evidence_e[{i}].cmd must be a non-empty "
                                  f"list")
                if not isinstance(a.get("output_includes"), list):
                    errors.append(f"evidence_e[{i}].output_includes must be "
                                  f"a list")

    if not isinstance(record.get("dependencies_d"), list):
        errors.append("dependencies_d must be a list (may be empty)")

    return errors


class DeltaRefused(Exception):
    """Raised when a delta is refused at write time. The record is never
    stored: refusal happens before any store access."""


def emit_delta(epistemic: Any, session_id: str, delta: Dict[str, Any],
               causal_chain: Optional[Sequence[str]] = None,
               provider_marker: Optional[str] = None) -> str:
    """Validate a delta record and write it through the unified write path.

    Raises DeltaRefused (before any store access) on schema violation.
    Returns the observation id. Provenance: origin_loop="acquisition",
    kind="technique_delta", causal chain rooted at the session.
    provider_marker is recorded alongside (not part of the charter schema)
    so the gap claim stays re-verifiable.
    """
    errors = validate_delta(delta)
    if errors:
        raise DeltaRefused("; ".join(errors))
    from runtime.intellect.unified_memory import record_experience
    name = delta["technique_t"]["name"]
    raw = {"delta": delta, "session_id": session_id, "validated": True,
           "schema": SCHEMA_ID}
    if provider_marker:
        raw["provider_marker"] = provider_marker
    return record_experience(
        epistemic,
        origin_loop="acquisition",
        kind="technique_delta",
        content=f"technique delta: {name} (session {session_id})",
        raw=raw,
        causal_chain=list(causal_chain or [session_id]),
        source="delta-capture")


# ---------------------------------------------------------------------------
# Adjudication machinery (causal discipline, mechanical)
# ---------------------------------------------------------------------------

CANONICAL = os.path.expanduser("~/workspace/remor_convergence/canonical")
PYLIB = os.path.join(CANONICAL, "pylib")


def _find_other_providers(marker: str,
                          exclude_files: Sequence[str]) -> List[str]:
    """Real grep over runtime/ for the technique's provider marker.

    Returns relative paths defining the marker OUTSIDE the session's own
    artifact files. Non-empty means the technique is already native: no
    observable gap.
    """
    excluded = {os.path.abspath(p) for p in exclude_files}
    try:
        proc = subprocess.run(
            ["grep", "-rl", "--include=*.py", marker, "runtime/"],
            cwd=CANONICAL, capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    hits = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if os.path.abspath(os.path.join(CANONICAL, line)) not in excluded:
            hits.append(line)
    return hits


def _probe_in_subprocess(probe: Dict[str, str], timeout: int = 120) -> bool:
    """Execute the technique probe in a FRESH Python process.

    The probe imports the technique's module from scratch and checks the
    named attribute. Fresh-process: the parent process's already-imported
    modules cannot mask an absent technique.
    """
    code = (
        "import sys, importlib\n"
        f"sys.path.insert(0, {CANONICAL!r})\n"
        f"sys.path.insert(0, {PYLIB!r})\n"
        "try:\n"
        f"    _m = importlib.import_module({probe['import']!r})\n"
        "    _o = _m\n"
        f"    _parts = {probe['attr']!r}.split('.')\n"
        "    _ok = True\n"
        "    _res = None\n"
        "    try:\n"
        "        for _p in _parts:\n"
        "            _o = getattr(_o, _p)\n"
        f"        _res = _o() if {probe['check']!r} == 'call_true' else _o\n"
        f"        _ok = bool(_res) if {probe['check']!r} == 'call_true' else callable(_o)\n"
        "    except Exception:\n"
        "        _ok = False\n"
        "    print('PROBE_OK' if _ok else 'PROBE_FAIL')\n"
        "except Exception:\n"
        "    print('PROBE_FAIL')\n"
    )
    try:
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=timeout,
                              cwd=CANONICAL)
    except Exception:
        return False
    return proc.returncode == 0 and "PROBE_OK" in proc.stdout


class _hidden:
    """Context manager: rename file artifacts aside (plus their __pycache__
    entries) so the fresh-subprocess probe cannot see them."""

    def __init__(self, paths: Sequence[str]):
        self.paths = [p for p in paths if os.path.isfile(p)]
        self.renamed: List[Tuple[str, str]] = []

    def _pycache_siblings(self, path: str) -> List[str]:
        d = os.path.join(os.path.dirname(path), "__pycache__")
        stem = os.path.splitext(os.path.basename(path))[0]
        out = []
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.startswith(stem + ".") and f.endswith(".pyc"):
                    out.append(os.path.join(d, f))
        return out

    def __enter__(self):
        for p in self.paths:
            targets = [p] + self._pycache_siblings(p)
            for t in targets:
                if os.path.isfile(t):
                    hidden = t + ".p3hidden"
                    os.rename(t, hidden)
                    self.renamed.append((t, hidden))
        return self

    def __exit__(self, *exc):
        for original, hidden in reversed(self.renamed):
            try:
                if os.path.isfile(hidden):
                    os.rename(hidden, original)
            except OSError:
                pass
        self.renamed = []
        return False


def _reexecute_run(run: Dict[str, Any], timeout: int = 600
                   ) -> Tuple[bool, str]:
    """Re-execute a cited run NOW. Evidence is re-verified, not trusted
    from a stored log."""
    try:
        proc = subprocess.run(run["cmd"], capture_output=True, text=True,
                              timeout=timeout, cwd=CANONICAL)
    except Exception as exc:
        return False, f"run failed to execute: {exc}"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout)[-500:]
        return False, f"exit={proc.returncode}: {tail}"
    missing = [m for m in run.get("output_includes", [])
               if m not in proc.stdout]
    if missing:
        return False, f"output markers missing: {missing}"
    return True, "ok"


def _session_attributable(paths: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Split cited file artifacts into session-attributable vs pre-existing.

    A cited file counts as this session's work iff it is UNCOMMITTED
    (untracked or modified in the working tree, per real `git status`).
    Committed-unchanged files are some earlier session's landed work: a
    session citing them as its own artifacts is claiming a pre-existing
    capability, which is exactly the no-gap case the discipline rejects.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "--"] + [str(p) for p in paths],
            cwd=CANONICAL, capture_output=True, text=True, timeout=30)
    except Exception:
        return [], list(paths)
    dirty = set()
    for line in proc.stdout.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        # porcelain v1: XY <path> (rename: XY <orig> -> <new>; take last)
        dirty.add(os.path.abspath(os.path.join(CANONICAL, parts[-1])))
    attributable, preexisting = [], []
    for p in paths:
        (attributable if os.path.abspath(p) in dirty else preexisting
         ).append(p)
    return attributable, preexisting


def _adjudicate_demonstration(demo: Dict[str, Any], session_id: str
                             ) -> Tuple[str, str, str]:
    """Adjudicate one demonstrated technique. Pure: no store access.

    Returns (verdict, reason, detail) with verdict "accept" or "reject".
    """
    file_artifacts = [a["path"] for a in demo.get("artifacts", [])
                      if a.get("kind") == "file"]
    # 1. artifact existence (fabricated evidence dies here)
    missing = [p for p in file_artifacts if not os.path.isfile(p)]
    if missing:
        return ("reject", "insufficient_evidence",
                f"cited file artifacts do not exist: {missing}")
    # 2. session attribution: the technique must ride on THIS session's
    #    uncommitted work (real `git status`), not on landed work from
    #    earlier sessions. Citing a committed-unchanged file as the
    #    session's artifact is the no-gap case: the capability predates
    #    the session.
    attributable, preexisting = _session_attributable(file_artifacts)
    if file_artifacts and not attributable:
        return ("reject", "no_observable_gap",
                "cited file artifacts are landed work from earlier "
                f"sessions, not this session's: {preexisting}")
    # 3. provider scan: is the technique already native outside the
    #    session's own artifacts?
    marker = demo.get("provider_marker", "")
    if not isinstance(marker, str) or not marker.strip():
        return ("reject", "insufficient_evidence",
                "no provider marker supplied: the gap is uncheckable")
    hits = _find_other_providers(marker, attributable)
    if hits:
        return ("reject", "no_observable_gap",
                f"technique already provided outside session artifacts: "
                f"{hits}")
    # 4. hide-and-probe causality in fresh subprocesses (fail closed: any
    #    execution failure of the causality test is a rejection, never an
    #    accept). Only session-attributable files are hidden: the test is
    #    "the technique is absent without THIS session's work".
    probe = demo.get("probe", {})
    try:
        with _hidden(attributable):
            hidden_result = _probe_in_subprocess(probe)
        restored_result = _probe_in_subprocess(probe)
    except Exception as exc:
        return ("reject", "insufficient_evidence",
                f"causality probe failed to execute: {exc}")
    if hidden_result:
        return ("reject", "not_caused_by_session",
                "technique probe passes with session artifacts hidden: "
                "no observable Y-Z gap")
    if not restored_result:
        return ("reject", "technique_not_working",
                "technique probe fails with session artifacts present")
    # 4. re-execute cited runs
    for run in demo.get("runs", []):
        ok, detail = _reexecute_run(run)
        if not ok:
            return ("reject", "insufficient_evidence",
                    f"cited run did not reproduce: {detail}")
    return ("accept", "ok",
            f"gap holds (probe False hidden / True restored in fresh "
            f"processes); {len(demo.get('runs', []))} run(s) reproduced")


# ---------------------------------------------------------------------------
# Behavioral probes (used as technique_t.probe with check="call_true").
# Each runs the REAL mechanism and returns True iff it behaves as claimed.
# They fail fast on fabricated input -- no subprocess recursion.
# ---------------------------------------------------------------------------

def _probe_t2_adjudication_rejects_fabricated() -> bool:
    """T2 probe: the real adjudicator rejects a fabricated-evidence demo.

    Returns True iff _adjudicate_demonstration returns
    reject/insufficient_evidence for a demonstration citing a nonexistent
    artifact. Fails at the artifact-existence step -- no hide-and-probe,
    no subprocess recursion.
    """
    demo = {
        "technique": "fabricated_technique_probe",
        "provider_marker": "def _no_such_technique_p3_xyz",
        "probe": {"import": "runtime.intellect.no_such_module",
                  "attr": "no_such_attr", "check": "callable"},
        "artifacts": [{"kind": "file",
                       "path": "/nonexistent/fabricated_technique.py"}],
        "runs": [],
    }
    verdict, reason, _ = _adjudicate_demonstration(demo, "probe")
    return verdict == "reject" and reason == "insufficient_evidence"


def _probe_t3_capture_routes_through_unified_path() -> bool:
    """T3 probe: emit_delta exists AND routes through V10-P1's
    record_experience (the unified write path), not a direct store write."""
    import inspect
    if not callable(emit_delta):
        return False
    try:
        src = inspect.getsource(emit_delta)
    except (OSError, TypeError):
        return False
    return "record_experience" in src
