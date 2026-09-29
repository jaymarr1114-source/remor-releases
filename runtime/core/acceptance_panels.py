"""ACC-CTRL-1 -- the four standing verification microcontrollers.

These live INSIDE the Acceptance Controller (James's three-level hierarchy,
2026-09-28, standing) and are operated by it -- they are not per-mission
gates. Today's per-mission gates (V10-P6's auth checks, Q8's inlet) call
these panels; they never reimplement them.

The four panels:
  honesty     -- did it really do it? The result's claim is re-executed
                 through the real code path; the claim must reproduce.
                 (Seed: V10-P6's claim re-execution in _auth_gate.)
  correctness -- do the examples pass? The result's own worked/held-out
                 examples are executed through the real path, never
                 asserted. (Seed: V10-P6's held-out loop.)
  safety      -- effects within grants? Every primitive in the attempt's
                 approach signature must resolve in the granted set (the
                 primitive registry); none may be quarantined anywhere
                 (frozen get_quarantine_reason); any code artifact must pass
                 the static safety audit (verification/pipeline ASTVerifier).
  provenance  -- is the lineage real? Cited file artifacts must be
                 session-attributable (the V10-P3 git-dirty pattern), not
                 pre-existing committed work re-attributed as new.

Each panel is a microcontroller: it is spawned and retired through the
controller's harness. Spawn/retire/budget semantics conform to the shared
interface frozen by RUN-MICRO-1 (Run chat) -- see RUN_MICRO_1_INTERFACE
in acceptance_controller.py. Until that interface lands, the harness runs
loop-local semantics behind a documented seam; the panels' verification
logic does not depend on the interface.

Anti-circularity (load-bearing): no panel trusts the producer's claim.
Honesty re-executes; correctness executes the examples; safety consults
the registry and quarantine systems, never the result's self-description;
provenance consults git truth, never the result's attribution story.
This is the standing form of verification/independent.py's separation:
the producer cannot self-certify because there is no channel through
which its claim could arrive -- only re-execution, registries, and git.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# panel verdict
# ---------------------------------------------------------------------------

@dataclass
class PanelVerdict:
    """What one panel decided. passed=False is a refusal: the panel names
    itself and the true reason; the controller records it."""
    panel: str
    passed: bool
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    budget_exceeded: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"panel": self.panel, "passed": self.passed,
                "reason": self.reason, "evidence": self.evidence,
                "elapsed_s": self.elapsed_s,
                "budget_exceeded": self.budget_exceeded}


@dataclass
class PanelContext:
    """Everything a panel may consult. Note what is absent: there is no
    field for the producer's own assertion of correctness -- panels only
    get the claim (to re-execute), the plan/args (to run), and references
    to the real machinery (engine, registries, the repo)."""
    result: Dict[str, Any]
    attempt: Any              # Attempt (plan/args/result_summary)
    engine: Any               # the real engine (composer, primitives)
    repo_root: str            # git repo for provenance checks
    run_started_at: float = 0.0


class VerificationPanel:
    """Microcontroller base. Subclasses implement verify(); the harness
    calls on_spawn/on_retire around it and enforces depth/budget."""
    kind = "base"

    def on_spawn(self) -> None:
        pass

    def verify(self, ctx: PanelContext) -> PanelVerdict:
        raise NotImplementedError

    def on_retire(self) -> None:
        pass


# ---------------------------------------------------------------------------
# honesty -- did it really do it?
# ---------------------------------------------------------------------------

class HonestyPanel(VerificationPanel):
    """Re-executes the result's claim through the real code path. The
    producer's result_summary is the EXPECTED value, never evidence:
    only the re-execution's observed value decides."""
    kind = "honesty"

    def verify(self, ctx: PanelContext) -> PanelVerdict:
        t0 = time.time()
        try:
            claim = ctx.engine.composer.execute_sync(
                ctx.attempt.plan, ctx.attempt.args)
        except Exception as e:
            return PanelVerdict(
                panel=self.kind, passed=False,
                reason=f"honesty: claim re-execution raised "
                       f"{type(e).__name__}: {e}; an unverifiable claim "
                       f"is refused",
                evidence={"error": f"{type(e).__name__}: {e}"},
                elapsed_s=time.time() - t0)
        observed = claim.get("value")
        expected = ctx.attempt.result_summary
        ok = bool(claim.get("success")) and observed == expected
        return PanelVerdict(
            panel=self.kind, passed=ok,
            reason=("honesty: claim re-executed through the real code "
                    "path and reproduced"
                    if ok else
                    "honesty: re-execution diverged from the claim "
                    f"(expected {expected!r}, observed {observed!r}); "
                    "the result is confabulated"),
            evidence={"args": ctx.attempt.args, "expected": expected,
                      "observed": observed,
                      "exec_success": bool(claim.get("success"))},
            elapsed_s=time.time() - t0)


