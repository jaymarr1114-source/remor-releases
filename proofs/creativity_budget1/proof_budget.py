#!/usr/bin/env python3
"""CREATIVITY-BUDGET-1 evidence battery.

Usage: python3 proof_budget.py --check <name> | --list
Each check runs in its own fresh process with its own scratch sqlite db.
Pass/fail is printed per check; exit 0 iff the check passes.
"""
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

WT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# proofs/creativity_budget1 -> worktree root
WT_ROOT = os.path.dirname(WT_ROOT)
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root

from runtime.creativity.budget import (
    BudgetEnvelope, BudgetHalted, BudgetRefused, CostBound, CostInput,
    CostKind, CreativityBudget, AuthorizationRecord, COMPUTE, MONETARY,
    MEASURE_WALL_CLOCK, MEASURE_FRM_COST_INPUT, propose_envelope,
    ENVELOPE_HEADROOM_FACTOR, SpendLedger,
)
from runtime.curiosity.frm.grant import FrmGrant

SCRATCH = os.path.join(WT_ROOT, "proofs", "creativity_budget1", "_scratch")
os.makedirs(SCRATCH, exist_ok=True)

CHECKS = {}


def check(name):
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco


def fresh_db(tag):
    fd, path = tempfile.mkstemp(prefix=f"budget_{tag}_", suffix=".db",
                                dir=SCRATCH)
    os.close(fd)
    os.unlink(path)  # sqlite creates it; no stale file
    return path


def envelope_compute(bound_s, provenance="test envelope"):
    return BudgetEnvelope(bounds={
        COMPUTE: CostBound(dimension=COMPUTE, unit="seconds", bound=bound_s,
                           measurement=MEASURE_WALL_CLOCK,
                           provenance=provenance),
        MONETARY: CostBound(dimension=MONETARY, unit="currency", bound=None,
                            measurement=MEASURE_FRM_COST_INPUT,
                            provenance="UNPRICED on the bench; UNSET"),
    })


def ok(msg):
    print(f"[PASS] {msg}")


def fail(msg):
    print(f"[FAIL] {msg}")
    return False


@check("grant_within_envelope")
def c_grant():
    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("g"))
    grant = b.request_budget(work_id="w1", estimated_compute_s=100.0,
                             note="test")
    assert isinstance(grant, FrmGrant), type(grant)
    assert grant.domain == "creativity", grant.domain
    assert grant.budget_s == 100.0, grant.budget_s  # margin 0.0 passed
    s = b.spend_summary("w1")
    assert s["grants"] == 1 and s["refusals"] == 0, s
    ok("within-envelope request granted via the real FRM path; recorded")
    return True


@check("refusal_beyond_envelope")
def c_refuse():
    calls = []

    def spy(**kw):
        calls.append(kw)
        raise AssertionError("issuer must not be called on refusal")

    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("r"),
                         grant_issuer=spy)
    try:
        b.request_budget(work_id="w1", estimated_compute_s=1000.0)
        return fail("expected BudgetRefused")
    except BudgetRefused as e:
        msg = str(e)
        assert "600.00" in msg, msg
        assert "authorization" in msg.lower(), msg
    assert calls == [], f"issuer called {len(calls)}x on refusal"
    r = b.ledger.refusals_for("w1")
    assert len(r) == 1 and r[0]["dimension"] == "compute_s", r
    assert b.spend_summary("w1")["spent_compute_s"] == 0.0
    ok("beyond-envelope -> hard refusal naming bound + authorization; "
       "issuer never called; zero spend accrued")
    return True


@check("expansion_authorized")
def c_expand():
    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("e"))
    try:
        b.request_budget(work_id="w1", estimated_compute_s=1000.0)
        return fail("expected refusal before authorization")
    except BudgetRefused:
        pass
    now = time.time()
    b.authorize_expansion(AuthorizationRecord(
        authorized_by="james", dimension=COMPUTE, additional=500.0,
        scope="w1", issued_at=now, expires_at=now + 3600.0))
    grant = b.request_budget(work_id="w1", estimated_compute_s=1000.0)
    assert isinstance(grant, FrmGrant)
    ok("authorized expansion (in-scope, unexpired) -> granted")
    return True


