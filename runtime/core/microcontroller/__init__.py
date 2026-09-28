"""Microcontroller substrate -- level 3 of the three-level hierarchy.

Public surface (the frozen interface, microcontroller-interface/v1):
  MicrocontrollerSubstrate, Microcontroller, LoopView, LoopAdmission,
  SpawnResult, Refusal, CognitionProvider, CognitionResult,
  NullCognitionProvider, LOOPS, INTERFACE_VERSION, and the state/refusal
  constants from substrate.

Above the loop level, only LoopView may cross: it carries no microcontroller
ids, purposes, or internals -- enforced by construction.
"""

from .substrate import (
    INTERFACE_VERSION,
    LOOPS,
    LOOP_RUN, LOOP_ACQUISITION, LOOP_EXECUTION, LOOP_ACCEPTANCE,
    LOOP_DISTILLATION, LOOP_GENERALIZATION,
    DEFAULT_MAX_DEPTH, DEFAULT_MAX_CHILDREN,
    MC_ACTIVE, MC_RESOLVED, MC_EXHAUSTED, MC_RETIRED_CASCADE,
    R_DEPTH_CAP, R_ADMISSION_EXHAUSTED, R_CONCURRENCY_CAP, R_CROSS_LOOP,
    R_UNKNOWN_PARENT, R_UNKNOWN_MC, R_ALREADY_RETIRED, R_INVALID_PURPOSE,
    R_INVALID_BUDGET, R_LOOP_UNREGISTERED, R_MC_NOT_ACTIVE,
    R_COGNITION_UNAVAILABLE,
    Refusal, CognitionResult, CognitionProvider, NullCognitionProvider,
    Microcontroller, LoopAdmission, LoopView, SpawnResult,
    MicrocontrollerSubstrate,
)

__all__ = [
    "INTERFACE_VERSION",
    "LOOPS",
    "LOOP_RUN", "LOOP_ACQUISITION", "LOOP_EXECUTION", "LOOP_ACCEPTANCE",
    "LOOP_DISTILLATION", "LOOP_GENERALIZATION",
    "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_CHILDREN",
    "MC_ACTIVE", "MC_RESOLVED", "MC_EXHAUSTED", "MC_RETIRED_CASCADE",
    "R_DEPTH_CAP", "R_ADMISSION_EXHAUSTED", "R_CONCURRENCY_CAP", "R_CROSS_LOOP",
    "R_UNKNOWN_PARENT", "R_UNKNOWN_MC", "R_ALREADY_RETIRED", "R_INVALID_PURPOSE",
    "R_INVALID_BUDGET", "R_LOOP_UNREGISTERED", "R_MC_NOT_ACTIVE",
    "R_COGNITION_UNAVAILABLE",
    "Refusal", "CognitionResult", "CognitionProvider", "NullCognitionProvider",
    "Microcontroller", "LoopAdmission", "LoopView", "SpawnResult",
    "MicrocontrollerSubstrate",
]
