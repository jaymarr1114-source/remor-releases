"""Curiosity hardening drills (Phase 6).

Each drill drives the REAL enforcement machinery against a live inquiry and
verifies preservation field-by-field. Nothing here mocks the kill path,
the FRM, or the stores.

Curiosity-domain package. It CANNOT touch enforcement mutation: the
domain-separation guard (swarm_engine.governance.curiosity_enforcement._guard)
refuses any enforcement write from a swarm_engine.curiosity.* frame, at
import time and at call time. Drill harnesses here only ever touch
curiosity-domain stores (checkpoints, evidence, attribution, roll-call
doubles, FRM); every enforcement transition happens in the proof driver,
run as __main__ -- the governance-plane caller.

Ownership:
- CUR-P6A owns the Level 1 (forced ceiling breach) drill.
- CUR-P6B owns the Level 2 drill harness.
- CUR-P6C owns p6c_drill.py (Level 3: severe violation + failed roll-call).
- CUR-P6D owns p6d_drill.py and p6d_curiosity_probe.py (D-3 re-enable matrix).
- CUR-P6E owns the Phase 6 regression orchestration (proofs only).
- CUR-P6F owns p6f_drill.py (fresh-process persistence proof).

This file carries no code -- only the package docstring.
"""
