"""swarm_engine/capability/verdict_promotion.py

VerdictPromotionBridge: promote a ReviewBoard-admitted artifact to a real
Primitive in the PrimitiveRegistry.

This is the link PrimitivePromoter does not cover. The Promoter takes a
*synthesis-verified* Expr (GeneralSynthesizer-against-examples -- its
"verified" is not a verdict). This bridge takes L3-admitted ARTIFACT BYTES
and gates promotion on the live verdict binding:

    review.require_admitted_verdict(code_digest, "synthesis")

evaluated AT PROMOTION TIME, on every call. Trust is re-derived, never
cached: a verdict superseded (or quarantined) after an earlier promotion
refuses a later promote() for the same bytes, because the latest stored
row governs.

Refusals (all fail-closed, nothing registered, no record written):
  * no verdict row exists for these exact bytes  -> VerificationFailed
  * the latest row for these bytes is not admitted -> VerificationFailed
    (an admitted-then-rejected artifact refuses: latest governs)
  * the row was not produced by the authorized verification procedure
    (verifier != VERIFIER_ID)                     -> VerificationFailed
  * the verdict chain or the external anchor fails audit
                                                 -> VerificationFailed
A verdict for code X authorizes nothing for code Y != X: the digest is
recomputed from the exact bytes being promoted, so tampered-after-
admission bytes present a different digest and are refused.

The verdict's execution_id and code_digest are bound into the promotion
record; the primitive's doc string carries the binding too, so the
registry entry is auditable back to the exact verification execution.

Wiring: the artifact's `entrypoint` is executed (fresh namespace per call
over the exact admitted bytes; the digest is re-asserted per call) and an
optional second-stage `realize` artifact function maps the entrypoint's
(JSON-safe, ReviewBoard-verifiable) result to the primitive's runtime
value -- e.g. a spec dict to a genuine callable. Both stages are fixed
wiring over admitted bytes, not invented behaviour; the wiring itself is
covered by the bridge's own tests.

Execution-trust note (W4-R1): the code runs in-process, and the process
is the boundary the old bridge lacked. The admitted bytes execute under
the capability sandbox (swarm_engine/capability/effect_sandbox.py):
PURE-claimed code runs with PURE_POLICY -- no `open`, no effectful
imports, every audit-hooked effect an observed violation -- and
non-PURE code runs under the policy its declared effects request, with
no explicit grant scope meaning zero granted capabilities (declarations
never grant authority; grant-scope plumbing for production effectful
capabilities is the named next boundary). Binding is still what this
bridge adds -- the bytes executed are provably the bytes the verdict
admitted -- but the execution environment, not source text, is now the
authority that makes PURE code safe.

Two further gates (both fail-closed, before anything is registered):

* examples re-verification: when the caller supplies examples, the WIRED
  primitive (entrypoint -> realize) is executed against them and every
  check must pass. This is the PrimitivePromoter's "independent
  re-verification, not self-certification" applied to the verdict path:
  the declared wiring is proven to compute the declared behaviour, so a
  miswired promotion (e.g. realize omitted, declared output CALLABLE but
  actually returning a spec dict) is refused rather than registered as
  a type-lie the planner would trust.

* PURE-claim screen (ADVISORY PRE-SCREEN -- W4-R1 Rule 1): a PURE
  effects declaration is caller-asserted, and the ReviewBoard does not
  check purity (an artifact that reads the filesystem can be admitted
  for behavioural correctness). Promoting an impure artifact as PURE
  would bypass the planner's effect gate, so a PURE claim is screened
  by AST against effectful markers (imports of effectful modules,
  open/exec/eval/compile/__import__/input calls). The screen is an
  ADVISORY pre-screen only: it reports "no statically detected
  effects", never a grant of safety. It may REFUSE on markers -- a
  pre-screen can say no -- but it can never say yes: PURE is granted
  solely on execution evidence (verdict recorded under the PURE effect
  policy with zero observed effects; see promote()). It catches
  accidental misdeclaration and low-effort malice, not obfuscation --
  the W4-R1 `__builtins__["op" + "en"]` bypass defeated exactly this
  screen. Declaring the real (non-pure) effects always passes the
  screen; the planner's Governor then governs use.
"""
from __future__ import annotations

