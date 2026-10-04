# CUR-P4B boundary record (2026-10-03/04)

## Mission scope
C-9 chunk 4b: disabled-domain + kill paths. The curiosity side only.
`runtime/core/` (Primary side) was read-only throughout (verified: empty
diff).

## Named boundaries (not built here)

1. **Primary-side return channel (standing, from CUR-P4A):**
   the ActivationRequestStore is unbuilt and `runtime/core/` has no
   issuance hook. The KILLED termination is delivered through the defined
   curiosity-side return channel (the request record readable via
   `get_request`), which the Primary track binds. No change from P4A.

2. **No new Primary-side gaps found.** The termination-delivery path the
   mandate anticipated as possibly-missing exists in the defined form
   (request record return channel); no new named boundary was needed.

## Integration-exposed gap repaired (disclosed, in-scope)

`run_controller/controller.py::_execute_kill` did not preserve partial
evidence as INCONCLUSIVE (C-9.4). The ordinary-stop path (P4A) had the
preservation block; the kill path had none. Repaired additively,
mirroring the stop path with kill-appropriate cause naming; kill records
(ST_KILLED, kill_requested, kill ledger notes, outcome="failed")
unchanged. Battery T02/T06 proves the preservation.

## Observed deviations (recorded, not repaired)

1. The frozen origin vocabulary misspells the value
   `"CURIOUSITY_INITIATED"` (`runtime/curiosity/executive/boundary.py`
   ORIGINS). Frozen file; renaming would break landed callers. The
   battery uses the as-built spelling.
2. Design §2 specifies take-up order kill→grant→fit; the landed
   `request_activation` evaluates kill→ownership→grant→fit (inherited
   from CUR-P4A's report). Every refusal is named either way.

## What remains unproven
See the mission report ("What remains unproven" section).