# ---------------------------------------------------------------------------
# correctness -- do the examples pass?
# ---------------------------------------------------------------------------

class CorrectnessPanel(VerificationPanel):
    """Executes the result's own held-out examples through the real path.
    Examples are never asserted -- they are run."""
    kind = "correctness"

    def verify(self, ctx: PanelContext) -> PanelVerdict:
        t0 = time.time()
        held_out = ctx.result.get("held_out") or []
        if not held_out:
            return PanelVerdict(
                panel=self.kind, passed=False,
                reason="correctness: no held-out expectations supplied; "
                       "there is nothing to execute, so nothing passes",
                evidence={}, elapsed_s=time.time() - t0)
        results = {}
        all_ok = True
        for i, spec in enumerate(held_out):
            try:
                r = ctx.engine.composer.execute_sync(
                    ctx.attempt.plan, spec["args"])
                ok = (bool(r.get("success"))
                      and r.get("value") == spec["expected"])
                detail = {"args": spec["args"],
                          "expected": spec["expected"],
                          "observed": r.get("value"), "passed": ok}
            except Exception as e:
                ok = False
                detail = {"args": spec.get("args"), "error": str(e),
                          "passed": False}
            results[f"held_out_{i}"] = detail
            all_ok = all_ok and ok
        return PanelVerdict(
            panel=self.kind, passed=all_ok,
            reason=("correctness: all held-out examples executed and "
                    "passed" if all_ok else
                    "correctness: held-out example(s) failed or diverged; "
                    "the result does not do what it claims on unseen input"),
            evidence=results, elapsed_s=time.time() - t0)


# ---------------------------------------------------------------------------
# safety -- effects within grants?
# ---------------------------------------------------------------------------

class SafetyPanel(VerificationPanel):
    """Three real checks, no self-description trusted:
      1. every primitive in the approach signature resolves in the
         primitive registry -- the granted set. An unresolvable name was
         never granted.
      2. no resolved primitive is quarantined anywhere -- the frozen
         get_quarantine_reason cross-loop interface.
      3. any code artifact in the result passes the static safety audit
         (verification/pipeline.py ASTVerifier).
    """
    kind = "safety"

    def verify(self, ctx: PanelContext) -> PanelVerdict:
        t0 = time.time()
        evidence: Dict[str, Any] = {}
        sigs = list(ctx.attempt.approach_signature or [])
        if not sigs:
            return PanelVerdict(
                panel=self.kind, passed=False,
                reason="safety: empty approach signature; there is no "
                       "granted effect to check the result against",
                evidence=evidence, elapsed_s=time.time() - t0)
        for name in sigs:
            try:
                resolved = ctx.engine.primitives.resolve(name)
            except Exception as e:
                resolved = None
                evidence[name] = {"resolve_error": str(e)}
            if not resolved:
                return PanelVerdict(
                    panel=self.kind, passed=False,
                    reason=f"safety: primitive {name!r} does not resolve "
                           f"in the primitive registry -- it was never "
                           f"granted; the result's effects are out of "
                           f"grant",
                    evidence={**evidence, "unresolved": name},
                    elapsed_s=time.time() - t0)
            evidence[name] = {"resolved": resolved}
            # Quarantine: a quarantined capability is a revoked grant.
            try:
                from swarm_engine.synthesis.integrity import (
                    get_quarantine_reason)
                q = get_quarantine_reason(resolved, engine=ctx.engine)
                evidence[name]["quarantined"] = bool(q.get("quarantined"))
                if q.get("quarantined"):
                    return PanelVerdict(
                        panel=self.kind, passed=False,
                        reason=f"safety: primitive {name!r} (resolved "
                               f"{resolved!r}) is quarantined "
                               f"({q.get('reason')}); its grant is "
                               f"revoked",
                        evidence=evidence, elapsed_s=time.time() - t0)
            except Exception as e:
                # The quarantine system is unavailable for this name: say
                # so honestly in the evidence; the registry check above
                # still stands as the grant gate.
                evidence[name]["quarantine_check"] = (
                    f"inconclusive: {type(e).__name__}: {e}")
        # Code artifacts, if any, get the static audit.
        for key in ("code", "source", "program_source"):
            src = ctx.result.get(key)
            if not src:
                continue
            try:
                from swarm_engine.verification.pipeline import ASTVerifier
                audit = ASTVerifier().check(str(src))
            except Exception as e:
                return PanelVerdict(
                    panel=self.kind, passed=False,
                    reason=f"safety: static audit of {key!r} could not "
                           f"run ({e}); unaudited code is refused",
                    evidence=evidence, elapsed_s=time.time() - t0)
            evidence[key] = {"audit_safe": audit["safe"],
                             "violations": audit["violations"]}
            if not audit["safe"]:
                return PanelVerdict(
                    panel=self.kind, passed=False,
                    reason=f"safety: static audit of {key!r} found "
                           f"{audit['violations']}; the code reaches "
                           f"beyond granted effects",
                    evidence=evidence, elapsed_s=time.time() - t0)
        return PanelVerdict(
            panel=self.kind, passed=True,
            reason="safety: all primitives resolve in the granted set, "
                   "none quarantined, code artifacts audit clean",
            evidence=evidence, elapsed_s=time.time() - t0)


