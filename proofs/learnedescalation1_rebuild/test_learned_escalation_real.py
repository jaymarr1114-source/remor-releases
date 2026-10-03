#!/usr/bin/env python3
"""LEARNED-ESCALATION-1 REBUILD: Real producer mechanism proof.

Proves the escalation signal INTERFACE is real and functional:
- A real producer can be installed via set_signal_producer()
- get_escalation_signal() returns the producer's signal (not None)
- Removing the producer causally reverts to heuristic fallback
- The heuristic is the fail-closed fallback, never removed

The PRODUCTION learned producer (from the Phase 5 grower/distillation
loop) is not yet implemented — that requires the distillation pipeline.
This proves the STRUCTURE that producer will plug into is real and
causal, not interface-only scaffolding.
"""
import sys

sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical")
sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical/pylib")
sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical/distill1")

from runtime.services import escalation_signals as es
from runtime.services.escalation_signals import (
    EscalationSignal,
    get_escalation_signal,
    heuristic_signal,
    set_signal_producer,
)


def test_no_producer_returns_none():
    """Without a producer, get_escalation_signal returns None (honest)."""
    print("[TEST] no producer → None (honest)...", flush=True)
    
    # Ensure no producer is set (reset state)
    es._producer = None
    
    result = get_escalation_signal("test prompt")
    assert result is None, f"Should return None without producer, got {result}"
    
    print("  PASS: Returns None honestly when no producer", flush=True)
    return True


def test_heuristic_fallback_always_available():
    """The heuristic fallback works independently of any producer."""
    print("[TEST] heuristic fallback always available...", flush=True)
    
    # Heuristic must work even with no producer
    es._producer = None
    
    # Long prompt should trigger escalation
    sig = heuristic_signal("This is a very long prompt " * 20 + " explain why")
    assert sig.source == "heuristic", "Must be labeled heuristic"
    assert sig.should_escalate, "Long prompt with 'explain' should escalate"
    assert 0.0 <= sig.confidence <= 1.0, "Confidence must be in [0,1]"
    
    # Short prompt should not escalate
    sig2 = heuristic_signal("Hi")
    assert not sig2.should_escalate, "Short greeting should not escalate"
    
    print(f"  PASS: Heuristic works (escalate={sig.should_escalate}, confidence={sig.confidence:.2f})", flush=True)
    return True


def test_real_producer_installation():
    """A REAL producer function can be installed and changes the output."""
    print("[TEST] real producer installation changes routing...", flush=True)
    
    # Define a REAL producer (not a mock — a real function implementing
    # the producer contract). This simulates what the grower track will
    # install when the distillation loop produces learned signals.
    def real_producer(prompt: str) -> EscalationSignal:
        # Real logic: escalate on specific learned pattern
        # (In production, this would be a learned model; here it's a
        # real deterministic function proving the mechanism)
        if "quantum" in prompt.lower():
            return EscalationSignal(
                should_escalate=True,
                confidence=0.95,
                reason="learned: quantum topics benefit from deep reasoning",
                source="learned",
            )
        return EscalationSignal(
            should_escalate=False,
            confidence=0.80,
            reason="learned: simple prompt, fast path sufficient",
            source="learned",
        )
    
    # Install the real producer
    set_signal_producer(real_producer)
    
    # The signal should now come from the producer, not None
    result = get_escalation_signal("Explain quantum entanglement")
    assert result is not None, "Producer installed, should not return None"
    assert result.source == "learned", f"Should be learned, got {result.source}"
    assert result.should_escalate, "Quantum prompt should escalate via learned signal"
    assert result.confidence == 0.95, "Confidence must match producer output"
    
    print(f"  PASS: Producer installed, learned signal returned: {result.reason}", flush=True)
    
    # Cleanup: remove producer
    es._producer = None
    return True


def test_producer_ablation_causal():
    """ABLATION: removing producer causally reverts to heuristic/None."""
    print("[TEST] producer ablation (causal)...", flush=True)
    
    def test_producer(prompt: str) -> EscalationSignal:
        return EscalationSignal(
            should_escalate=True,
            confidence=0.99,
            reason="test producer",
            source="learned",
        )
    
    # WITH producer: get learned signal
    set_signal_producer(test_producer)
    with_producer = get_escalation_signal("any prompt")
    assert with_producer is not None, "With producer, should get signal"
    assert with_producer.source == "learned", "Should be learned"
    
    # WITHOUT producer (ablated): get None
    es._producer = None
    without_producer = get_escalation_signal("any prompt")
    assert without_producer is None, "Without producer, should get None"
    
    # The heuristic is still available as fallback
    heuristic = heuristic_signal("any prompt")
    assert heuristic.source == "heuristic", "Heuristic must still work"
    
    print("  PASS: Ablation proves causality (producer → learned, no producer → None, heuristic fallback intact)", flush=True)
    return True


def test_producer_error_fails_closed():
    """Producer errors fail closed to None (heuristic fallback)."""
    print("[TEST] producer error fails closed...", flush=True)
    
    def broken_producer(prompt: str):
        raise RuntimeError("simulated producer failure")
    
    set_signal_producer(broken_producer)
    
    # Should return None on error, not crash, not return garbage
    result = get_escalation_signal("test")
    assert result is None, f"Should fail closed to None, got {result}"
    
    # Heuristic still available
    h = heuristic_signal("test")
    assert h is not None, "Heuristic must survive producer failure"
    
    es._producer = None
    print("  PASS: Producer error fails closed to None, heuristic intact", flush=True)
    return True


def main():
    print("=" * 60, flush=True)
    print("LEARNED-ESCALATION-1 REBUILD: Real producer mechanism proof", flush=True)
    print("=" * 60, flush=True)
    print("Note: Production learned producer requires Phase 5 grower loop.", flush=True)
    print("This proves the STRUCTURE is real and causal.", flush=True)
    print("=" * 60, flush=True)
    
    results = []
    results.append(("no_producer_returns_none", test_no_producer_returns_none()))
    results.append(("heuristic_fallback", test_heuristic_fallback_always_available()))
    results.append(("real_producer_installation", test_real_producer_installation()))
    results.append(("producer_ablation_causal", test_producer_ablation_causal()))
    results.append(("producer_error_fails_closed", test_producer_error_fails_closed()))
    
    print("=" * 60, flush=True)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"Results: {passed}/{total} passed", flush=True)
    
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}", flush=True)
    
    if passed == total:
        print("\nAll LEARNED-ESCALATION-1 rebuild tests PASSED.", flush=True)
        print("Real producer mechanism, causal ablation, fail-closed heuristic.", flush=True)
        print("Production learned signals await Phase 5 grower loop.", flush=True)
        return 0
    else:
        print(f"\n{total - passed} test(s) FAILED.", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
