#!/usr/bin/env python3
"""CREATIVITY-LEDGER-1 proof battery.

Runs in a fresh process (see gate_run.sh). All fixtures are REAL records in
scratch sqlite stores created by this driver via the stores' own public APIs:
EvidenceStore.add_entry/verify_entry (EVIDENCE-WIRE-1), ProvenanceStore.record/
set_trust, GapRegistry.register. Nothing is asserted into existence.

Two disclosed exceptions: the store APIs offer no unverify/delete operation
(verification is an append-only attested act in the current tree), so the two
check-time adversarial cases flip bits via sqlite directly on the scratch DBs.
This drives the LEDGER (the system under test) into the state; the ledger's
response is what is measured. Both cases print a NOTE saying so.
"""
import os
import sqlite3
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WT_ROOT = os.environ.get("WT_ROOT", os.path.abspath(
    os.path.join(SCRIPT_DIR, "..", "..")))
sys.path.insert(0, WT_ROOT)
sys.path.insert(0, os.path.join(WT_ROOT, "pylib"))  # swarm_engine import root
# (CREATIVITY-INTEGRATE-1, 2026-10-04: the package __init__ now exports
# the executive, whose import chain needs swarm_engine; same repair
# class as the slice1 battery's WT_ROOT fix.)

from runtime.creativity.ledger import (  # noqa: E402
    CompositionVerdict,
    Ledger,
    LedgerEntry,
    LedgerRefused,
)
from runtime.services.evidence import (  # noqa: E402
    EvidenceStore,
    QuarantinedSubstrateRefused,
)
from runtime.governance.provenance import (  # noqa: E402
    Origin,
    ProvenanceRecord,
    ProvenanceStore,
    TrustLevel,
)
from runtime.acquisition.gaps import GapRegistry  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


def expect_raises(name, exc_types, fn, must_contain=""):
    global PASS, FAIL
    try:
        fn()
    except exc_types as e:
        if must_contain and must_contain not in str(e):
            FAIL += 1
            print(f"[FAIL] {name} raised {type(e).__name__} but message "
                  f"missing {must_contain!r}: {e}")
        else:
            PASS += 1
            print(f"[PASS] {name} (raised {type(e).__name__})")
    except Exception as e:  # noqa: BLE001
        FAIL += 1
        print(f"[FAIL] {name} raised wrong exception "
              f"{type(e).__name__}: {e}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} did not raise")


tmp = tempfile.mkdtemp(prefix="creativity_ledger1_")
ev = EvidenceStore(os.path.join(tmp, "evidence.db"))
prov = ProvenanceStore(db_path=os.path.join(tmp, "provenance.db"))
greg = GapRegistry(engine=None, db_path=os.path.join(tmp, "gaps.db"))
ledger = Ledger(ev, prov, gap_registry=greg)

# -- fixture: verified evidence entries naming their primitives -----------
a = ev.add_entry("observation",
                 "gate crossing: primitive 'prim-alpha' admitted "
                 "(CREATIVITY-SLICE-1 battery 61/61)",
                 source="gate:CREATIVITY-SLICE-1@c803a8b")
ev.verify_entry(a["id"], "gate:CREATIVITY-SLICE-1")
b = ev.add_entry("observation",
                 "gate crossing: primitive 'prim-beta' admitted "
                 "(CREATIVITY-SLICE-1 battery 61/61)",
                 source="gate:CREATIVITY-SLICE-1@c803a8b")
ev.verify_entry(b["id"], "gate:CREATIVITY-SLICE-1")
check("fixture evidence entries verified",
      ev.get_substrate_entry(a["id"])["ok"]
      and ev.get_substrate_entry(b["id"])["ok"])

# -- fixture: provenance-sourced primitive at TRUSTED ----------------------
prov.record(ProvenanceRecord(
    capability_id="prim-gamma", origin=Origin.BUILTIN,
    trust=TrustLevel.TRUSTED, source="gate:CUR-P1C",
    events=[{"event": "gate_crossed", "detail": "CUR-P1C 15/15"}]))
