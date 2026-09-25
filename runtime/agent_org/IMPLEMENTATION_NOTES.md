# agent_org — Implementation Notes

Lineage: producing agent Grok (xAI), bench Felix.
Work area: `~/workspace/remor_agent_org/runtime_work/agent_org/` (real code),
`~/workspace/remor_agent_org/pylib/swarm_engine/` (import shim: empty
`__init__.py` + one symlink per `runtime_work/` top-level entry),
`~/workspace/remor_agent_org/scratch/` (isolated test sqlite files).
No other file under `runtime_work/` was modified. Stdlib only.

## Files written

| Module | Contents |
|---|---|
| `exceptions.py` | `OrgError`, `AuthorityError`, `LifecycleError`, `ExecutionError`, `VerificationFailed`, `SynthesisError` |
| `store.py` | `OrgStore`: 13 append-only hash-chained tables (`row_digest = sha256(canonical_json(fields) + prev_digest)`, `GENESIS` first row); `insert` / `latest` / `rows` / `history` / `audit` / `audit_all` / `close` |
| `substrates.py` | `Substrate` ABC (`substrate_id` = `kind:name:version`), `CallableSubstrate`, `SymbolicSubstrate` (first-match-wins), `LLMSubstrate` (`run()` always raises `SubstrateUnavailable` — honest ABSENT) |
| `codec_space.py` | `chunk_dedup` / `rle` / `identity` with own framing; every `build()` returns `(code, "selftest")`; generated modules define `encode`/`decode`/`selftest` per the codec contract; `self_check()` round-trip unit checks |
| `discovery.py` | `make_discovery_callable()` (exhaustive grid), `make_discovery_symbolic()` (feature-ordered shortlist, measured winner); both MEASURE every candidate; result dicts carry `implementation`, `entrypoint="selftest"`, `technique`, `params`, `tags`, `io_contract`, `claimed_capabilities`, `measurements`, `notes` |
| `templates.py` | `AgentTemplate`, `TemplateRegistry` (engine-authorized, chained events), seed of `tpl_llm_coder_v1` / `tpl_symbolic_coder_v1` / `tpl_callable_coder_v1` with all 11 contracts |
| `identity.py` | `AgentRecord` (`agt_<16hex of sha256(template\|version\|random)>`) |
| `registry.py` | `AgentRegistry`: full 14-state lifecycle map enforced (`LifecycleError` otherwise); `register` (fresh producer, workspace, chained event); `destroy` (terminal, workspace removed, custody dropped, history kept); `replace_substrate` (AVAILABLE/RETIRED only, chained old/new ids, identity+producer+history preserved) |
| `factory.py` | `AgentFactory.create`: fresh id/workspace/producer per instance, no grants carried; `tpl_llm_coder_v1` without explicit substrate → `SubstrateUnavailable` |
| `assignment.py` | `Assignment` / `AssignmentManager`: `create` (requires AVAILABLE), `activate` (issues per-pattern `capability:use` grants scoped to assignment id), `complete`/`expire`/`revoke` (revoke grants), `require_active` (ACTIVE + agent binding, else `AuthorityError`), `get` |
| `runner.py` | `AgentRunner.execute`: `require_active` → JSON serialization boundary → ASSIGNED→EXECUTING → substrate run → JSON-serializability check → **capability gate** (missing/non-list `claimed_capabilities` or any claim not fnmatching the allow-list → `AuthorityError` + FAILED, fail closed) → content-addressed `WorkProduct` (SUBMITTED), agent EXECUTING→SUBMITTED; in-memory custody map (REMOR holds producer tokens; substrates never see them); provenance `supplier_id`=agent producer, `evaluator`=remor:engine |
| `work_product.py` | `WorkProduct` dataclass + persistence; `self_reported_success` stored, never consulted |
| `subprocess_runner.py` | `run_code(code, entrypoint, cases)`: temp dir, `python3` subprocess, 30s timeout, JSON over stdio, entrypoint called as `entrypoint(**case.args)`; accepts `Case` objects or dicts |
| `review.py` | `ReviewBoard`: `submit_for_review`, `review` (real `IndependentValidator` + subprocess runner + `Arbiter(require_binding=True)` + engine oracle; every case registered as an engine oracle authorized for `"verification"`), `accept`/`reject` (require live `EngineOracleHandle` for `remor:engine`, else `AuthorityError`; chained `ao_manager_decisions` rows), `get_work_product`, `audit_decisions`; review never reads `self_reported_success` (only mentioned in comments) |
| `experience.py` | `ExperienceCandidate`, `OrganizationalExperience`, `ExperienceStore`: `submit_candidate`, `promote` (L1→L2: subprocess + `IndependentValidator` on held-out generality cases, ALL must pass), `record_l3` (pinned signature; chained `ao_manager_decisions` row producer `remor:engine` / `ACCEPT` / kind `synthesis`; L3 row with `derived_from`, `level="L3"`), `list_candidates`, `list_experiences`, `get_experience`, `get_relevant` (L2 only, no code, no agent-private state) |
| `performance.py` | `PerformanceLog`: `record` / `template_stats` (raw counts) / `agent_history`; documented as evidence, not a ranking |
| `synthesis.py` | `RelationshipFinder.find` (contract + tag complementarity + MEASURED X-alone/Y-alone/X∘Y with strict `ratio(X∘Y) < min(ratio(X), ratio(Y))` and exact round-trip; measured Y∘X fallback recorded in evidence), `emit_fused_codec` (deterministic, spec-fields-only, RMZ1 framing, refuses unknown pairs), `OrgSynthesizer.synthesize` (computed token-difflib distinctness gate ≥ 0.40 each + sanity gate), `MIXED_CORPUS`, `tokenize`/`token_similarity` |
| `org.py` | `RemorOrganization.boot(work_dir)` classmethod with all pinned attributes (`.factory`, `.agents`, `.templates`, `.assignments`, `.runner`, `.review`, `.experience`, `.performance`, `.synthesizer`, `.relationships`, `.engine`, `.oregistry`, `.store`, `.work_dir`); `.audit()` (both DBs + decision chain); `.close()` |

