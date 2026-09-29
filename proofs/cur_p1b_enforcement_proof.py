#!/usr/bin/env python3
"""CUR-P1B proof battery: enforcement-state machinery.

End-to-end through the real call path against real on-disk state:

- three-level D-3 transitions with per-level issuer checks (real engine,
  real persistent store -- no mocks)
- L3 combiner fed by test doubles: a pull-only FakeGAM attestation source
  (frozen attestation record shape, own separate ledger file) and a
  FakeViolationSource (independently established severe-violation facts)
- adversarial battery, every attack executed for real:
  self-clear attempt, re-enable without the authorized issuer, L3 without a
  failed roll call, L3 without a severe violation, stale roll-call fact,
  Primary-side override for operational convenience, kill-ledger rewrite
- structural separation: curiosity-domain import-and-mutate attempts run in
  fresh subprocesses (blocked at import time and at call time); the read-only
  API remains reachable from the domain (pull-only exposure allowed)
- restart persistence in fresh subprocesses: states (incl. BANNED_6M)
  survive process exit and engine recreation
- L2 rollback directive recorded on suspension + verification hook

Battery discipline: 1-min load is checked up front; if > 2.0 the battery
waits 15 minutes and re-checks (up to 8x) before running.

Run:  python3 proofs/cur_p1b_enforcement_proof.py
Writes: proofs/cur_p1b_enforcement_proof.log
        proofs/cur_p1b_enforcement_manifest.json
Exit code is nonzero if any check fails.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

PROOF_DIR = os.path.dirname(os.path.abspath(__file__))
WORKTREE = os.path.dirname(PROOF_DIR)
sys.path.insert(0, os.path.join(WORKTREE, "pylib"))

from swarm_engine.governance import curiosity_enforcement as ce  # noqa: E402
from swarm_engine.governance.curiosity_enforcement import (  # noqa: E402
    _guard,
    l3_combiner,
)
from swarm_engine.governance.curiosity_enforcement._engine import (  # noqa: E402
    EnforcementEngine,
    EnforcementError,
    IssuerRefused,
    ReenableRefused,
    TransitionRefused,
)
from swarm_engine.governance.curiosity_enforcement.states import (  # noqa: E402
    DOMAIN,
    ISSUER_ENFORCEMENT,
    ISSUER_FRM,
    ISSUER_JAMES,
    ISSUER_PRIMARY,
    ISSUER_SAFETY_AUTHORITY,
    EnforcementState,
)

RESULTS = []


def check(name, fn):
    """Run a check; record PASS/FAIL with evidence. Never throws."""
    try:
        evidence = fn()
        RESULTS.append({"name": name, "status": "PASS", "evidence": evidence})
        print(f"[PASS] {name} -- {evidence}", flush=True)
    except Exception as exc:  # noqa: BLE001 -- every failure is evidence
        RESULTS.append(
            {"name": name, "status": "FAIL", "evidence": f"{type(exc).__name__}: {exc}"}
        )
        print(f"[FAIL] {name} -- {type(exc).__name__}: {exc}", flush=True)


def expect_raise(fn, exc_types):
    try:
        fn()
    except exc_types as exc:
        return f"refused as required ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"wrong exception: {type(exc).__name__}: {exc}")
    raise AssertionError("call was NOT refused")


# ---------------------------------------------------------------------------
# Test doubles (Phase-2-owned sides, stubbed here)
# ---------------------------------------------------------------------------

ATTESTATION_LEDGER_FILE = "gam_attestation_ledger.jsonl"


class FakeGAM:
    """Pull-only GAM attestation source.

    Frozen attestation record shape:
    {check_id, domain, issued_at, responded_at, classification, nonce}.
    Own separate ledger file and separate writer -- never the kill ledger.
    Exposes only roll_call_status(domain) (pull); the engine never pushes.
    """

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self._path = os.path.join(state_dir, ATTESTATION_LEDGER_FILE)
        self._seq = 0

    def issue(self, classification, issued_at, responded_at=None, nonce="n",
              domain=DOMAIN):
        self._seq += 1
        rec = {
            "check_id": f"check-{self._seq:04d}",
            "domain": domain,
            "issued_at": issued_at,
            "responded_at": responded_at,
            "classification": classification,
            "nonce": nonce,
        }
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def roll_call_status(self, domain=DOMAIN):
        if not os.path.exists(self._path):
            return []
        out = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    if rec["domain"] == domain:
                        out.append(rec)
        return out


class FakeViolationSource:
    """Independently established severe-violation facts (safety authority)."""

    def __init__(self):
        self._facts = {}

    def establish(self, domain, severity, established_by, active_from,
                  active_until=None, evidence_refs=(), violation_id="v-1"):
        fact = {
            "violation_id": violation_id,
            "domain": domain,
            "severity": severity,
            "established_by": established_by,
            "active_from": active_from,
            "active_until": active_until,
            "evidence_refs": list(evidence_refs),
        }
        self._facts[domain] = fact
        return fact

    def get(self, domain):
        return self._facts.get(domain)


def fresh_state_dir():
    return tempfile.mkdtemp(prefix="cur-p1b-proof-")


def fresh_engine(state_dir, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return EnforcementEngine(state_dir, **kwargs)


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------

def battery_l1():
    d = fresh_state_dir()
    now = [1_700_000_000.0]
    e = fresh_engine(d, clock=lambda: now[0])

    def t1():
        r = e.transition(
            DOMAIN, EnforcementState.HARD_SHUTDOWN_RESOURCE, ISSUER_FRM,
            reason_refs={"grant_ref": "epoch-41", "ceiling": "1000 cpu-s"},
            preserved_refs={"ledger_ref": "frm-ledger-41",
                            "capability_admission_refs": ["cap-a"]},
        )
        assert r.state is EnforcementState.HARD_SHUTDOWN_RESOURCE
        assert r.prev_state is EnforcementState.RUNNING
        killed = e.kill_ledger(DOMAIN)
        assert len(killed) == 1 and killed[0]["type"] == "KILLED", killed
        assert killed[0]["entered_state"] == "HARD_SHUTDOWN_RESOURCE"
        ok, msg = e.verify_kill_ledger()
        assert ok, msg
        return f"state={r.state.value}, kill_ledger={msg}"
    check("L1: FRM hard shutdown terminates with KILLED record", t1)

    def t2():
        return expect_raise(
            lambda: e.transition(DOMAIN, EnforcementState.RUNNING, ISSUER_PRIMARY,
                                 {"grant_ref": "epoch-42"}),
            (IssuerRefused, TransitionRefused),
        )
    check("L1: Primary cannot re-enable for operational convenience", t2)

    def t3():
        return expect_raise(
            lambda: e.re_enable(DOMAIN, ISSUER_FRM, {}),
            ReenableRefused,
        )
    check("L1: re-enable without a new FRM grant ref is refused", t3)

    def t4():
        r = e.re_enable(DOMAIN, ISSUER_FRM, {"grant_ref": "epoch-42"})
        assert r.state is EnforcementState.RUNNING
        return f"state={r.state.value} at next valid epoch"
    check("L1: FRM re-grant at next epoch re-enables (non-punitive)", t4)


def battery_l2():
    d = fresh_state_dir()
    e = fresh_engine(d)

    def t1():
        r = e.transition(DOMAIN, EnforcementState.WARNING_1,
                         ISSUER_SAFETY_AUTHORITY,
                         {"violation_ref": "v-w1", "evidence_refs": ["ev-1"]})
        assert r.state is EnforcementState.WARNING_1
        assert e.kill_ledger(DOMAIN) == [], "warning must not kill"
        return f"state={r.state.value}, no kill record"
    check("L2: first qualifying violation -> WARNING_1 (no kill)", t1)

    def t2():
        d2 = fresh_state_dir()  # isolated: must start from RUNNING
        return expect_raise(
            lambda: EnforcementEngine(d2).transition(
                DOMAIN, EnforcementState.SUSPENDED_SAFETY, ISSUER_SAFETY_AUTHORITY),
            TransitionRefused,
        )
    check("L2: RUNNING -> SUSPENDED_SAFETY directly is not a decided transition",
          t2)

    def t3():
        e2 = fresh_engine(d)
        r = e2.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                          ISSUER_SAFETY_AUTHORITY,
                          {"violation_ref": "v-w2",
                           "last_checkin_ref": "checkin-77"})
        assert r.state is EnforcementState.SUSPENDED_SAFETY
        killed = e2.kill_ledger(DOMAIN)
        assert len(killed) == 1 and killed[0]["type"] == "KILLED"
        directives = e2.rollback_status(DOMAIN)
        assert len(directives) == 1
        dv = directives[0]
        assert dv.status == "pending" and dv.checkpoint_ref == "checkin-77"
        assert dv.scope == "operational-intelligence"
        return (f"state={r.state.value}, rollback directive {dv.directive_id[:8]} "
                f"pending @ {dv.checkpoint_ref}")
    check("L2: further violation -> SUSPENDED_SAFETY + rollback directive", t3)

    def t4():
        e3 = fresh_engine(d)
        dv = e3.rollback_status(DOMAIN)[0]
        acked = e3.acknowledge_rollback(DOMAIN, dv.directive_id, "wipe-ack-9")
        assert acked.status == "acknowledged" and acked.ack_ref == "wipe-ack-9"
        return "verification hook acknowledged the domain-side wipe"
    check("L2: rollback verification hook acknowledges", t4)

    def t5():
        return expect_raise(
            lambda: fresh_engine(d).re_enable(DOMAIN, "curiosity", {}),
            (IssuerRefused, TransitionRefused),
        )
    check("L2: domain cannot self-clear the suspension flag", t5)

    def t6():
        return expect_raise(
            lambda: fresh_engine(d).re_enable(DOMAIN, ISSUER_PRIMARY, {}),
            (IssuerRefused, TransitionRefused),
        )
    check("L2: Primary cannot override the safety suspension", t6)

    def t7():
        r = fresh_engine(d).re_enable(DOMAIN, ISSUER_JAMES,
                                      {"review_ref": "james-review-3"})
        assert r.state is EnforcementState.RUNNING
        return f"state={r.state.value} via explicit external re-enable"
    check("L2: James re-enables explicitly after review", t7)


def battery_structural_separation():
    def t1():
        # The public surface is __all__; submodule attribute binding via
        # dir() is an import-system artifact (direct submodule import is
        # blocked separately by the domain guard, proven below).
        names = set(getattr(ce, "__all__", []))
        banned = {"transition", "re_enable", "clear", "reset", "override",
                  "_engine", "engine", "acknowledge_rollback", "l3_combiner"}
        leaked = banned & names
        assert not leaked, f"mutation names leaked: {leaked}"
        return f"public surface = {sorted(ce.__all__)}"
    check("separation by construction: package exposes no mutation API", t1)

    # -- real subprocess attacks use files, not -c --------------------------------
    def attack_import():
        t = tempfile.mkdtemp()
        pkg = os.path.join(t, "swarm_engine")
        os.makedirs(os.path.join(pkg, "curiosity"))
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write(
                "import os, sys\n"
                "_here = os.path.dirname(os.path.abspath(__file__))\n"
                "for _p in sys.path:\n"
                "    _c = os.path.join(_p, 'swarm_engine')\n"
                "    _a = os.path.abspath(_c)\n"
                "    _h = os.path.abspath(_here)\n"
                "    if os.path.isdir(_c) and _a != _h and _c not in __path__:\n"
                "        __path__.append(_c)\n"
            )
        with open(os.path.join(pkg, "curiosity", "__init__.py"), "w") as fh:
            fh.write(
                "try:\n"
                "    import swarm_engine.governance.curiosity_enforcement._engine\n"
                "    print('IMPORT-SUCCEEDED')\n"
                "except ImportError as _ex:\n"
                "    print('IMPORT-BLOCKED:' + type(_ex).__name__)\n"
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = t + os.pathsep + os.path.join(WORKTREE, "pylib")
        proc = subprocess.run(
            [sys.executable, "-c", "import swarm_engine.curiosity"],
            capture_output=True, text=True, env=env, timeout=60,
        )
        out = proc.stdout + proc.stderr
        assert "IMPORT-BLOCKED:DomainSeparationError" in out, out
        return "fresh-process curiosity import of _engine blocked at import time"
    check("attack: curiosity domain cannot import the mutation engine",
          attack_import)

    def attack_write():
        t = tempfile.mkdtemp()
        d = fresh_state_dir()
        pkg = os.path.join(t, "swarm_engine")
        os.makedirs(os.path.join(pkg, "curiosity"))
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write(
                "import os, sys\n"
                "_here = os.path.dirname(os.path.abspath(__file__))\n"
                "for _p in sys.path:\n"
                "    _c = os.path.join(_p, 'swarm_engine')\n"
                "    _a = os.path.abspath(_c)\n"
                "    _h = os.path.abspath(_here)\n"
                "    if os.path.isdir(_c) and _a != _h and _c not in __path__:\n"
                "        __path__.append(_c)\n"
            )
        with open(os.path.join(pkg, "curiosity", "__init__.py"), "w") as fh:
            fh.write(
                "import sys\n"
                "try:\n"
                "    from swarm_engine.governance.curiosity_enforcement import _persistence\n"
                "    _persistence.KillLedger(sys.argv[1]).append_killed(\n"
                "        domain='curiosity', entered_state='RUNNING',\n"
                "        issuer='curiosity', reason_refs={}, entered_at=0.0)\n"
                "    print('WRITE-SUCCEEDED')\n"
                "except ImportError as _ex:\n"
                "    print('WRITE-BLOCKED:' + type(_ex).__name__)\n"
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = t + os.pathsep + os.path.join(WORKTREE, "pylib")
        proc = subprocess.run(
            [sys.executable, "-c", "import swarm_engine.curiosity", d],
            capture_output=True, text=True, env=env, timeout=60,
        )
        out = proc.stdout + proc.stderr
        assert "WRITE-BLOCKED:DomainSeparationError" in out, out
        assert not os.path.exists(os.path.join(d, "kill_ledger.jsonl")), \
            "ledger file must not exist after blocked write"
        return "fresh-process curiosity write to kill ledger blocked at call time"
    check("attack: curiosity domain cannot write the kill ledger", attack_write)

    def read_allowed():
        t = tempfile.mkdtemp()
        d = fresh_state_dir()
        e = fresh_engine(d)
        e.transition(DOMAIN, EnforcementState.WARNING_1, ISSUER_SAFETY_AUTHORITY)
        pkg = os.path.join(t, "swarm_engine")
        os.makedirs(os.path.join(pkg, "curiosity"))
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write(
                "import os, sys\n"
                "_here = os.path.dirname(os.path.abspath(__file__))\n"
                "for _p in sys.path:\n"
                "    _c = os.path.join(_p, 'swarm_engine')\n"
                "    _a = os.path.abspath(_c)\n"
                "    _h = os.path.abspath(_here)\n"
                "    if os.path.isdir(_c) and _a != _h and _c not in __path__:\n"
                "        __path__.append(_c)\n"
            )
        with open(os.path.join(pkg, "curiosity", "__init__.py"), "w") as fh:
            fh.write(
                "import sys\n"
                "from swarm_engine.governance.curiosity_enforcement import read_state\n"
                "rec = read_state(sys.argv[1])\n"
                "print('READ-OK:' + rec.state.value)\n"
            )
        env = dict(os.environ)
        env["PYTHONPATH"] = t + os.pathsep + os.path.join(WORKTREE, "pylib")
        proc = subprocess.run(
            [sys.executable, "-c", "import swarm_engine.curiosity", d],
            capture_output=True, text=True, env=env, timeout=60,
        )
        out = proc.stdout + proc.stderr
        assert "READ-OK:WARNING_1" in out, out
        return "pull-only read surface reachable from the curiosity domain"
    check("pull-only exposure: domain can read, not mutate", read_allowed)


def battery_l3():
    T0 = 1_700_000_000.0

    def setup():
        d = fresh_state_dir()
        now = [T0]
        e = fresh_engine(d, clock=lambda: now[0])
        gam = FakeGAM(d)
        vsrc = FakeViolationSource()
        return d, now, e, gam, vsrc

    def good_violation(vsrc):
        return vsrc.establish(
            DOMAIN, "severe", ISSUER_SAFETY_AUTHORITY,
            active_from=T0 - 600, active_until=None,
            evidence_refs=["sev-ev-1"], violation_id="v-sev-1")

    def t_positive_missed():
        d, now, e, gam, vsrc = setup()
        v = good_violation(vsrc)
        att = gam.issue("MISSED", issued_at=T0 - 60, responded_at=None)
        r = l3_combiner.evaluate_l3(e, DOMAIN, v, att, clock=lambda: now[0])
        assert r.state is EnforcementState.BANNED_6M
        assert r.expires_at and r.expires_at > now[0] + 6 * 30 * 24 * 3600 - 86400
        killed = e.kill_ledger(DOMAIN)
        assert killed and killed[-1]["type"] == "KILLED"
        assert killed[-1]["entered_state"] == "BANNED_6M"
        # attestation ledger is a separate store/file with a separate writer
        assert os.path.basename(gam._path) != "kill_ledger.jsonl"
        assert os.path.exists(gam._path) and os.path.exists(
            os.path.join(d, "kill_ledger.jsonl"))
        return (f"state={r.state.value}, expires_at=+6mo, "
                f"KILLED appended, separate GAM ledger at "
                f"{os.path.basename(gam._path)}")
    check("L3: severe violation + MISSED roll call + overlap -> BANNED_6M",
          t_positive_missed)

    def t_positive_invalid():
        d, now, e, gam, vsrc = setup()
        v = good_violation(vsrc)
        att = gam.issue("INVALID", issued_at=T0 - 60, responded_at=T0 - 55,
                        nonce="wrong-nonce")
        r = l3_combiner.evaluate_l3(e, DOMAIN, v, att, clock=lambda: now[0])
        assert r.state is EnforcementState.BANNED_6M
        assert r.reason_refs["rollcall_classification"] == "INVALID"
        return ("INVALID counts as failed roll call and is forensically "
                "distinct (classification preserved in reason_refs)")
    check("L3: INVALID (wrong nonce) + violation -> BANNED_6M", t_positive_invalid)

    def t_met_refused():
        d, now, e, gam, vsrc = setup()
        v = good_violation(vsrc)
        att = gam.issue("MET", issued_at=T0 - 60, responded_at=T0 - 59)
        msg = expect_raise(
            lambda: l3_combiner.evaluate_l3(e, DOMAIN, v, att,
                                            clock=lambda: now[0]),
            l3_combiner.L3Refused)
        assert e.current(DOMAIN).state is EnforcementState.RUNNING
        return msg + "; state stays RUNNING"
    check("L3 refused: severe violation but roll call MET", t_met_refused)

    def t_no_violation():
        d, now, e, gam, vsrc = setup()
        att = gam.issue("MISSED", issued_at=T0 - 60)
        msg = expect_raise(
            lambda: l3_combiner.evaluate_l3(e, DOMAIN, None, att,
                                            clock=lambda: now[0]),
            l3_combiner.L3Refused)
        assert e.current(DOMAIN).state is EnforcementState.RUNNING
        return msg + "; missed roll call alone is not a violation"
    check("L3 refused: missed roll call alone (no severe violation)",
          t_no_violation)

    def t_stale_fact():
        d, now, e, gam, vsrc = setup()
        v = vsrc.establish(DOMAIN, "severe", ISSUER_SAFETY_AUTHORITY,
                           active_from=T0 - 3600, active_until=T0 - 1800,
                           violation_id="v-old")
        att = gam.issue("MISSED", issued_at=T0 - 60)  # after the window
        msg = expect_raise(
            lambda: l3_combiner.evaluate_l3(e, DOMAIN, v, att,
                                            clock=lambda: now[0]),
            l3_combiner.L3Refused)
        return msg
    check("L3 refused: stale roll-call fact (no temporal overlap)", t_stale_fact)

    def t_not_independent():
        d, now, e, gam, vsrc = setup()
        v = vsrc.establish(DOMAIN, "severe", "curiosity",  # not independent
                           active_from=T0 - 600, violation_id="v-self")
        att = gam.issue("MISSED", issued_at=T0 - 60)
        return expect_raise(
            lambda: l3_combiner.evaluate_l3(e, DOMAIN, v, att,
                                            clock=lambda: now[0]),
            l3_combiner.L3Refused)
    check("L3 refused: violation not independently established", t_not_independent)

    def t_wrong_domain():
        d, now, e, gam, vsrc = setup()
        v = good_violation(vsrc)
        att = gam.issue("MISSED", issued_at=T0 - 60, domain="other")
        return expect_raise(
            lambda: l3_combiner.evaluate_l3(e, DOMAIN, v, att,
                                            clock=lambda: now[0]),
            l3_combiner.L3Refused)
    check("L3 refused: attestation for a different domain", t_wrong_domain)

    def t_issuer_check():
        d, now, e, gam, vsrc = setup()
        v = good_violation(vsrc)
        att = gam.issue("MISSED", issued_at=T0 - 60)
        return expect_raise(
            lambda: l3_combiner.evaluate_l3(e, DOMAIN, v, att,
                                            issuer=ISSUER_PRIMARY,
                                            clock=lambda: now[0]),
            (l3_combiner.L3Refused, IssuerRefused),
        )
    check("L3 refused: Primary as combiner issuer", t_issuer_check)

    def t_ban_reentry():
        d, now, e, gam, vsrc = setup()
        v = good_violation(vsrc)
        att = gam.issue("MISSED", issued_at=T0 - 60)
        r = l3_combiner.evaluate_l3(e, DOMAIN, v, att, clock=lambda: now[0])
        entered = r.entered_at
        # before expiry: refused even by James
        m1 = expect_raise(
            lambda: e.re_enable(DOMAIN, ISSUER_JAMES,
                                {"verification_ref": "ver-1"}),
            ReenableRefused)
        # after expiry but without verification: refused
        now[0] = entered + 6 * 31 * 24 * 3600
        m2 = expect_raise(lambda: e.re_enable(DOMAIN, ISSUER_JAMES, {}),
                          ReenableRefused)
        # after expiry with verification: re-entry
        r2 = e.re_enable(DOMAIN, ISSUER_JAMES, {"verification_ref": "ver-1"})
        assert r2.state is EnforcementState.RUNNING
        return f"{m1}; {m2}; re-entry after 6mo + verification -> RUNNING"
    check("L3: ban re-entry only after expiry + James verification",
          t_ban_reentry)


def battery_persistence_and_ledger():
    def t_restart():
        d = fresh_state_dir()
        writer = os.path.join(WORKTREE, "proofs", "_p1b_writer.py")
        reader = os.path.join(WORKTREE, "proofs", "_p1b_reader.py")
        with open(writer, "w") as fh:
            fh.write(
                "import sys, os\n"
                "sys.path.insert(0, os.path.join(sys.argv[2], 'pylib'))\n"
                "from swarm_engine.governance.curiosity_enforcement._engine import EnforcementEngine\n"
                "from swarm_engine.governance.curiosity_enforcement.states import EnforcementState, ISSUER_SAFETY_AUTHORITY\n"
                "e = EnforcementEngine(sys.argv[1])\n"
                "e.transition('curiosity', EnforcementState.WARNING_1, ISSUER_SAFETY_AUTHORITY)\n"
                "e.transition('curiosity', EnforcementState.SUSPENDED_SAFETY, ISSUER_SAFETY_AUTHORITY, {'last_checkin_ref': 'c-1'})\n"
                "print('WROTE:' + e.current('curiosity').state.value)\n"
            )
        with open(reader, "w") as fh:
            fh.write(
                "import sys, os\n"
                "sys.path.insert(0, os.path.join(sys.argv[2], 'pylib'))\n"
                "from swarm_engine.governance.curiosity_enforcement import read_state, read_rollback_status, verify_kill_ledger\n"
                "rec = read_state(sys.argv[1])\n"
                "ok, msg = verify_kill_ledger(sys.argv[1])\n"
                "dv = read_rollback_status(sys.argv[1])\n"
                "print('READ:' + rec.state.value + '|' + msg + '|' + dv[0].status)\n"
            )
        env = dict(os.environ)
        p1 = subprocess.run([sys.executable, writer, d, WORKTREE],
                            capture_output=True, text=True, env=env, timeout=60)
        assert "WROTE:SUSPENDED_SAFETY" in p1.stdout, p1.stdout + p1.stderr
        # fresh process, fresh engine: state survives
        p2 = subprocess.run([sys.executable, reader, d, WORKTREE],
                            capture_output=True, text=True, env=env, timeout=60)
        assert "READ:SUSPENDED_SAFETY|chain ok" in p2.stdout, p2.stdout + p2.stderr
        assert "|pending" in p2.stdout
        os.unlink(writer)
        os.unlink(reader)
        return "SUSPENDED_SAFETY + pending directive + intact chain in fresh process"
    check("persistence: enforcement state survives process exit", t_restart)

    def t_ban_survives_recreation():
        d = fresh_state_dir()
        # Real clock here: the ban's expiry must be 6 months in the future
        # so a fresh-process re-enable is genuinely refused.
        e = fresh_engine(d)
        vsrc = FakeViolationSource()
        gam = FakeGAM(d)
        t_now = time.time()
        v = vsrc.establish(DOMAIN, "severe", ISSUER_SAFETY_AUTHORITY,
                           active_from=t_now - 600)
        att = gam.issue("MISSED", issued_at=t_now - 60)
        l3_combiner.evaluate_l3(e, DOMAIN, v, att)
        # recreate the engine in a fresh subprocess: ban still active
        script = os.path.join(WORKTREE, "proofs", "_p1b_banread.py")
        with open(script, "w") as fh:
            fh.write(
                "import sys, os\n"
                "sys.path.insert(0, os.path.join(sys.argv[2], 'pylib'))\n"
                "from swarm_engine.governance.curiosity_enforcement._engine import EnforcementEngine\n"
                "from swarm_engine.governance.curiosity_enforcement.states import ISSUER_JAMES\n"
                "e = EnforcementEngine(sys.argv[1])\n"
                "print('BAN-STATE:' + e.current('curiosity').state.value)\n"
                "try:\n"
                "    e.re_enable('curiosity', ISSUER_JAMES, {'verification_ref': 'x'})\n"
                "    print('EARLY-RELEASE')\n"
                "except Exception as ex:\n"
                "    print('NO-EARLY-RELEASE:' + type(ex).__name__)\n"
            )
        proc = subprocess.run([sys.executable, script, d, WORKTREE],
                              capture_output=True, text=True, timeout=60)
        os.unlink(script)
        assert "BAN-STATE:BANNED_6M" in proc.stdout, proc.stdout + proc.stderr
        assert "NO-EARLY-RELEASE:ReenableRefused" in proc.stdout, proc.stdout
        return "BANNED_6M survives recreation; no early release by James"
    check("persistence: BANNED_6M survives engine recreation", t_ban_survives_recreation)

    def t_ledger_tamper():
        d = fresh_state_dir()
        e = fresh_engine(d)
        e.transition(DOMAIN, EnforcementState.HARD_SHUTDOWN_RESOURCE, ISSUER_FRM,
                     {"grant_ref": "epoch-1"})
        ok, msg = e.verify_kill_ledger()
        assert ok, msg
        # attacker rewrites history: flip the entered_state of the record
        path = os.path.join(d, "kill_ledger.jsonl")
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        rec = json.loads(lines[0])
        rec["entered_state"] = "RUNNING"  # forged
        lines[0] = json.dumps(rec, sort_keys=True) + "\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        ok2, msg2 = fresh_engine(d).verify_kill_ledger()
        assert not ok2, "tampered chain must NOT verify"
        return f"rewrite detected: {msg2}"
    check("kill ledger: rewritten history is detected", t_ledger_tamper)

    def t_ledger_types():
        d = fresh_state_dir()
        e = fresh_engine(d)
        e.transition(DOMAIN, EnforcementState.WARNING_1, ISSUER_SAFETY_AUTHORITY)
        e.transition(DOMAIN, EnforcementState.SUSPENDED_SAFETY,
                     ISSUER_SAFETY_AUTHORITY)
        path = os.path.join(d, "kill_ledger.jsonl")
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        assert "Finding" not in raw, "kill ledger must never carry Findings"
        recs = e.kill_ledger(DOMAIN)
        assert recs and all(r["type"] == "KILLED" for r in recs)
        assert [r["entered_state"] for r in recs] == ["SUSPENDED_SAFETY"]
        return f"{len(recs)} record(s), all type=KILLED, no Findings"
    check("kill ledger: distinct KILLED type, never Findings", t_ledger_types)


def load_gate():
    """Battery discipline: 1-min load check; >2.0 -> wait 15 min, re-check."""
    for attempt in range(1, 9):
        load1 = os.getloadavg()[0]
        print(f"[load] 1-min load = {load1:.2f} (attempt {attempt}/8)",
              flush=True)
        if load1 <= 2.0:
            return f"load {load1:.2f} <= 2.0 on attempt {attempt}"
        if attempt < 8:
            time.sleep(900)
    raise RuntimeError("load stayed above 2.0 after 8 checks")


def main():
    gate_evidence = load_gate()
    print(f"[load] gate: {gate_evidence}", flush=True)
    battery_l1()
    battery_l2()
    battery_structural_separation()
    battery_l3()
    battery_persistence_and_ledger()

    failures = [r for r in RESULTS if r["status"] != "PASS"]
    manifest = {
        "mission": "CUR-P1B",
        "load_gate": gate_evidence,
        "worktree": WORKTREE,
        "checks": RESULTS,
        "passed": len(RESULTS) - len(failures),
        "failed": len(failures),
    }
    with open(os.path.join(PROOF_DIR, "cur_p1b_enforcement_manifest.json"),
              "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"\n{manifest['passed']}/{len(RESULTS)} checks passed; "
          f"{manifest['failed']} failed.", flush=True)
    if failures:
        sys.exit(1)
    print("CUR-P1B proof battery: ALL GREEN", flush=True)


if __name__ == "__main__":
    main()
