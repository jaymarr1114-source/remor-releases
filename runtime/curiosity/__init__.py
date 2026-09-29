"""Curiosity-side runtime: Phase 1 roll-call channel (CUR-P1E).

Deliberately narrow: this package owns ONLY the GAM roll-call channel
(schemas, scheduler, classifier, append-only attestation ledger,
pull-only exposure, and Phase-1 test-double responders). It does NOT
build the Curiosity Executive, the Run Controller, or any loop -- the
domain-side handler is Phase 2's.
"""