check("fixture provenance record at TRUSTED",
      prov.get("prim-gamma").trust == TrustLevel.TRUSTED)

# -- construction ----------------------------------------------------------
e_alpha = LedgerEntry("prim-alpha", "evidence", a["id"],
                      "gate:CREATIVITY-SLICE-1@c803a8b")
e_beta = LedgerEntry("prim-beta", "evidence", b["id"],
                     "gate:CREATIVITY-SLICE-1@c803a8b")
e_gamma = LedgerEntry("prim-gamma", "provenance", "prim-gamma",
                      "gate:CUR-P1C@2d76850")
ledger.add_entry(e_alpha)
ledger.add_entry(e_beta)
ledger.add_entry(e_gamma)
check("entries indexed", ledger.indexed() == ["prim-alpha", "prim-beta",
                                              "prim-gamma"])

expect_raises("construction refused: nonexistent evidence id",
              LedgerRefused,
              lambda: ledger.add_entry(LedgerEntry(
                  "prim-nope", "evidence", 999999, "gate:x")),
              must_contain="no longer resolvable")

c = ev.add_entry("observation", "an unverified note about 'prim-unv'",
                 source="test")
expect_raises("construction refused: unverified evidence entry",
              (LedgerRefused, QuarantinedSubstrateRefused),
              lambda: ledger.add_entry(LedgerEntry(
                  "prim-unv", "evidence", c["id"], "gate:x")))

d = ev.add_entry("observation", "a verified entry about something else",
                 source="gate:x")
ev.verify_entry(d["id"], "gate:x")
expect_raises("construction refused: verified entry not citing primitive",
              LedgerRefused,
              lambda: ledger.add_entry(LedgerEntry(
                  "prim-alpha", "evidence", d["id"], "gate:x")),
              must_contain="does not cite primitive")

expect_raises("construction refused: empty gate reference",
              LedgerRefused,
              lambda: ledger.add_entry(LedgerEntry(
                  "prim-alpha", "evidence", a["id"], "")),
              must_contain="gate reference")

expect_raises("construction refused: unknown verification source",
              LedgerRefused,
              lambda: ledger.add_entry(LedgerEntry(
                  "prim-alpha", "rumor", a["id"], "gate:x")),
              must_contain="unknown verification source")

# -- legality: all verified -----------------------------------------------
v = ledger.check_composition(["prim-alpha", "prim-beta", "prim-gamma"])
check("all-verified composition LEGAL",
      isinstance(v, CompositionVerdict) and v.legal
      and v.checked == ["prim-alpha", "prim-beta", "prim-gamma"]
      and not v.illegal and not v.gaps and not v.absent,
      f"got legal={v.legal} illegal={v.illegal}")

# -- legality: one unverified primitive fails the whole composition --------
v2 = ledger.check_composition(["prim-alpha", "prim-delta"])
check("mixed composition ILLEGAL (totality, no partial credit)",
      not v2.legal
      and len(v2.illegal) == 1
      and v2.illegal[0]["primitive_id"] == "prim-delta"
      and v2.absent == ["prim-delta"],
      f"got {v2}")
check("named gap registered in the real gap machinery",
      len(v2.gaps) == 1 and any(
          "unverified primitive 'prim-delta'" in g.summary
          and g.technique is not None
          and "prim-delta" in g.technique.objective
          for g in greg.list_gaps()),
      f"gaps={v2.gaps}")
check("composition absence recorded visibly",
      v2.absent == ["prim-delta"] and not v2.legal)

# -- gap deduplication on repeated checks ----------------------------------
v3 = ledger.check_composition(["prim-alpha", "prim-delta"])
check("repeated unverified demand reuses the open gap (no spam)",
      v3.gaps == v2.gaps and len(greg.list_gaps()) == 1,
      f"gaps={v3.gaps} total={len(greg.list_gaps())}")

