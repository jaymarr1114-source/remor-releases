"""Curiosity hardening drills (Phase 6).

Curiosity-domain package. It CANNOT touch enforcement mutation: the
domain-separation guard (swarm_engine.governance.curiosity_enforcement._guard)
refuses any enforcement write from a swarm_engine.curiosity.* frame, at
import time and at call time. Drill harnesses here only ever touch
curiosity-domain stores (checkpoints, evidence, FRM); every enforcement
transition happens in the proof driver, run as __main__ -- the
governance-plane caller.

CUR-P6F owns p6f_drill.py in this package (new file; branch cur-p6f).
LANDING NOTE (2026-10-04): cur-p6a, cur-p6c, cur-p6d each created this
__init__.py with different content on their unlanded branches; Felix
reconciles the four versions at landing (docstring-only; no code).
"""
