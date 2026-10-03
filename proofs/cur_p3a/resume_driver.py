#!/usr/bin/env python3
"""T20 driver B: resume a killed inquiry in a FRESH process.

Rebuilds the full stack over the SAME database files (fresh objects,
no shared memory with kill_driver.py), resumes the inquiry from its
durable checkpoint, and drives it to terminal. Prints JSON:
inquiry_id, terminal_state, evidence_id, lineage event names.
Usage: resume_driver.py <run_name> <inquiry_id>
"""
import json
import sys

from int_stack import (SUPPORT_DOC, NEUTRAL_DOCS, met_roll_call, new_stack)


def main():
    name, inquiry_id = sys.argv[1], sys.argv[2]
    stack = new_stack(name, corpus_docs=[SUPPORT_DOC, *NEUTRAL_DOCS])
    met_roll_call(stack)
    rc = stack["rc"]
    resumed = rc.resume_inquiry(inquiry_id)
    assert resumed["state"] == "ACTIVE", resumed
    assert resumed["resumed_from"] != "pause", resumed
    for _ in range(600):
        inq = rc._inquiries[inquiry_id]
        if inq.state in ("TERMINATED", "SUSPENDED", "KILLED"):
            break
        rc.tick()
    inq = rc._inquiries[inquiry_id]
    assert inq.result is not None, "inquiry did not stop"
    events = [e.get("event") for e in inq.lineage]
    print(json.dumps({
        "inquiry_id": inquiry_id,
        "terminal_state": inq.result.get("terminal_state"),
        "evidence_id": inq.result.get("evidence_id"),
        "events": events,
    }))


if __name__ == "__main__":
    main()