## Spec deviations (all deliberate, all documented)

1. **`run_code` returns a `RunReport` object**, not a bare tuple. The real
   `Tester` (`swarm_engine/verification/independent.py`) reads
   `report.ok` / `report.value` / `report.error` attributes; a bare tuple
   would break it. `RunReport` supports attribute access AND tuple
   unpacking.
2. **`selftest` never raises on invalid hex** — it returns
   `{"roundtrip_ok": False, "ratio": -1.0, "error": ...}`. Required by the
   `IndependentValidator`'s novelty-probe stage: probes like
   `"novel0probe"` are not hex, and a raising `selftest` would set
   `generalises=False` and misclassify every genuine codec as a memorised
   table. Refusal is truthful, not silent.
3. **`get_relevant` returns L2 records only** (spec-literal). L3 rows are
   reachable via `get_experience` / `list_experiences`.
4. **`RelationshipFinder` tries Y∘X as a measured fallback** when X∘Y
   fails the strict gate; the order actually measured is recorded in
   `relationship.evidence["order"]`. Everything is measured; nothing is
   asserted.
5. **`synthesize` has an extra sanity gate**: Z must round-trip the
   relationship corpus exactly in-process and its measured aggregate ratio
   must strictly beat both parents, else `SynthesisError`. The driver's
   independent verification remains the admission gate; this only stops us
   emitting code that is broken on its face.
6. **`ExperienceCandidate` / `OrganizationalExperience` carry an extra
   `params` dict** — `emit_fused_codec`'s spec contract requires
   `{technique_name, params, io_contract, tags}`. `record_l3` takes params
   as an explicit operational-metadata argument (default `{}`); it is never
   trust evidence -- trust comes only from the stored synthesis verdict.
