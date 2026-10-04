"""Curiosity hardening drills (Phase 6).

Each drill drives the REAL enforcement machinery against a live inquiry and
verifies preservation field-by-field. Nothing here mocks the kill path,
the FRM, or the stores. CUR-P6A owns the Level 1 (forced ceiling breach)
drill; later chunks own L2/L3, re-enable, regression, and persistence.

Curiosity-domain package. It CANNOT touch enforcement mutation: the
domain-separation guard (swarm_engine.governance.curiosity_enforcement._guard)
refuses any enforcement write from a swarm_engine.curiosity.* frame, at
import time and at call time. Drill harnesses here only ever touch
curiosity-domain stores (checkpoints, evidence, attribution, roll-call
doubles); every enforcement transition happens in the proof driver, run
as __main__ -- the governance-plane caller.

CUR-P6C owns p6c_drill.py in this package (new file; branch cur-p6c).
"""
