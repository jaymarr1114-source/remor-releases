"""CUR-P6D drill harness helpers.

Lives inside the curiosity domain package, so it NEVER imports enforcement
mutation modules: the import-time domain-separation guard would (correctly)
refuse. The proof driver -- running as ``__main__`` on the governance plane --
owns every enforcement import and passes constructed objects in.

Contents: a controllable clock, fresh-subprocess state/ledger readers (real
cross-process evidence via the pull-only read API), and a structured attempt
log that records every re-enable attempt -- success and refusal -- with
issuer, level, conditions presented, and outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional


class ControllableClock:
    """Deterministic clock the drill can advance (e.g. past a ban expiry)."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._t = float(start)

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        self._t += float(seconds)
        return self._t

    @property
    def now(self) -> float:
        return self._t


def _reader_code(pylib_dir: str, state_dir: str, what: str) -> str:
    if what == "state":
        expr = (
            "from swarm_engine.governance.curiosity_enforcement.read_api "
            "import read_state; r = read_state(%r); "
            "print(r.state.value if r is not None else 'NONE')"
        ) % (state_dir,)
    elif what == "ledger":
        expr = (
            "from swarm_engine.governance.curiosity_enforcement.read_api "
            "import read_kill_ledger; "
            "print(json.dumps(read_kill_ledger(%r)))" % (state_dir,)
        )
    elif what == "ledger_verify":
        expr = (
            "from swarm_engine.governance.curiosity_enforcement.read_api "
            "import verify_kill_ledger; ok, msg = verify_kill_ledger(%r); "
            "print(('OK' if ok else 'BROKEN') + '|' + msg)" % (state_dir,)
        )
    elif what == "record":
        expr = (
            "from swarm_engine.governance.curiosity_enforcement.read_api "
            "import read_state; r = read_state(%r); "
            "print(json.dumps(r.to_dict()) if r is not None else 'null')"
        ) % (state_dir,)
    else:
        raise ValueError("unknown reader: %r" % (what,))
    prelude = "import sys, json; sys.path.insert(0, %r); " % (pylib_dir,)
    return prelude + expr


def fresh_read(pylib_dir: str, state_dir: str, what: str = "state") -> str:
    """Read enforcement state from a brand-new OS process.

    Real cross-process evidence: the reader subprocess shares nothing with
    the driver except the on-disk state dir.
    """
    code = _reader_code(pylib_dir, state_dir, what)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "fresh reader failed: rc=%d stderr=%s"
            % (proc.returncode, proc.stderr[-2000:])
        )
    return proc.stdout.strip()


class AttemptLog:
    """Every re-enable attempt -- success and refusal -- with full context."""

    def __init__(self) -> None:
        self.attempts: List[Dict[str, Any]] = []

    def record(
        self,
        tag: str,
        from_state: str,
        to_state: str,
        issuer: str,
        reason_refs: Optional[Dict[str, Any]],
        outcome: str,
        detail: str = "",
    ) -> Dict[str, Any]:
        entry = {
            "tag": tag,
            "from_state": from_state,
            "to_state": to_state,
            "issuer": issuer,
            "reason_refs": dict(reason_refs or {}),
            "outcome": outcome,  # "success" | "refused"
            "detail": detail,
            "recorded_at": time.time(),
        }
        self.attempts.append(entry)
        return entry

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.attempts, fh, indent=2, sort_keys=True)

    def refused_tags(self) -> List[str]:
        return [a["tag"] for a in self.attempts if a["outcome"] == "refused"]

    def success_tags(self) -> List[str]:
        return [a["tag"] for a in self.attempts if a["outcome"] == "success"]