7. **Bad (non-JSON-serializable) task → `ExecutionError` with the agent left
   ASSIGNED.** `ASSIGNED → FAILED` is not a legal lifecycle step; marking
   FAILED would raise `LifecycleError` and mask the real error. The agent
   stays reusable.
9b. **Grant revocation failure is surfaced**: `AssignmentManager._close`
   attempts every grant's revocation and raises `OrgError` listing the
   failures after the state transition is honestly recorded — a failed
   revocation never silently passes.
8. **Review `spec` shape**: drivers must supply `.input_names` and
   `.examples` — an `IndependentValidator` requirement, not an `agent_org`
   invention.
9. **Fused architecture** (global lean-RLE pass → chunk-dedup over the RLE
   stream with an explicit chunk table, RMZ1 framing) was chosen by offline
   measurement among candidate fusion designs: the per-chunk-RLE variant
   measured 0.7409 aggregate on `MIXED_CORPUS` (worse than both parents)
   and was discarded. The generator itself is deterministic.

## Unit-test results (`scratch/test_agent_org.py`, 81 checks, ALL PASS)

- Codec: `self_check()` — 7 method×param combos × 9 vectors, exact
  round-trips; `selftest` contract keys; truthful refusal on garbage hex.
- Lifecycle: illegal transitions (`REGISTERED→EXECUTING`, etc.) raise
  `LifecycleError`; double-destroy raises; full legal path exercised.
- Authority: assignment requires AVAILABLE; `activate` issues scoped
  grants (`check_grant("capability:use", "codec:chunk_dedup")` true under
  `codec:*`); `require_active` enforced at `execute`.
- Capability gate: out-of-scope claim (`admin:root` under `codec:rle`)
  → `AuthorityError` + agent FAILED; missing `claimed_capabilities` →
  `AuthorityError` (fail closed); bad task → `ExecutionError`, agent stays
  ASSIGNED.
- Review: `submit_for_review` → `review` → verdict **admitted** (real
  subprocess runs: behavioural + 12 hostile + generalisation + 3 novelty +
  determinism, all oracle-bound); `accept` requires live engine handle
  (`None` → `AuthorityError`); decision chain audits green.
- Learning: candidate → `promote` on held-out generality cases → L2.
- End-to-end: agent A (callable) discovered **rle** (ratio 0.054 on
  byte-run corpus); agent B (symbolic) discovered **chunk_dedup**;
  measured relationship on `MIXED_CORPUS`: x=0.7155, y=0.4356,
  xy=0.4026 (strictly better, exact round-trip), order `x_then_y`;
  `synthesize` → Z (`RMZ1`, `selftest`): token distinctness vs_x=0.599,
  vs_y=0.520 (≥ 0.40, computed), z_ratio=0.4148 < 0.4356;
  `record_l3` + `engine.transition_trust("artifact:codec_z", None,
  "TRUSTED")` → L3 with `derived_from=[exp_x, exp_y]`.
- `replace_substrate`: AVAILABLE→ok (id/producer/history preserved,
  chained old/new ids); wrong state → `LifecycleError`.
- `destroy`: workspace removed, custody dropped, history rows remain,
  experience rows survive (no cascading).
- Revocation sabotage: deleting engine grant rows then `complete()` raises
  `OrgError` (all revocations attempted, failures listed); assignment still
  lands COMPLETED honestly.
- `LLMSubstrate.run` and `factory.create("tpl_llm_coder_v1")` both raise
  `SubstrateUnavailable`.
- Tamper check: direct sqlite mutation of a chained row → `audit`
  reports `row_digest mismatch`; untouched tables stay green.
- Rehydration: fresh `RemorOrganization.boot` on the same dir recovers
  templates, agents, L2/L3 experiences; `get_relevant` exposes no code and
  no agent-private state; full `audit()` green; boot idempotent (3
  template rows, no duplicates).