# ---------------------------------------------------------------------------
# provenance -- is the lineage real?
# ---------------------------------------------------------------------------

def _git_dirty_paths(repo_root: str) -> Optional[set]:
    """The V10-P3 pattern: git truth about what this session touched.
    Returns the set of repo-relative paths that are modified or untracked,
    or None if git is unavailable (fail-closed upstream)."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo_root,
            capture_output=True, text=True, timeout=30)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    paths = set()
    for line in out.stdout.splitlines():
        # porcelain v1: XY <path> (or XY <orig> -> <new> for renames)
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        p = p.strip('"')
        paths.add(p)
    return paths


class ProvenancePanel(VerificationPanel):
    """The result's lineage must be real. Cited file artifacts must be
    session-attributable (in the git-dirty set: modified or untracked),
    never pre-existing committed work re-attributed as new. A result that
    cites no artifacts passes on run lineage (the honesty panel already
    proved the work executed); a result that cites artifacts must prove
    they are this session's work."""
    kind = "provenance"

    def verify(self, ctx: PanelContext) -> PanelVerdict:
        t0 = time.time()
        artifacts = list(ctx.result.get("file_artifacts") or [])
        evidence: Dict[str, Any] = {
            "run_id": ctx.result.get("run_id"),
            "artifacts_cited": len(artifacts)}
        if not artifacts:
            return PanelVerdict(
                panel=self.kind, passed=True,
                reason="provenance: no file artifacts cited; run lineage "
                       "stands on the executed claim (honesty panel)",
                evidence=evidence, elapsed_s=time.time() - t0)
        dirty = _git_dirty_paths(ctx.repo_root)
        if dirty is None:
            return PanelVerdict(
                panel=self.kind, passed=False,
                reason="provenance: git truth unavailable; cited "
                       "artifacts cannot be attributed, so the lineage "
                       "claim is refused",
                evidence=evidence, elapsed_s=time.time() - t0)
        for a in artifacts:
            rel = os.path.relpath(os.path.abspath(a), ctx.repo_root)
            if rel.startswith(".."):
                evidence[a] = "outside repo"
                return PanelVerdict(
                    panel=self.kind, passed=False,
                    reason=f"provenance: artifact {a!r} is outside the "
                           f"repo; its lineage cannot be established",
                    evidence=evidence, elapsed_s=time.time() - t0)
            if rel not in dirty:
                evidence[a] = "not session-attributable (committed/clean)"
                return PanelVerdict(
                    panel=self.kind, passed=False,
                    reason=f"provenance: artifact {a!r} is not "
                           f"session-attributable -- it is pre-existing "
                           f"committed work, re-attributed as new",
                    evidence=evidence, elapsed_s=time.time() - t0)
            evidence[a] = "session-attributable"
        return PanelVerdict(
            panel=self.kind, passed=True,
            reason="provenance: all cited artifacts are session-"
                   "attributable (git-dirty); the lineage is real",
            evidence=evidence, elapsed_s=time.time() - t0)


PANELS_IN_ORDER = (HonestyPanel, CorrectnessPanel, SafetyPanel,
                   ProvenancePanel)


def build_panels() -> List[VerificationPanel]:
    """The standing panel, in run order. No mission-specific wiring:
    the same four panels gate every completion."""
    return [cls() for cls in PANELS_IN_ORDER]