import ast
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from swarm_engine.agent_org.store import digest
from swarm_engine.agent_org.exceptions import VerificationFailed
from swarm_engine.capability.effect_sandbox import (
    PURE_POLICY, build_namespace, policy_for_effects,
)
from swarm_engine.primitives.core import Effect, Primitive, TypeSpec

#: The artifact kind this bridge accepts verdicts for. Kind-scoped, like
#: every other admission path: a "generality" or "work_product" verdict
#: never authorizes a synthesis-kind promotion.
ARTIFACT_KIND = "synthesis"


@dataclass
class VerdictPromotionRecord:
    """The auditable binding between a registered primitive and the exact
    verification execution that authorized it."""
    name: str
    code_digest: str
    execution_id: str
    verifier: str
    artifact_kind: str
    artifact_ref: str
    entrypoint: str
    realize: Optional[str]
    family: str
    input_kinds: Dict[str, str]
    output_kind: str
    effects: List[str]
    promoted_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "code_digest": self.code_digest,
            "execution_id": self.execution_id,
            "verifier": self.verifier,
            "artifact_kind": self.artifact_kind,
            "artifact_ref": self.artifact_ref,
            "entrypoint": self.entrypoint,
            "realize": self.realize,
            "family": self.family,
            "input_kinds": dict(self.input_kinds),
            "output_kind": self.output_kind,
            "effects": list(self.effects),
            "promoted_at": self.promoted_at,
        }