@check("expansion_expired")
def c_expired():
    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("x"))
    now = time.time()
    b.authorize_expansion(AuthorizationRecord(
        authorized_by="james", dimension=COMPUTE, additional=5000.0,
        scope="w1", issued_at=now - 7200.0, expires_at=now - 3600.0))
    try:
        b.request_budget(work_id="w1", estimated_compute_s=1000.0)
        return fail("expired authorization must not lift the refusal")
    except BudgetRefused:
        pass
    ok("expired authorization -> refusal stands")
    return True


@check("expansion_wrong_scope")
def c_scope():
    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("s"))
    now = time.time()
    b.authorize_expansion(AuthorizationRecord(
        authorized_by="james", dimension=COMPUTE, additional=5000.0,
        scope="other-work", issued_at=now, expires_at=now + 3600.0))
    try:
        b.request_budget(work_id="w1", estimated_compute_s=1000.0)
        return fail("out-of-scope authorization must not lift the refusal")
    except BudgetRefused:
        pass
    ok("out-of-scope authorization -> refusal stands")
    return True


@check("ceiling_stop")
def c_ceiling():
    b = CreativityBudget(envelope_compute(100.0), spend_db_path=fresh_db("c"))
    b.request_budget(work_id="w1", estimated_compute_s=90.0)
    assert b.record_spend("w1", 60.0) is None
    stop = b.record_spend("w1", 50.0,  # cumulative 110 >= 100
                          checkpoints={"stage": "variation", "n": 3})
    assert stop is not None, "ceiling did not fire on actuals"
    p = stop.preservation
    for key in ("classification", "consumption_records", "grants",
                "granted_compute_s", "spent_compute_s", "checkpoints"):
        assert key in p, f"preservation missing {key}"
    assert p["classification"].startswith("resource-governance"), p
    assert p["checkpoints"] == {"stage": "variation", "n": 3}, p
    assert p["spent_compute_s"] == 110.0, p
    before = b.spend_summary("w1")["spent_compute_s"]
    try:
        b.record_spend("w1", 1.0)
        return fail("spend after halt must raise BudgetHalted")
    except BudgetHalted:
        pass
    after = b.spend_summary("w1")["spent_compute_s"]
    assert after == before == 110.0, (before, after)
    try:
        b.request_budget(work_id="w1", estimated_compute_s=1.0)
        return fail("request after halt must raise BudgetHalted")
    except BudgetHalted:
        pass
    ok("ceiling hit mid-work -> non-punitive stop with full preservation; "
       "zero further spend accrues; further requests halted")
    return True


@check("estimate_divergence")
def c_diverge():
    b = CreativityBudget(envelope_compute(200.0), spend_db_path=fresh_db("d"))
    b.request_budget(work_id="w1", estimated_compute_s=10.0)  # understated
    stop = b.record_spend("w1", 250.0)  # actuals blow past the estimate
    assert stop is not None, "ceiling must hold on actuals, not estimates"
    s = b.spend_summary("w1")
    assert s["spent_compute_s"] == 250.0 and s["granted_compute_s"] == 10.0, s
    ok("understated estimate: divergence recorded visibly; ceiling held on "
       "actuals (granted 10s, spent 250s, stopped)")
    return True


@check("split_requests_aggregated")
def c_split():
    b = CreativityBudget(envelope_compute(100.0), spend_db_path=fresh_db("p"))
    granted = 0
    refused_at = None
    for i in range(6):
        try:
            b.request_budget(work_id="w1", estimated_compute_s=20.0)
            granted += 1
        except BudgetRefused:
            refused_at = i
            break
    assert granted == 5 and refused_at == 5, (granted, refused_at)
    ok("one work item split into 6x20s requests under one work_id: first 5 "
       "granted (cumulative 100s), 6th refused — per-work_id aggregation "
       "defeats the split")
    return True