- `emit_fused_codec` refuses unknown technique pairs and missing spec
  keys with `SynthesisError`.

## Bench-added hardening (2026-09-25, post-implementation review)

- **Verdict→accept binding.** `review()` now persists every verdict to a new
  chained table `ao_review_verdicts` (wp_id, admitted, reasons, bindings);
  `accept()` requires the latest stored verdict for the work product to be
  admitted, else `VerificationFailed`. Previously the review→accept sequence
  was enforced only by REMOR procedure (the driver); now the store refuses
  accept-without-(passing-)review structurally. `reject()` is unchanged
  (rejection needs no positive verdict).
- **Verdict→knowledge binding (mission 2026-09-25, verdict binding).**
  `ExperienceStore` now holds the engine's `ReviewBoard` (attached at boot;
  `promote()`/`record_l3()` fail closed without it) and derives ALL trust
  from persisted `ao_review_verdicts` rows via
  `ReviewBoard.require_admitted_verdict()` -- never from caller input:
  - `promote(candidate_id, generality_cases)` executes the generality
    gauntlet through `review.verify_generality()` (engine-executed,
    verdict stored with `artifact_kind="generality"` bound to the exact
    candidate bytes). The old caller-supplied `validator` parameter is
    GONE: a caller must not choose the oracle that judges its candidate.
    L2 rows record `discovered_by=<agent_id>`, `origin="agent_discovery"`,
    `verdict_execution_id`.
  - `record_l3(..., engine, params=None)` requires a stored admitted
    verdict with `artifact_kind="synthesis"` for `digest(code)` produced
    by the authorized verification procedure, with the verdict chain
    audited on every call. The old caller-supplied `validation_evidence`
    parameter is GONE: a caller cannot manufacture high-trust state by
    constructing evidence claiming verification succeeded. `derived_from`
    ids must all exist. `params` is operational metadata (e.g. synthesis
    measurements), never trust evidence. L3 rows record
    `discovered_by="remor:engine"`, `origin="remor_synthesis"`,
    `verdict_execution_id`.
  - Verdicts are kind-scoped: a "generality" verdict never authorizes L3,
    a "synthesis" verdict never authorizes promotion, a verdict for code X
    never authorizes code Y. Append-only supersede: a later rejected
    verdict for the same digest+kind revokes the earlier admission.
- Driver-visible API differences from the original spec (kept, documented):
  `ExperienceStore.promote()` returns `(True, exp_id)` / `(False, reasons)`
  instead of raising or returning the experience object — fail-closed
  promotion; drivers unpack and call `get_experience(exp_id)`.
  `OracleRegistry.check_grant()` returns `(bool, reason)` tuples (inherited).

- Review `spec` needs `.input_names = ["data_hex"]` and `.examples` as
  `[({"data_hex": hex}, expected_or_None), ...]`; review `cases` should be
  `Case(args={"data_hex": ...}, predicate=...)` checking
  `v["roundtrip_ok"] is True`.
- `promote(candidate_id, generality_cases)` builds its own
  minimal spec from the cases and executes the generality gauntlet through
  the engine's ReviewBoard -- there is no validator parameter; the engine
  chooses the oracle, the driver only supplies the cases (the question).
- Z admission (driver side): verify Z through
  `org.review.verify_artifact(code_z, entry_z, specZ, z_cases,
  artifact_ref="fused_rmz1")` (engine-executed, verdict persisted), then
  `experience.record_l3(..., engine=org.engine, params=rel_ev.get("z_params",
  {}))` and
  `org.engine.transition_trust("artifact:codec_z", None, "TRUSTED", ...)`.

---

## Addendum: Verification Verdict Binding (mission 2026-09-25, Felix bench)

