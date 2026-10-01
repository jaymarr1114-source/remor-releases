#!/usr/bin/env python3
"""DISTILL-1 mandate 4: the distillation ticket format.

Every borrowed capability REMOR distills gets one of these. Measurable
exit criteria, no vibes. This module defines, validates, and seals
tickets. DISTILL-1's own ticket (distill1/ticket_distill1.json) is the
first demonstration.

Ticket schema:
  ticket_id, capability, teacher{model, revision, provenance},
  student_base{model, source, sha256}, train_set{path, sha256, n},
  held_out_set{path, sha256, n}, delta_records[list of record ids],
  exit_criteria{
    native: "no borrowed cognition calls at student runtime",
    envelope: {max_size_mb, max_ram_mb},
    latency_budget_s,
    held_out: {min_mean, max_clarification_zeros, must_beat_base}
  },
  result{status, measurements{}, evidence{}, sealed_at}
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

REQUIRED = ("ticket_id", "capability", "teacher", "student_base",
            "train_set", "held_out_set", "delta_records", "exit_criteria")
EXIT_REQUIRED = ("native", "envelope", "latency_budget_s", "held_out")
HELDOUT_REQUIRED = ("min_mean", "max_clarification_zeros", "must_beat_base")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def validate(ticket):
    errors = []
    for f in REQUIRED:
        if f not in ticket:
            errors.append(f"missing: {f}")
    ec = ticket.get("exit_criteria", {})
    for f in EXIT_REQUIRED:
        if f not in ec:
            errors.append(f"exit_criteria missing: {f}")
    for f in HELDOUT_REQUIRED:
        if f not in ec.get("held_out", {}):
            errors.append(f"exit_criteria.held_out missing: {f}")
    if not isinstance(ticket.get("delta_records"), list):
        errors.append("delta_records must be a list")
    return errors


def seal(ticket, measurements, evidence):
    """Attach results and seal. Status is computed from exit criteria,
    never hand-set."""
    errors = validate(ticket)
    if errors:
        raise ValueError(f"invalid ticket: {errors}")
    ec = ticket["exit_criteria"]
    ho = ec["held_out"]
    m = measurements
    checks = {
        "native_no_borrow_at_runtime": bool(m.get("native_no_borrow_proven")),
        "envelope": (m.get("size_mb", 1e9) <= ec["envelope"]["max_size_mb"]
                     and m.get("ram_mb", 1e9) <= ec["envelope"]["max_ram_mb"]),
        "latency": m.get("max_turn_s", 1e9) <= ec["latency_budget_s"],
        "held_out_mean": m.get("held_out_mean", 0) >= ho["min_mean"],
        "held_out_clarification_zeros":
            m.get("held_out_clarification_zeros", 1e9) <= ho["max_clarification_zeros"],
        "beats_base": (m.get("held_out_mean", 0) > m.get("base_mean", 1e9))
            if ho["must_beat_base"] else True,
    }
    ticket["result"] = {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "measurements": m,
        "evidence": evidence,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    return ticket


def main():
    # Demonstration: validate DISTILL-1's own ticket file if present.
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "ticket_distill1.json")
    if not os.path.isfile(path):
        print("no ticket file yet (created at seal time)")
        return
    with open(path) as fh:
        t = json.load(fh)
    errors = validate(t)
    if errors:
        print("INVALID:", errors)
        sys.exit(1)
    print(f"ticket {t['ticket_id']}: schema valid; "
          f"status={t.get('result', {}).get('status', 'unsealed')}")


if __name__ == "__main__":
    main()
