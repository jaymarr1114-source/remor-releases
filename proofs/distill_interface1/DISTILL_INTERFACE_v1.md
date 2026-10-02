# Distill Loop Consumed Interface — FROZEN v1 (2026-10-02)

**Frozen by:** Grower track Phase 6 (DISTILL-INTERFACE-FREEZE-1)
**Consumers:** Production serving track, any future track
**Rule:** Consumers use this interface. They do not redefine it. Changes go through James.

## 1. Acquired-capability registry read contract

Acquired capabilities are identified by the `acquired.` prefix on the operation name.

```python
# Check if a capability is acquired (distilled):
op.startswith("acquired.")  # e.g., "acquired.acq_trace_guided_dlt_c107_8fb6417e2374"

# Read contract:
# - Capabilities are stored via the primitive registry
# - The `acquired.` prefix is the namespace; the suffix is the capability ID
# - Consumers read; they never write to the acquired namespace
```

## 2. Borrow/defer/native routing contract

The distill loop routes through three paths:

| Route | When | Mechanism |
|---|---|---|
| borrow | Teacher demonstrates; loop distills | `DistillationLoop.distill(delta)` |
| defer | Substrate unavailable; wait | Honest deferral with reason |
| native | Capability already acquired | Direct `acquired.*` invocation |

```python
# Routing decision (from escalation_signals.py pattern):
# 1. Check if capability is acquired (native path)
# 2. If not, check if teacher available (borrow path)
# 3. If neither, defer with honest reason
```

## 3. ReviewBoard admission verdicts

All distillations go through ReviewBoard before promotion.

```python
# Admission verdicts:
# - ADMIT: capability verified, promoted to acquired.* namespace
# - REJECT: verification failed, not promoted
# - DEFER: insufficient evidence, retry later

# The verdict is bound to the promotion — no promotion without ADMIT.
# From distill.py: "verdict-bound promotion"
```

## Contract guarantees

1. **No silent promotion:** Every `acquired.*` capability has a ReviewBoard ADMIT verdict.
2. **No namespace collision:** Only the distill loop writes to `acquired.*`.
3. **Honest deferral:** When the teacher is unavailable, the reason is recorded, not hidden.
4. **Traceability:** Every acquired capability traces to its delta record and teacher demonstrations.

## Version history

- v1 (2026-10-02): Initial freeze. Covers registry read, routing, admission.
