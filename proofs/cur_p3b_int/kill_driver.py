#!/usr/bin/env python3
"""T20 driver A: dispatch a creative exploration, advance one tick, kill it.

Runs in its OWN process. Prints JSON: inquiry_id, checkpoint_id, state.
The process then exits -- real process death. resume_driver.py rebuilds
the stack over the same database files in a fresh process.
Usage: kill_driver.py <run_name>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from creative_stack import (COMMISSION_COVERED, LEDGER_SONG, NEUTRAL_DOCS,
                            creative_trigger, met_roll_call, new_stack)


def main():
    name = sys.argv[1]
    stack = new_stack(name, corpus_docs=[LEDGER_SONG, *NEUTRAL_DOCS])
    met_roll_call(stack)
    ex, rc = stack["ex"], stack["rc"]
    decision = ex.request_activation(creative_trigger())
    assert decision.approved and decision.loop == "creative_exploration"
    inquiry_id = rc.dispatch(decision)
    rc.tick()  # one real tick: MCs spawned, first node operated
    inq = rc._inquiries[inquiry_id]
    assert inq.state == "ACTIVE", inq.state
    result = rc.kill_inquiry(inquiry_id, "t20 adversarial kill")
    print(json.dumps({
        "inquiry_id": inquiry_id,
        "checkpoint_id": result["checkpoint_id"],
        "state": result["state"],
        "lineage_events": len(inq.lineage),
    }))


if __name__ == "__main__":
    main()
