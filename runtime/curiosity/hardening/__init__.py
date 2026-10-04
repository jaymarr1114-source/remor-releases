"""Curiosity hardening drills (Phase 6).

Curiosity-domain package. It CANNOT touch enforcement mutation: the
domain-separation guard (swarm_engine.governance.curiosity_enforcement._guard)
refuses any enforcement write from a swarm_engine.curiosity.* frame, at
import time and at call time. Drill harnesses here only ever touch
curiosity-domain stores (checkpoints, evidence, attribution, roll-call
doubles); every enforcement transition happens in the proof driver, run
as __main__ -- the governance-plane caller.

CUR-P6C owns p6c_drill.py in this package (new file; branch cur-p6c).
"""
