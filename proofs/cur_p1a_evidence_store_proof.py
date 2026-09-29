#!/usr/bin/env python3
"""CUR-P1A proof battery: the fenced Curiosity Evidence Store (C-7).

Every claim is backed by real execution in this file; nothing is
hard-coded. Run:  python3 proofs/cur_p1a_evidence_store_proof.py
from the repo root. Writes a sibling .log with the same output.

Battery discipline: full battery runs last; the script checks 1-minute
load at start and refuses to run under contention (>2.0), per the mission
packet. Timeout-cascade failures (timeouts -> KeyErrors/flips vanishing
on retry) get ONE clean uncontended re-run before any tree investigation.
"""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

LOAD_THRESHOLD = 2.0

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def load_ok():
    try:
        with open("/proc/loadavg") as fh:
            load1 = float(fh.read().split()[0])
    except OSError:
        load1 = 0.0
    print(f"1-min load: {load1}")
    return load1 <= LOAD_THRESHOLD, load1


def main():
    run_ok, load1 = load_ok()
    if not run_ok:
        print(f"LOAD {load1} > {LOAD_THRESHOLD}: battery refused under "
              "contention per discipline; re-run when the machine is quiet.")
        sys.exit(3)

    from runtime.curiosity.evidence import (
        TERMINAL_STATES, CuriosityFinding, EvidenceProvenance,
        EvidenceRefused, DomainFenceError, CuriosityEvidenceStore,
        CuriosityWriter, LoopCallerStub, terminal_state_parity,
    )

    tmp = tempfile.mkdtemp(prefix="cur_p1a_proof_")
    db = os.path.join(tmp, "curiosity_evidence.db")
    store = CuriosityEvidenceStore(db)
    writer = CuriosityWriter(store)
    stub = LoopCallerStub(writer, loop="questioning", model="stub/phase1")

    # -- 1. vocabulary parity with the Primary enumerated set --------------
    # The Primary package __init__ pulls in engine deps, so read the tuple
    # statically from the real source file (no hard-coded comparison).
    import ast as _ast
    _rel_src = os.path.join(REPO, "runtime", "core", "executive",
                            "relevance.py")
    with open(_rel_src) as _fh:
        _tree = _ast.parse(_fh.read(), _rel_src)
    _pts = None
    for _node in _tree.body:
        if (isinstance(_node, _ast.AnnAssign)
                and isinstance(_node.target, _ast.Name)
                and _node.target.id == "TERMINAL_STATES"):
            _pts = _ast.literal_eval(_node.value)
    assert _pts is not None, "TERMINAL_STATES not found in " + _rel_src
    check("terminal-state vocabulary parity (frozen interface)",
          terminal_state_parity(tuple(_pts)),
          f"curiosity={len(TERMINAL_STATES)} primary={len(_pts)}")

    # -- 2. well-formed write through the real call path --------------------
    f1 = stub.submit_finding("gap-1", "CURIOUSITY_INITIATED",
                             "QUESTION_RESOLVED", "payload://x/1")
    got = store.get(f1.evidence_id)
    check("well-formed write persists through stub->writer->store",
          got is not None and got.as_dict() == f1.as_dict(),
          f"evidence_id={f1.evidence_id[:12]}")

    # -- A1. finding without provenance refused -----------------------------
    n0 = len(store.all())
    bad = CuriosityFinding.new("questioning", "gap-x", "CURIOUSITY_INITIATED",
                               "QUESTION_RESOLVED", None, "payload://x/bad")
    refused = False
    try:
        writer.submit(bad)  # noqa - direct call from __main__ would also fence;
                             # fence is bypassed here? no: see note below
    except (EvidenceRefused, DomainFenceError) as e:
        refused = type(e).__name__
    # NOTE: __main__ is out-of-domain, so this raised DomainFenceError before
    # validation. The EvidenceRefused path is proved properly via the stub:
    check("A1-note: __main__ cannot reach validation (fence fires first)",
          refused == "DomainFenceError", refused)

    # malformed records go through the stub's in-domain raw seam
    refused2 = False
    try:
        stub.submit_raw(bad)
    except EvidenceRefused:
        refused2 = True
    check("A1: finding without provenance refused (EvidenceRefused)",
          refused2)
    check("A1: refused record not persisted", len(store.all()) == n0)

    # -- A2. finding without terminal_state refused -------------------------
    bad2 = CuriosityFinding.new("questioning", "gap-x", "CURIOUSITY_INITIATED",
                                None, EvidenceProvenance("questioning",
                                "gap-x", "stub/phase1"), "payload://x/bad2")
    refused3 = False
    try:
        stub.submit_raw(bad2)
    except EvidenceRefused:
        refused3 = True
    check("A2: finding without terminal_state refused", refused3)
    check("A2: refused record not persisted", len(store.all()) == n0)

    # -- A3. terminal_state outside the enumerated set refused --------------
    bad3 = CuriosityFinding.new("questioning", "gap-x", "CURIOUSITY_INITIATED",
                                "SOME_UNLISTED_STATE",
                                EvidenceProvenance("questioning", "gap-x",
                                                   "stub/phase1"),
                                "payload://x/bad3")
    refused4 = False
    try:
        stub.submit_raw(bad3)
    except EvidenceRefused:
        refused4 = True
    check("A3: unlisted terminal_state refused", refused4)
    check("A3: refused record not persisted", len(store.all()) == n0)

    # -- duplicate evidence_id refused loudly (PLOOP-12 lesson) --------------
    dup_ok = False
    try:
        stub.submit_raw(f1)  # same evidence_id as the stored record
    except ValueError as e:
        dup_ok = "duplicate" in str(e)
    check("duplicate evidence_id refused loudly", dup_ok)

    # -- A4. true-but-irrelevant finding retained fenced ----------------------
    f_true_irrel = stub.submit_finding(
        "gap-unrelated", "CURIOUSITY_INITIATED", "HYPOTHESIS_SUPPORTED",
        "payload://x/irrel", triage="retain")
    retained = store.get(f_true_irrel.evidence_id)
    check("A4: true-but-irrelevant retained (not discarded)",
          retained is not None
          and retained.terminal_state == "HYPOTHESIS_SUPPORTED")
    # no promotion path exists anywhere in the evidence package:
    no_promote = True
    for obj, name in ((store, "store"), (writer, "writer")):
        for attr in ("promote", "present", "publish", "admit", "commit"):
            if hasattr(obj, attr):
                no_promote = False
                print(f"  promotion API found: {name}.{attr}")
    check("A4: no promote/present/publish/admit/commit API in evidence pkg",
          no_promote)
    check("A4: DB holds exactly one table (the fenced curiosity table)",
          store.tables() == ["curiosity_evidence"], str(store.tables()))

    # -- A5. Primary-side write attempt: no writer to call + runtime refusal -
    probe_src = '''
import sys
sys.path.insert(0, %r)
from runtime.curiosity.evidence import CuriosityWriter, CuriosityEvidenceStore, CuriosityFinding, EvidenceProvenance, DomainFenceError
db = sys.argv[1]
st = CuriosityEvidenceStore(db)
w = CuriosityWriter(st)
f = CuriosityFinding.new("questioning", "gap-p", "CURIOUSITY_INITIATED", "BLOCKED",
                         EvidenceProvenance("questioning", "gap-p", "stub/phase1"),
                         "payload://x/probe")
try:
    w.submit(f)
    print("PROBE-WROTE")
except DomainFenceError as e:
    print("PROBE-REFUSED " + type(e).__name__)
''' % REPO
    probe_path = os.path.join(tmp, "primary_probe.py")
    with open(probe_path, "w") as fh:
        fh.write(probe_src)
    n1 = len(store.all())
    out = subprocess.run([sys.executable, "-u", probe_path, db],
                         capture_output=True, text=True, cwd=tmp)
    print("  primary_probe stdout:", out.stdout.strip())
    check("A5: Primary-side module write refused at runtime",
          "PROBE-REFUSED DomainFenceError" in out.stdout, out.stdout.strip())
    check("A5: refused attempt left the store untouched",
          len(store.all()) == n1, f"before={n1} after={len(store.all())}")

    # -- A5b. by construction: no Primary-side import of the writer ---------
    import re
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(REPO, "runtime")):
        if os.path.abspath(root).startswith(
                os.path.abspath(os.path.join(REPO, "runtime", "curiosity"))):
            continue
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8", errors="strict") as fh:
                    src = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(r"from runtime\.curiosity|from \.\.curiosity|"
                         r"import runtime\.curiosity|from \.curiosity", src):
                offenders.append(os.path.relpath(p, REPO))
    check("A5b: no non-curiosity runtime module imports runtime.curiosity",
          not offenders, "; ".join(offenders[:5]) or "clean")

    # -- 3. fresh-process persistence, incl. SIGKILL before close ------------
    child_src = '''
import sys, time
sys.path.insert(0, %r)
from runtime.curiosity.evidence import CuriosityEvidenceStore, CuriosityWriter, LoopCallerStub
st = CuriosityEvidenceStore(sys.argv[1])
w = CuriosityWriter(st)
stub = LoopCallerStub(w, loop="generalization", model="stub/phase1")
f = stub.submit_finding("gap-kill", "PRIMARY_REQUESTED", "NOVELTY_CLASSIFIED", "payload://x/kill")
print("READY " + f.evidence_id, flush=True)
time.sleep(60)
''' % REPO
    child_path = os.path.join(tmp, "killed_writer.py")
    with open(child_path, "w") as fh:
        fh.write(child_src)
    proc = subprocess.Popen([sys.executable, "-u", child_path, db],
                            stdout=subprocess.PIPE, text=True, cwd=tmp)
    ready = proc.stdout.readline().strip()
    eid = ready.split()[1]
    proc.send_signal(signal.SIGKILL)
    rc = proc.wait()
    print(f"  child wrote, then SIGKILLed (rc={rc})")
    fresh = CuriosityEvidenceStore(db)  # fresh process view: new store object
    back = fresh.get(eid)
    check("fresh-process persistence after SIGKILL",
          back is not None and back.terminal_state == "NOVELTY_CLASSIFIED"
          and back.payload_ref == "payload://x/kill",
          f"evidence_id={eid[:12]}")

    # -- 4. no-duplication: distinct DB, no shared tables with acceptance ---
    acc_db = os.path.join(REPO, "runtime", "services", "acceptance.db")
    acc_tables = []
    if os.path.exists(acc_db):
        with sqlite3.connect(acc_db) as c:
            acc_tables = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
    overlap = set(acc_tables) & set(store.tables())
    check("no shared tables with Primary acceptance store",
          not overlap and os.path.abspath(db) != os.path.abspath(acc_db),
          f"acceptance tables={acc_tables}")

    # -- read API is open (Primary Acceptance inspects read-only, Ph4+) ----
    check("read API: all() returns every persisted record",
          len(store.all()) >= 3, f"count={len(store.all())}")

    print()
    failed = [r for r in RESULTS if not r[1]]
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