# -- quarantine at add time: raises, never a verdict -----------------------
e2 = ev.add_entry("observation", "quarantined note about 'prim-eps'",
                  source="test")
expect_raises("add_entry on quarantined substrate raises "
              "QuarantinedSubstrateRefused",
              QuarantinedSubstrateRefused,
              lambda: ledger.add_entry(LedgerEntry(
                  "prim-eps", "evidence", e2["id"], "gate:x")),
              must_contain="prim-eps")

# -- quarantine at check time ----------------------------------------------
print("NOTE: EvidenceStore offers no unverify API (verification is an "
      "append-only attested act); the next case flips verified=0 via sqlite "
      "on the scratch DB to exercise the ledger's check-time refusal path.")
z = ev.add_entry("observation",
                 "gate crossing: primitive 'prim-zeta' admitted",
                 source="gate:x")
ev.verify_entry(z["id"], "gate:x")
ledger.add_entry(LedgerEntry("prim-zeta", "evidence", z["id"], "gate:x"))
check("prim-zeta legal before invalidation",
      ledger.check_composition(["prim-zeta"]).legal)
con = sqlite3.connect(os.path.join(tmp, "evidence.db"))
con.execute("UPDATE evidence_entries SET verified=0 WHERE id=?", (z["id"],))
con.commit()
con.close()
expect_raises("check-time quarantined substrate raises "
              "QuarantinedSubstrateRefused (never a verdict)",
              QuarantinedSubstrateRefused,
              lambda: ledger.check_composition(["prim-zeta"]),
              must_contain="prim-zeta")
con = sqlite3.connect(os.path.join(tmp, "evidence.db"))
con.execute("UPDATE evidence_entries SET verified=1 WHERE id=?", (z["id"],))
con.commit()
con.close()
check("prim-zeta legal again after re-verification (live re-resolution)",
      ledger.check_composition(["prim-zeta"]).legal)

# -- adversarial: verification event deleted after indexing -----------------
print("NOTE: EvidenceStore offers no delete API; the next case removes the "
      "row via sqlite on the scratch DB to prove the ledger re-resolves at "
      "check time instead of trusting its index.")
con = sqlite3.connect(os.path.join(tmp, "evidence.db"))
con.execute("DELETE FROM evidence_entries WHERE id=?", (a["id"],))
con.commit()
con.close()
v4 = ledger.check_composition(["prim-alpha"])
check("deleted verification event -> ILLEGAL, element named",
      not v4.legal and len(v4.illegal) == 1
      and v4.illegal[0]["primitive_id"] == "prim-alpha"
      and "no longer resolvable" in v4.illegal[0]["reason"],
      f"got {v4.illegal}")

# -- adversarial: provenance trust dropped after indexing -------------------
prov.set_trust("prim-gamma", TrustLevel.UNKNOWN, "test: simulated regression")
v5 = ledger.check_composition(["prim-gamma"])
check("trust dropped below TRUSTED -> ILLEGAL, reason names the trust",
      not v5.legal and "UNKNOWN" in v5.illegal[0]["reason"]
      and "TRUSTED" in v5.illegal[0]["reason"],
      f"got {v5.illegal}")
prov.set_trust("prim-gamma", TrustLevel.TRUSTED, "test: restored")
check("trust restored -> LEGAL again (no cached trust)",
      ledger.check_composition(["prim-gamma"]).legal)

# -- the ledger is not a verification authority -----------------------------
expect_raises("ledger.verify_entry refused",
              LedgerRefused,
              lambda: ledger.verify_entry(1, "x"),
              must_contain="not a verification authority")
expect_raises("ledger.admit refused",
              LedgerRefused,
              lambda: ledger.admit("prim-x"),
              must_contain="not a verification authority")
leak = [n for n in ("admit", "attest", "override", "verify")
        if hasattr(Ledger, n)]
check("no verification-authority surface on the Ledger class", not leak,
      f"leaked={leak}")

print(f"\n{ PASS}/{PASS + FAIL} PASS")
sys.exit(0 if FAIL == 0 else 1)