@check("split_across_work_ids_bounded")
def c_split_bound():
    # Honest bound: work identity lives with EXEC-1/the run controller.
    # This check documents the boundary rather than hiding it.
    b = CreativityBudget(envelope_compute(100.0), spend_db_path=fresh_db("b"))
    b.request_budget(work_id="w1", estimated_compute_s=100.0)
    b.request_budget(work_id="w2", estimated_compute_s=100.0)  # not refused
    print("[BOUND] cross-work_id splitting is not detectable at this layer: "
          "aggregation is per work_id; work identity is EXEC-1/run-controller "
          "territory. Classified BOUNDED, not hidden.")
    ok("cross-work_id split behavior documented as BOUND")
    return True


@check("monetary_unpriced")
def c_unpriced():
    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("m"))
    grant = b.request_budget(
        work_id="w1", estimated_compute_s=10.0,
        estimated_monetary=CostInput(kind=CostKind.UNPRICED, value=None,
                                    provenance="bench: no price inputs"))
    assert isinstance(grant, FrmGrant)
    b.record_spend("w1", 5.0,
                   monetary=CostInput(kind=CostKind.UNPRICED, value=None,
                                      provenance="bench"))
    conn = sqlite3.connect(b.ledger._db_path)
    try:
        row = conn.execute(
            "SELECT monetary_kind FROM spend WHERE work_id='w1'").fetchone()
        assert row[0] == "UNPRICED", row
    finally:
        conn.close()
    ok("monetary UNPRICED with UNSET bound: carried in records, not enforced, "
       "never fabricated")
    return True


@check("monetary_declared_enforced")
def c_declared():
    env = BudgetEnvelope(bounds={
        COMPUTE: CostBound(dimension=COMPUTE, unit="seconds", bound=600.0,
                           measurement=MEASURE_WALL_CLOCK,
                           provenance="test"),
        MONETARY: CostBound(dimension=MONETARY, unit="currency", bound=50.0,
                            measurement=MEASURE_FRM_COST_INPUT,
                            provenance="synthetic DECLARED bound for the "
                                       "mechanism check only — not a bench "
                                       "number"),
    })
    b = CreativityBudget(env, spend_db_path=fresh_db("md"))
    try:
        b.request_budget(work_id="w1", estimated_compute_s=1.0,
                         estimated_monetary=CostInput(
                             kind=CostKind.DECLARED, value=100.0,
                             provenance="test"))
        return fail("over-bound monetary request must be refused")
    except BudgetRefused as e:
        assert "monetary" in str(e).lower(), str(e)
    grant = b.request_budget(work_id="w1", estimated_compute_s=1.0,
                             estimated_monetary=CostInput(
                                 kind=CostKind.DECLARED, value=10.0,
                                 provenance="test"))
    assert isinstance(grant, FrmGrant)
    ok("declared monetary bound enforced when set (mechanism check); the "
       "bench ships it UNSET")
    return True


@check("malformed_inputs")
def c_malformed():
    b = CreativityBudget(envelope_compute(600.0), spend_db_path=fresh_db("f"))
    for fn, label in [
        (lambda: b.request_budget(work_id="w1", estimated_compute_s=-1.0),
         "negative estimate"),
        (lambda: b.request_budget(work_id="  ", estimated_compute_s=1.0),
         "empty work_id"),
        (lambda: b.record_spend("w1", -5.0), "negative spend"),
        (lambda: propose_envelope({}), "empty measurements"),
        (lambda: SpendLedger(""), "empty db_path"),
        (lambda: SpendLedger(":memory:"), ":memory: db"),
    ]:
        try:
            fn()
            return fail(f"{label}: expected ValueError")
        except ValueError:
            pass
    ok("malformed inputs fail closed (negative/empty/memory-db refused)")
    return True


