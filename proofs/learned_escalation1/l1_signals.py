"""LEARNED-ESCALATION-1: learned signals with heuristic fallback.

- Interface frozen: get_escalation_signal / decide_escalation.
- No producer -> heuristic fallback (fail-closed).
- Producer installed -> learned signal used when confident.
- Misroute: learned disagrees with heuristic, heuristic was right.
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/learned-escalation-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

from swarm_engine.services.escalation_signals import (
    get_escalation_signal, decide_escalation, heuristic_signal,
    set_signal_producer, EscalationSignal)

# No producer: fallback to heuristic.
check("l1_no_producer_none", get_escalation_signal("hello") is None)
esc, sig, used = decide_escalation("hello")
check("l1_fallback_heuristic", not used and sig.source == "heuristic",
      f"source={sig.source}")

# Heuristic triggers on question words.
esc2, sig2, _ = decide_escalation("why is the sky blue and how does it work in detail with many words " * 5)
check("l1_heuristic_escalates", esc2, f"reason={sig2.reason}")

# Install a learned producer.
def fake_producer(prompt):
    return EscalationSignal(should_escalate=True, confidence=0.9,
                            reason="learned:test", source="learned")
set_signal_producer(fake_producer)
esc3, sig3, used3 = decide_escalation("hi")
check("l1_learned_used", used3 and sig3.source == "learned",
      f"source={sig3.source}")
check("l1_learned_decision", esc3 == True)

# Low confidence -> fallback to heuristic.
def weak_producer(prompt):
    return EscalationSignal(should_escalate=True, confidence=0.1,
                            reason="weak", source="learned")
set_signal_producer(weak_producer)
esc4, sig4, used4 = decide_escalation("hi")
check("l1_weak_falls_back", not used4 and sig4.source == "heuristic")

# Clear producer.
set_signal_producer(None)
check("l1_cleared", get_escalation_signal("hi") is None)

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