Lineage Grok (xAI). Objective: bind L2 promotion and L3 synthesis admission
to STORED independent verification verdicts, closing boundary K of the
agent-organization mission (`record_l3(validation_evidence=...)` trusted
caller-supplied evidence; `promote(..., validator)` trusted a
caller-selected validator).

### Mechanism

- `ao_review_verdicts` rows are the ONLY trust root for admission. Each row
  is written exclusively by `ReviewBoard._store_verdict()` immediately after
  the real `IndependentValidator` executes, and binds: `code_digest` (exact
  bytes verified), `artifact_kind` (`work_product` | `generality` |
  `synthesis`), `artifact_ref`, `verifier` (constant `VERIFIER_ID`, set only
  by this module — never caller input), `execution_id`, `spec_digest` (the
  verification standard), plus the existing hash chain.
- `ReviewBoard.require_admitted_verdict(code_digest, artifact_kind)` is the
  single trust-derivation point. It (1) audits the verdict chain
  (fail-closed on tamper), (2) takes the LATEST row for the exact digest +
  kind (append-only; a later rejected verdict supersedes an earlier
  admitted one), (3) requires `admitted == "1"`, (4) requires
  `verifier == VERIFIER_ID`. Returns the row; admission paths derive
  `validation_evidence` from it. Anything else raises `VerificationFailed`.
- `ExperienceStore.promote(candidate_id, generality_cases)` — the
  `validator` parameter is GONE. Promotion runs
  `ReviewBoard.verify_generality()` (engine-executed, verdict persisted with
  kind `generality`), then derives L2 from the stored row. Stores
  `discovered_by` (agent id), `origin="agent_discovery"`,
  `verdict_execution_id`.
- `ExperienceStore.record_l3(..., derived_from, engine, params=None)` — the
  `validation_evidence` parameter is GONE. Admission requires: live engine
  handle; non-empty `derived_from` whose every id exists; a stored admitted
  `synthesis` verdict for the exact code digest. `validation_evidence` is
  derived from the stored row (`verdict=admitted`, `execution_id`,
  `spec_digest`, measured `findings`). Stores `discovered_by="remor:engine"`,
  `origin="remor_synthesis"`, `verdict_execution_id`. `params` is explicit
  operational metadata (chunk sizes etc.), never trust evidence.
- `ReviewBoard.verify_artifact(code, entrypoint, spec, cases, artifact_ref)`
  is the engine-executed synthesis-verification path drivers must use before
  `record_l3` (kind `synthesis`). `accept()` additionally checks the stored
  `work_product` verdict's `code_digest` against the CURRENT artifact bytes
  (modified-after-verdict is refused).
- `OrganizationalExperience` gained `discovered_by`, `origin`,
  `verdict_execution_id` (read back from the row in `_experience_from_row`).
- `RemorOrganization.boot` attaches the constructed `ReviewBoard` to the
  `ExperienceStore`; promotion/admission fail closed without it
  (`AuthorityError`).

### What was NOT kept

- An earlier draft of this mission added an `ao_experience_reuse` table and
  reuse-edge writes inside `accept()`, plus a duplicated agent transition.
  It was unproven scope creep (absent from the verified agent-org
  baseline) and introduced a real bug (double ACCEPTED transition raising
  `LifecycleError`); all of it was removed and `accept()` restored to
  baseline + version binding. No `ao_experience_reuse` table exists.

### Honest boundaries (unchanged or narrowed)

- Hash-chain tamper-evidence stops naive row mutation, NOT a whole-DB
  rewrite with recomputed chains (needs external anchoring, absent).
  A forged verdict row with a CORRECTLY recomputed chain is
  indistinguishable from a genuine one at this layer — the binding raises
  the bar from "caller asserts" to "attacker must rewrite the DB", it does
  not remove the inherited whole-DB-rewrite boundary.
- Reasoning substrates remain UNAVAILABLE (`SubstrateUnavailable`).
- 13 oracle families remain unbound (inherited from the oracle-binding
  mission); repair-oracle absent; rollback API absent.