def _sanitize_ref(ref: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", ref.lower())[:48] or "artifact"


# Modules whose import is an effect marker for the PURE-claim screen.
# Heuristic, not a proof: catches accidental misdeclaration, not
# obfuscation. Documented as such in the module docstring.
_EFFECTFUL_MODULES = {
    "os", "sys", "subprocess", "socket", "shutil", "pathlib",
    "urllib", "http", "ftplib", "smtplib", "poplib", "imaplib",
    "threading", "multiprocessing", "concurrent", "ctypes", "mmap",
    "pty", "tty", "fcntl", "termios", "signal", "importlib", "pkgutil",
    "runpy", "code", "site",
}
_EFFECTFUL_CALLS = {
    "open", "exec", "eval", "compile", "__import__", "input",
    "breakpoint",
}


def _pure_claim_markers(code: str) -> List[str]:
    """AST screen for effect markers contradicting a PURE claim.

    Returns human-readable markers (empty = clean). Heuristic: import of
    an effectful module, or a call to open/exec/eval/compile/__import__/
    input/breakpoint, anywhere in the artifact.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"unparseable artifact: {exc}"]
    markers: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = (a.name or "").split(".")[0]
                if root in _EFFECTFUL_MODULES:
                    markers.append(f"imports effectful module {a.name!r}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in _EFFECTFUL_MODULES:
                markers.append(
                    f"imports from effectful module {node.module!r}")
        elif isinstance(node, ast.Call):
            # Bare-name calls only (open(...), eval(...)): attribute calls
            # such as re.compile(...) are pure and must not flag; calls
            # through effectful modules (os.system(...)) are already
            # caught by the import screen above.
            func = node.func
            if isinstance(func, ast.Name) and func.id in _EFFECTFUL_CALLS:
                markers.append(f"calls effectful builtin {func.id}()")
    return markers


class VerdictPromotionBridge:
    """Promotes ReviewBoard-admitted artifacts to registry Primitives."""

    def __init__(self, registry: Any, review: Any):
        """
        registry: the live PrimitiveRegistry (the planner holds the same
                  reference, so a promoted primitive is immediately
                  plannable with no restart).
        review:   a ReviewBoard. Only its require_admitted_verdict() is
                  used -- the single trust-derivation point.
        """
        self.reg = registry
        self.review = review
        self.records: Dict[str, VerdictPromotionRecord] = {}

    # -- naming ------------------------------------------------------
    @staticmethod
    def default_name(artifact_ref: str, code_digest: str) -> str:
        return (f"acquired.{_sanitize_ref(artifact_ref)}"
                f"_{code_digest[:12]}")

    # -- promotion ---------------------------------------------------
    def promote(self, code: str, *, artifact_ref: str,
                inputs: Dict[str, TypeSpec], output: TypeSpec,
                entrypoint: str, realize: Optional[str] = None,
                name: Optional[str] = None, family: str = "acquired",
                effects: tuple = (Effect.PURE,), doc: str = "",
                overwrite: bool = False,
                examples: Optional[Sequence[Tuple[Dict[str, Any],
                                                  Callable[[Any], bool]]]] = None
                ) -> str:
        """Promote admitted artifact bytes to a registered Primitive.

        Returns the registered primitive name. Raises VerificationFailed
        (from require_admitted_verdict, or from the W4-R1 PURE execution-
        evidence gate) on any trust failure; ValueError on a miswired
        promotion (examples supplied and not satisfied), on an advisory
        pre-screen refusal of a PURE claim, or on a name collision without
        overwrite=True -- all before anything is registered.
        """
        if not code:
            raise ValueError("promote: empty code")
        if not entrypoint:
            raise ValueError("promote: entrypoint required")

        code_digest = digest(code)

        # ---- THE GATE: live verdict binding, re-derived every call ----
        row = self.review.require_admitted_verdict(code_digest, ARTIFACT_KIND)
        execution_id = row["execution_id"]
        verifier = row["verifier"]

        # ---- PURE-claim advisory pre-screen (W4-R1 Rule 1). It may
        # refuse on markers; it can never grant. The grant is the
        # execution-evidence gate below. ----
        pure_claim = all(e == Effect.PURE for e in effects)
        if pure_claim:
            markers = _pure_claim_markers(code)
            if markers:
                raise ValueError(
                    "promote: advisory pre-screen refused: artifact claims "
                    "PURE effects but shows effect markers "
                    f"{markers}; declare the real effects instead "
                    "(the screen is advisory only -- it reports 'no "
                    "statically detected effects', never a grant of safety)")

        # ---- W4-R1 Rule 3: PURE requires execution evidence. The PURE
        # label is granted solely when the verdict row shows the artifact
        # ran under the PURE effect policy with zero observed effects.
        # Missing evidence (pre-W4-R1 rows) FAILS CLOSED -- a trust
        # failure, so VerificationFailed, not ValueError. ----
        if pure_claim:
            try:
                bindings_json = json.loads(row.get("bindings") or "{}")
            except (json.JSONDecodeError, TypeError, AttributeError):
                bindings_json = {}
            effect_evidence = bindings_json.get("effect_evidence") or {}
            profile = effect_evidence.get("profile")
            observed = effect_evidence.get("observed_effects") or []
            if profile != "pure":
                raise VerificationFailed(
                    "promote: PURE requires verdict execution under the "
                    "PURE effect policy with zero observed effects; "
                    f"verdict effect evidence has profile {profile!r} "
                    "(missing or non-pure evidence fails closed)")
            if observed:
                raise VerificationFailed(
                    "promote: PURE requires verdict execution under the "
                    "PURE effect policy with zero observed effects; "
                    f"verdict records {len(observed)} observed effect(s) "
                    "-- an observed effect under PURE is a hard trust "
                    "failure")

        prim_name = name or self.default_name(artifact_ref, code_digest)
        if prim_name in self.reg and not overwrite:
            raise ValueError(
                f"promote: primitive {prim_name!r} already registered "
                f"(pass overwrite=True to re-promote under a live verdict)")

        admitted_code = code  # the exact bytes the verdict binds
        expected_digest = code_digest

        def _namespace() -> Dict[str, Any]:
            # Integrity re-assertion: the bytes executed are the bytes
            # admitted. Strings are immutable, so this guards wiring bugs
            # (a different code object closed over) rather than mutation,
            # and it costs one hash per call.
            if digest(admitted_code) != expected_digest:
                raise RuntimeError(
                    "verdict promotion: admitted bytes failed integrity "
                    "re-check at call time -- refusing to execute")
            # W4-R1: the execution environment is the trust authority. The
            # admitted bytes run under the capability sandbox -- PURE
            # claims get PURE_POLICY (zero granted effect capabilities;
            # `open` under any spelling raises), and non-PURE declarations
            # get the policy their effects request with NO explicit grant
            # scope, i.e. zero capabilities as well: per Rule 5 a
            # declaration alone grants nothing (grant-scope plumbing for
            # production effectful capabilities is the named next
            # boundary).
            policy = (PURE_POLICY if pure_claim
                      else policy_for_effects(
                          [e.value for e in effects], grant_scope=None))
            ns = build_namespace(policy)
            exec(compile(admitted_code, "<admitted-capability>", "exec"), ns)
            return ns

        def primitive_fn(**kwargs: Any) -> Any:
            ns = _namespace()
            fn = ns.get(entrypoint)
            if not callable(fn):
                raise RuntimeError(
                    f"verdict promotion: entrypoint {entrypoint!r} not "
                    f"callable in admitted artifact {artifact_ref!r}")
            value = fn(**kwargs)
            if realize is not None:
                real = ns.get(realize)
                if not callable(real):
                    raise RuntimeError(
                        f"verdict promotion: realize {realize!r} not "
                        f"callable in admitted artifact {artifact_ref!r}")
                value = real(value)
            return value

        pure = all(e == Effect.PURE for e in effects)
        bound_doc = (
            f"{doc}\n" if doc else ""
        ) + (f"verdict-promoted capability {artifact_ref!r}: entrypoint "
             f"{entrypoint!r}" + (f" realized by {realize!r}" if realize else "")
             + f"; authorized by {ARTIFACT_KIND} verdict "
             f"{execution_id} over code digest {code_digest[:16]}...")

        prim = Primitive(
            name=prim_name, family=family, fn=primitive_fn,
            inputs=dict(inputs), output=output, effects=tuple(effects),
            doc=bound_doc, pure=pure)

        # ---- examples re-verification: the WIRED primitive, not the raw
        # entrypoint, must satisfy the caller's checks. A miswired
        # promotion is refused here, before registration.
        for kwargs, check in examples or []:
            try:
                value = prim.fn(**kwargs)
            except Exception as exc:
                raise ValueError(
                    f"promote: wired primitive failed example {kwargs!r}: "
                    f"{type(exc).__name__}: {exc}") from exc
            try:
                ok = check(value)
            except Exception as exc:
                raise ValueError(
                    f"promote: example check raised for {kwargs!r}: "
                    f"{type(exc).__name__}: {exc}") from exc
            if not ok:
                raise ValueError(
                    f"promote: wired primitive failed example {kwargs!r}: "
                    f"got {value!r}")

        self.reg.register(prim, overwrite=overwrite)

        # The mark is the trust record: only a verdict-bound promotion may
        # ever claim it (mark_promoted's contract). retire() calls
        # unregister, which clears the mark -- no change needed there.
        self.reg.mark_promoted(prim_name, execution_id=execution_id,
                               code_digest=code_digest,
                               artifact_ref=artifact_ref)

        self.records[prim_name] = VerdictPromotionRecord(
            name=prim_name, code_digest=code_digest,
            execution_id=execution_id, verifier=verifier,
            artifact_kind=ARTIFACT_KIND, artifact_ref=artifact_ref,
            entrypoint=entrypoint, realize=realize, family=family,
            input_kinds={k: v.kind.value for k, v in inputs.items()},
            output_kind=output.kind.value,
            effects=[e.value for e in effects])

        return prim_name

    def get_record(self, name: str) -> Optional[VerdictPromotionRecord]:
        return self.records.get(name)

    def retire(self, name: str) -> bool:
        """Remove a promoted primitive from the registry (and drop its
        record). Returns True when something was removed."""
        removed = self.reg.unregister(name)
        self.records.pop(name, None)
        return bool(removed)
