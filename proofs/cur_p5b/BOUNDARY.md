# CUR-P5B boundary record (2026-10-04)

No terminal boundary was hit: the mission completed against the
authorized scope. Recorded here are the named limits and the
integration note for future authorized work.

## 1. The as-built score rule stands (documented delta)

`run_controller/controller.py::_persist_terminal` still applies the
Phase-2 score heuristic for questioning terminals. This mission did
not touch it (frozen). The unified pass in `runtime/curiosity/triage/`
supersedes it semantically; wiring the controller to call the pass is
a future authorized integration. The delta (INSUFFICIENT→
propose_investigation, BOUNDARY_ESTABLISHED→boundary) is charter-
grounded and recorded in rules.py + the mission report.

## 2. Store does not enforce the triage vocabulary

`CuriosityFinding.validate()` does not check `provenance.triage`
against the four-value set. A hand-built finding could carry
triage="admitted" and still validate. The guarantee holds by sole-
assigner discipline (this package) + battery, not by store
enforcement. If James wants it hardened, the authorized change is a
one-line vocabulary check in records.py `validate()` (frozen file --
his call, not this mission's).

## 3. No static scan is future-proof

The T07c AST scan proves no CURRENT module auto-admits on triage.
It cannot bind future modules. The durable guarantee is the absence
of any admission API for a future reader to call.

## 4. Triage is origin-agnostic by design

The rules do not read `origin`. Initiated vs Primary-requested
findings triage identically from content. The P5A-path combination
(initiated findings through this pass) is unproven but the pass
needs no change for it.
