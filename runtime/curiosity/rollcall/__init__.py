"""GAM roll-call channel runtime (CUR-P1E).

Public surface:
  schemas      Challenge / ChallengeResponse / AttestationRecord wire
               schemas (fixed, capability-free, strict validation)
  classifier   MET / MISSED / INVALID classification
  scheduler    policy-bounded, rate-limited challenge issuance
  ledger       append-only, hash-chained attestation ledger with
               >= 6-month retention
  gam          GovernanceAttestationMonitor (attestation-only)
  responder    Phase-1 test-double domain responders
"""

from . import classifier, gam, ledger, responder, scheduler, schemas

__all__ = ["classifier", "gam", "ledger", "responder", "scheduler",
           "schemas"]