@check("measured_proposal")
def c_measured():
    batteries = [
        ("slice1", os.path.join(WT_ROOT, "proofs", "creativity_slice1",
                                "gate_run.sh")),
        ("ledger1", os.path.join(WT_ROOT, "proofs", "creativity_ledger1",
                                 "gate_run.sh")),
        ("critique1", os.path.join(WT_ROOT, "proofs", "creativity_critique1",
                                   "gate_run.sh")),
        ("release1", os.path.join(WT_ROOT, "proofs", "creativity_release1",
                                  "gate_run.sh")),
    ]
    for _, script in batteries:
        assert os.path.isfile(script), f"missing battery {script}"
    # Measurement envelope: generous, because this IS the measurement.
    b = CreativityBudget(envelope_compute(14400.0, provenance="measurement"),
                         spend_db_path=fresh_db("mm"))
    # The slice1 battery predates ledger.py: its proof script puts only
    # pylib on sys.path, but the package __init__ now imports ledger,
    # which needs `runtime` importable. Supply the worktree root via
    # PYTHONPATH in the replay environment (harness-side workaround —
    # the battery's own files are another mission's, left untouched;
    # the staleness is surfaced as a repair item in the mission report).
    replay_env = dict(os.environ)
    replay_env["PYTHONPATH"] = (WT_ROOT + os.pathsep +
                                replay_env.get("PYTHONPATH", ""))
    measurements = {}
    for name, script in batteries:
        work_id = f"measure-{name}"
        b.request_budget(work_id=work_id, estimated_compute_s=3600.0,
                         note=f"Phase-1 battery replay: {name}")
        t0 = time.perf_counter()
        proc = subprocess.run(["bash", script],
                              cwd=os.path.dirname(script),
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT,
                              timeout=1800,
                              env=replay_env)
        actual = time.perf_counter() - t0
        out = proc.stdout.decode("utf-8", "replace")
        # Known-stale assertion (surfaced as a track repair item, not
        # hidden): the slice1 battery's scope fence asserts the package
        # holds exactly its three original modules, but the track has
        # since legitimately grown the package (ledger/critique/release/
        # budget per the ownership map). The replay is measurable iff
        # the ONLY failure is that stale scope assertion.
        fail_lines = [l for l in out.splitlines() if l.startswith("[FAIL]")]
        stale_only = (
            proc.returncode != 0
            and len(fail_lines) == 1
            and "package has the three modules" in fail_lines[0]
        )
        if proc.returncode != 0 and not stale_only:
            print(out[-3000:])
            return fail(f"battery {name} exited {proc.returncode} "
                        "during measurement replay")
        if stale_only:
            print(f"  NOTE {name}: 1 known-stale scope assertion failed "
                  "('package has the three modules' — the track has grown "
                  "the package since; surfaced as a repair item); "
                  "workload otherwise complete, timing recorded")
        b.record_spend(work_id, actual)
        measurements[name] = actual
        print(f"  measured {name}: {actual:.2f}s "
              f"(exit {proc.returncode})")
    assert all(v > 0 for v in measurements.values()), measurements
    proposal = propose_envelope(measurements, host_note="bench 2-core")
    derived = math.ceil(max(measurements.values()) * ENVELOPE_HEADROOM_FACTOR)
    assert proposal.bound_for(COMPUTE).bound == float(derived), (
        proposal.bound_for(COMPUTE).bound, derived)
    assert proposal.bound_for(MONETARY).bound is None
    assert "budget.propose_envelope" in \
        proposal.bound_for(COMPUTE).provenance
    print(f"  proposal compute bound: "
          f"{proposal.bound_for(COMPUTE).bound:.0f}s "
          f"(= ceil({max(measurements.values()):.2f} * "
          f"{ENVELOPE_HEADROOM_FACTOR}))")
    print(f"  proposal monetary: UNSET (UNPRICED on the bench)")
    print(f"  provenance: {proposal.bound_for(COMPUTE).provenance}")
    ok("Phase-1 batteries replayed under accounting; real costs recorded; "
       "proposal derivation reproduces exactly from fresh measurements")
    return True


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        name = sys.argv[2]
        if name not in CHECKS:
            print(f"[FAIL] unknown check {name!r}")
            return 1
        t0 = time.perf_counter()
        try:
            passed = CHECKS[name]()
        except Exception as e:  # noqa: BLE001 — battery must not crash
            import traceback
            traceback.print_exc()
            print(f"[FAIL] {name}: raised {type(e).__name__}: {e}")
            return 1
        dt = time.perf_counter() - t0
        print(f"[DONE] {name}: {'PASS' if passed else 'FAIL'} "
              f"({dt:.1f}s)")
        return 0 if passed else 1
    if len(sys.argv) == 2 and sys.argv[1] == "--list":
        for name in CHECKS:
            print(name)
        return 0
    print("usage: proof_budget.py --check <name> | --list")
    return 2


if __name__ == "__main__":
    sys.exit(main())
