"""W4-R1 MANDATE PROOFS: the sandbox (not the scanner) is the trust authority.

James's decision (2026-09-27): the PURE static screen is never the authority
that makes code safe -- the *execution environment* is. This suite proves it
causally and adversarially:

  Proof 1 -- the exact W4-R1 bypass (``__builtins__["op" + "en"]``) is
             advisory-clean, yet denied by capability-absence in a fresh
             subprocess, with the target file absent from disk.
  Proof 2 -- effectless code (function, class-based, statistics/math)
             is scanner-clean, runs correctly under PURE, and produces the
             real execution evidence the promotion gate requires.
  Proof 3 -- an explicit grant is the ONLY difference between a write that
             succeeds in-scope and the identical bytes denied under PURE;
             granted writes outside the scope are blocked.
  Proof 4 -- declared-vs-actual matrix: a PURE declaration over a real
             write fails, and a verdict row carrying real observed effects
             is refused at the promotion gate as a HARD TRUST FAILURE;
             a write_fs declaration with no grant widens nothing.
  Proof 5 -- obfuscation battery: four textually distinct fresh-process
             constructions of the file write, each blocked, each target
             absent.
  Proof 6 -- positive control: the exact bypass bytes under TRUSTED_POLICY
             genuinely write the file, proving the denies above come from
             the sandbox and not from a broken harness.

ANTI-SIMULATION CONTRACT (hard):
  * Every "blocked" assertion is paired with a filesystem-ABSENCE
    assertion on a real path (os.path.exists after the run).
  * Every "succeeds" assertion is paired with filesystem-PRESENCE plus
    exact content assertions.
  * No unittest.mock, no monkeypatching of builtins, no faked run_code:
    every test calls the real
    swarm_engine.agent_org.subprocess_runner.run_code, which spawns a
    FRESH python3 subprocess per call.
  * The four battery constructions are four separate string constants;
    no shared helper builds the write call (asserted pairwise-distinct).
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANON = os.path.normpath(os.path.join(_HERE, "..", ".."))
_PYL = os.path.join(_CANON, "pylib")
sys.path.insert(0, _PYL)
# The child subprocess inherits the environment (not sys.path), so pylib
# must ride on PYTHONPATH for the shim's `import swarm_engine` to resolve.
os.environ["PYTHONPATH"] = _PYL + os.pathsep + os.environ.get(
    "PYTHONPATH", "")

from swarm_engine.acquisition.semantic import Case  # noqa: E402
from swarm_engine.agent_org.exceptions import VerificationFailed  # noqa: E402
from swarm_engine.agent_org.review import VERIFIER_ID  # noqa: E402
from swarm_engine.agent_org.store import digest  # noqa: E402
from swarm_engine.agent_org.subprocess_runner import run_code  # noqa: E402
from swarm_engine.capability.effect_sandbox import (  # noqa: E402
    PURE_POLICY,
    TRUSTED_POLICY,
    EffectPolicy,
    policy_for_effects,
    restricted_builtins,
)
from swarm_engine.capability.verdict_promotion import (  # noqa: E402
    VerdictPromotionBridge,
    _pure_claim_markers,
)
from swarm_engine.primitives.core import ANY, Effect, PrimitiveRegistry  # noqa: E402
from swarm_engine.verification.independent import (  # noqa: E402
    IndependentValidator,
)


# ---------------------------------------------------------------------------
# Fixed attack / capability fixtures. These are REVIEWED TEST-PROCEDURE
# bytes fixed in this file (never synthesized or admitted candidate bytes),
# so they may be used as the positive control under TRUSTED_POLICY per the
# inviolable rule in effect_sandbox.py (justification recorded at that
# call site). Everywhere else they run under PURE.
# ---------------------------------------------------------------------------

# Proof 1 / Proof 6: the EXACT W4-R1 bypass spelling from the mandate.
BYPASS_SRC = '''def capability(path, text):
    f = __builtins__["op" + "en"](path, "w")
    f.write(text)
    f.close()
    return "wrote"
'''

# Proof 2: effectless positives.
SQUARE_SRC = '''def capability(xs):
    return [x*x for x in xs]
'''

CLASS_SRC = '''class _Squarer:
    def square(self, x):
        return x*x

def capability(xs):
    s = _Squarer()
    return [s.square(x) for x in xs]
'''

STATS_SRC = '''import statistics
import math

def capability(xs):
    return {"mean": statistics.mean(xs), "hyp": math.hypot(*xs)}
'''

# Proof 3 / Proof 4: honest writer using plain open() (bare call -- the
# advisory screen sees it; the sandbox decides).
WRITER_SRC = '''def capability(path, text):
    with open(path, "w") as f:
        f.write(text)
    return "wrote"
'''

# Proof 5: four DISTINCT fresh-process constructions. Textually separate
# constants -- no shared helper builds the write call.
BATTERY_1_SRC = '''def capability(path, text):
    w = getattr(__builtins__, "op"+"en")(path, "w")
    w.write(text)
    w.close()
    return "wrote"
'''

BATTERY_2_SRC = '''def capability(path, text):
    w = eval("op"+"en")(path, "w")
    w.write(text)
    w.close()
    return "wrote"
'''

BATTERY_3_SRC = '''def capability(path, text):
    __import__("os").system("echo INTRUSION > " + path)
    return "wrote"
'''

BATTERY_4_SRC = '''def _wrap(fn):
    def inner(*a, **k):
        return fn(*a, **k)
    return inner

def _make():
    hidden = lambda path, text: (__builtins__["op" + "en"](path, "w").write(text), "wrote")[1]
    return _wrap(hidden)

capability = _make()
'''

ATTACK_FIXTURES = {
    "BYPASS_SRC",
    "BATTERY_1_SRC",
    "BATTERY_2_SRC",
    "BATTERY_3_SRC",
    "BATTERY_4_SRC",
}

# Obfuscation fragments: string pieces an obfuscated spelling is built
# from. A pattern-matching blacklist would have to contain these. The
# enforcement mechanism must contain NONE of them.
OBFUSCATION_FRAGMENTS = {"op", "en", "ope", "pen"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _mkdtemp():
    return tempfile.mkdtemp(prefix="w4r1_mandate_")


def _case(report, idx=0):
    """The per-case outcome. run_code reports harness-level ok=True when the
    fresh subprocess executed; a DENIED capability surfaces as the case's
    own ok=False with the denial reason in case['error']."""
    assert report.value, "run_code returned no per-case results"
    return report.value[idx]


def _string_constants(path):
    """All string constants in a source file, excluding docstrings."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                doc_ids.add(id(body[0].value))
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and id(n) not in doc_ids}


def _fragment_owners(path):
    """Map each obfuscation fragment -> the top-level assignment names
    whose value contains it as an exact string constant."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    owners = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets
                     if isinstance(t, ast.Name)}
            consts = {c.value for c in ast.walk(node.value)
                      if isinstance(c, ast.Constant)
                      and isinstance(c.value, str)}
            for const in consts:
                for frag in OBFUSCATION_FRAGMENTS:
                    if const == frag:
                        owners.setdefault(frag, set()).update(names)
    return owners


# ---------------------------------------------------------------------------
# Proof 1 -- exact bypass reproduction
# ---------------------------------------------------------------------------

class TestProof1ExactBypass:
    def test_1a_scanner_is_advisory_clean(self):
        # Expected: [] -- the AST screen is blind to the bypass, and that
        # is now FINE: the screen is advisory-only, never a grant.
        markers = _pure_claim_markers(BYPASS_SRC)
        assert markers == [], f"unexpected markers: {markers}"

    def test_1b_bypass_denied_by_capability_absence(self):
        scratch = _mkdtemp()
        try:
            target = os.path.join(scratch, "marker.txt")
            report = run_code(
                BYPASS_SRC, "capability",
                [{"path": target, "text": "pwn"}])
            case = _case(report)
            # Expected: the case fails -- the name `open` simply does not
            # exist in the PURE namespace, however it is spelled.
            assert case["ok"] is False
            assert "KeyError" in case["error"], case["error"]
            assert "'open'" in case["error"], case["error"]
            # The file was genuinely never created.
            assert not os.path.exists(target), \
                "bypass wrote the file -- sandbox FAILED"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_1c_no_blacklist_in_the_enforcement_mechanism(self):
        # Expected: zero obfuscation-fragment string constants in the
        # enforcement file. The denial above is capability-absence
        # (`open` missing from the namespace), not pattern matching.
        sandbox_path = os.path.join(
            _CANON, "runtime", "capability", "effect_sandbox.py")
        consts = _string_constants(sandbox_path)
        hits = consts & OBFUSCATION_FRAGMENTS
        assert hits == set(), (
            f"blacklist-style fragments in effect_sandbox.py: {hits}")
        # The advisory screen file likewise carries no obfuscation
        # fragments (it screens bare `open(...)` AST calls only, and is
        # documented advisory-only -- it may refuse, never grant).
        promo_path = os.path.join(
            _CANON, "runtime", "capability", "verdict_promotion.py")
        hits2 = _string_constants(promo_path) & OBFUSCATION_FRAGMENTS
        assert hits2 == set(), (
            f"blacklist-style fragments in verdict_promotion.py: {hits2}")
        # Positive statement of the mechanism: under PURE there is simply
        # no `open` to reach -- no spelling of the name can find one.
        assert "open" not in restricted_builtins(PURE_POLICY)
        # This test file's only fragment occurrences are the attack
        # fixtures themselves (the attacks under test), never a scanning
        # pattern. (OBFUSCATION_FRAGMENTS is this scan's own vocabulary
        # definition, excluded -- it is the thing being searched for,
        # not a blacklist entry used to scan code.)
        owners = _fragment_owners(__file__)
        for frag, names in owners.items():
            names = set(names) - {"OBFUSCATION_FRAGMENTS"}
            assert names <= ATTACK_FIXTURES, (
                f"fragment {frag!r} outside attack fixtures: {names}")
        assert set(owners) >= {"op", "en"}, \
            f"attack fixtures missing expected fragments: {set(owners)}"

    def test_1d_denial_is_auditable(self):
        scratch = _mkdtemp()
        try:
            target = os.path.join(scratch, "marker.txt")
            report = run_code(
                BYPASS_SRC, "capability",
                [{"path": target, "text": "pwn"}])
            case = _case(report)
            # report.observed_effects is a real list (empty: the KeyError
            # fired before any C-layer effect could be attempted).
            assert isinstance(report.observed_effects, list)
            assert report.observed_effects == []
            # The failure reason is recorded on the per-case result.
            assert case["error"], "denial reason was not recorded"
            assert "KeyError" in case["error"]
            # The enforced policy is recorded too.
            assert report.effect_policy.get("profile") == "pure"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Proof 2 -- effectless positive
# ---------------------------------------------------------------------------

class TestProof2EffectlessPositive:
    def test_2a_scanner_clean(self):
        for name, src in (("function", SQUARE_SRC),
                          ("class-based", CLASS_SRC),
                          ("statistics/math", STATS_SRC)):
            assert _pure_claim_markers(src) == [], \
                f"{name}: unexpected markers"

    @pytest.mark.parametrize("src,expected", [
        (SQUARE_SRC, [1, 4, 9, 16]),
        (CLASS_SRC, [1, 4, 9, 16]),
    ])
    def test_2b_runs_correctly_under_pure(self, src, expected):
        report = run_code(src, "capability", [{"xs": [1, 2, 3, 4]}])
        case = _case(report)
        assert case["ok"] is True, case["error"]
        assert case["value"] == expected
        assert report.observed_effects == []
        assert report.effect_violations == []

    def test_2b_statistics_math_variant(self):
        report = run_code(STATS_SRC, "capability", [{"xs": [1, 2, 3, 4]}])
        case = _case(report)
        assert case["ok"] is True, case["error"]
        assert case["value"]["mean"] == pytest.approx(2.5)
        assert case["value"]["hyp"] == pytest.approx(
            (1 + 4 + 9 + 16) ** 0.5)
        assert report.observed_effects == []
        assert report.effect_violations == []

    def test_2c_real_validator_execution_evidence(self):
        """The REAL IndependentValidator (real fresh-subprocess runner)
        produces the execution evidence the promote() gate requires:
        profile == 'pure' with zero observed effects.

        EXACT GAP (documented, not faked): the full
        ReviewBoard.verify_artifact -> _store_verdict -> promote() chain is
        NOT exercised in-test. _store_verdict refuses without an external
        anchor journal (AnchorMissing unless `anchor_admin.py init` created
        the genesis record), the Arbiter with require_binding=True refuses
        evidence lacking verified oracle bindings (which requires a live
        EngineOracleHandle bound to remor:engine), and accept() requires
        that live engine handle. Those are production trust anchors; a
        unit test must not fabricate them. What IS proven here is the
        evidence the gate consumes, produced by the real verifier over
        real subprocess execution -- profile 'pure', zero observed
        effects.
        """
        spec = SimpleNamespace(
            input_names=["xs"],
            description="square each element",
            examples=[({"xs": [1, 2, 3]}, [1, 4, 9]),
                      ({"xs": [0, 5]}, [0, 25])])
        cases = [Case(args={"xs": [2, 3]}, expect=[4, 9], label="basic")]
        validator = IndependentValidator(run_code)
        evidence = validator.verifier.verify(
            SQUARE_SRC, "capability", spec, cases)
        assert evidence.cases_passed == evidence.cases_run == 1
        assert evidence.failed_levels() == []
        # The promotion gate's required evidence, from real execution:
        assert evidence.effect_policy == {"profile": "pure", "grants": []}
        assert evidence.observed_effects == []


# ---------------------------------------------------------------------------
# Proof 3 -- explicit grant toggle
# ---------------------------------------------------------------------------

class TestProof3GrantToggle:
    def _granted(self, scope):
        return EffectPolicy("granted", ("fs.write:" + scope,))

    def test_3a_granted_write_succeeds_in_scope(self):
        scratch = _mkdtemp()
        try:
            target = os.path.join(scratch, "in_scope.txt")
            report = run_code(
                WRITER_SRC, "capability",
                [{"path": target, "text": "hello-grant"}],
                effect_policy=self._granted(scratch))
            case = _case(report)
            assert case["ok"] is True, case["error"]
            assert case["value"] == "wrote"
            # Presence + exact content on the real filesystem.
            assert os.path.exists(target)
            with open(target, encoding="utf-8") as fh:
                assert fh.read() == "hello-grant"
            # The REAL audit hook observed the genuine open event, and the
            # policy allowed it: zero violations.
            assert len(report.observed_effects) == 1
            evt = report.observed_effects[0]
            assert evt["event"] == "open"
            assert evt["args"][0] == os.path.realpath(target)
            assert report.effect_violations == []
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_3b_identical_bytes_denied_under_pure(self):
        scratch = _mkdtemp()
        try:
            target = os.path.join(scratch, "denied.txt")
            report = run_code(
                WRITER_SRC, "capability",
                [{"path": target, "text": "nope"}])  # PURE default
            case = _case(report)
            assert case["ok"] is False
            assert "NameError" in case["error"], case["error"]
            assert "'open'" in case["error"], case["error"]
            assert not os.path.exists(target), \
                "write happened under PURE -- sandbox FAILED"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_3c_grant_is_the_only_difference(self):
        # The source strings are IDENTICAL; only the policy differs.
        src_a = WRITER_SRC
        src_b = WRITER_SRC
        assert src_a == src_b
        assert src_a is src_b  # literally the same bytes object
        scratch = _mkdtemp()
        outside = _mkdtemp()
        try:
            in_target = os.path.join(scratch, "in.txt")
            out_target = os.path.join(outside, "escape.txt")
            rep_in = run_code(
                src_a, "capability", [{"path": in_target, "text": "x"}],
                effect_policy=self._granted(scratch))
            rep_pure = run_code(
                src_b, "capability", [{"path": in_target, "text": "x"}])
            assert _case(rep_in)["ok"] is True
            assert _case(rep_pure)["ok"] is False
            # Granted policy, OUTSIDE the scope: blocked, file absent.
            rep_out = run_code(
                src_a, "capability", [{"path": out_target, "text": "x"}],
                effect_policy=self._granted(scratch))
            out_case = _case(rep_out)
            assert out_case["ok"] is False
            assert "PermissionError" in out_case["error"], out_case["error"]
            assert "outside granted scope" in out_case["error"]
            assert not os.path.exists(out_target), \
                "out-of-scope write happened -- sandbox FAILED"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)


# ---------------------------------------------------------------------------
# Proof 4 -- declared vs actual matrix
# ---------------------------------------------------------------------------

class _CannedReview:
    """Minimal review double for the promotion GATE check only.

    It returns a caller-assembled verdict row; the code under test is the
    REAL VerdictPromotionBridge.promote(). The row models the trust-failure
    scenario 'declares PURE but actually writes': its effect_evidence
    carries REAL audit records produced by real execution (Proof 3a).
    The gate must refuse -- nothing is registered.
    """

    def __init__(self, row):
        self._row = row
        self.seen_digest = None

    def require_admitted_verdict(self, code_digest, artifact_kind):
        self.seen_digest = code_digest
        assert artifact_kind == "synthesis"
        return self._row


class TestProof4DeclaredVsActual:
    def test_4a_pure_declaration_over_real_write_fails_then_gate_refuses(self):
        scratch = _mkdtemp()
        try:
            # (i) Declares PURE (default policy), actually writes via the
            # bypass spelling: the run fails, the file is absent.
            target = os.path.join(scratch, "sneaky.txt")
            report = run_code(
                BYPASS_SRC, "capability",
                [{"path": target, "text": "pwn"}])
            assert _case(report)["ok"] is False
            assert not os.path.exists(target)

            # (ii) Produce REAL observed effect records via a granted write.
            granted_target = os.path.join(scratch, "granted.txt")
            granted = run_code(
                WRITER_SRC, "capability",
                [{"path": granted_target, "text": "real"}],
                effect_policy=EffectPolicy(
                    "granted", ("fs.write:" + scratch,)))
            assert _case(granted)["ok"] is True
            real_effects = granted.observed_effects
            assert real_effects and real_effects[0]["event"] == "open"

            # (iii) The promotion gate: a verdict row claiming profile
            # 'pure' while carrying those REAL observed effects must raise
            # VerificationFailed -- a HARD TRUST FAILURE that invalidates
            # the classification. Nothing may be registered.
            row = {
                "admitted": "1",
                "execution_id": "vex_w4r1_4a",
                "verifier": VERIFIER_ID,
                "bindings": json.dumps({
                    "effect_evidence": {
                        "profile": "pure",
                        "grants": [],
                        "observed_effects": real_effects,
                    }
                }),
            }
            review = _CannedReview(row)
            bridge = VerdictPromotionBridge(PrimitiveRegistry(), review)
            with pytest.raises(VerificationFailed) as excinfo:
                bridge.promote(
                    BYPASS_SRC,
                    artifact_ref="w4r1_mandate_4a",
                    inputs={"path": ANY, "text": ANY},
                    output=ANY,
                    entrypoint="capability",
                    effects=(Effect.PURE,))
            msg = str(excinfo.value)
            # Auditable: the reason names the observed effect and the hard
            # trust failure.
            assert "observed effect" in msg, msg
            assert "hard trust failure" in msg, msg
            assert review.seen_digest == digest(BYPASS_SRC)
            assert "w4r1_mandate_4a" not in bridge.records
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_4b_write_fs_declaration_without_grant_widens_nothing(self):
        # A declaration builds the sandbox REQUEST; only an explicit grant
        # decides. No grant scope -> PURE_POLICY, byte-identical.
        assert policy_for_effects(["write_fs"], None) == PURE_POLICY
        # And the pure body runs fine under the default PURE policy: the
        # declaration widened nothing at execution either.
        report = run_code(SQUARE_SRC, "capability", [{"xs": [3]}])
        case = _case(report)
        assert case["ok"] is True, case["error"]
        assert case["value"] == [9]
        assert report.effect_policy.get("profile") == "pure"


# ---------------------------------------------------------------------------
# Proof 5 -- obfuscation battery
# ---------------------------------------------------------------------------

BATTERY = [
    ("getattr-chain", BATTERY_1_SRC, "AttributeError"),
    ("eval-built-name", BATTERY_2_SRC, "NameError"),
    ("import-indirection", BATTERY_3_SRC, "ImportError"),
    ("lambda-decorator-nesting", BATTERY_4_SRC, "KeyError"),
]


class TestProof5ObfuscationBattery:
    def test_battery_constructions_are_textually_distinct(self):
        srcs = [BATTERY_1_SRC, BATTERY_2_SRC, BATTERY_3_SRC, BATTERY_4_SRC]
        assert len(set(srcs)) == 4, "battery sources are not all distinct"
        # No shared helper builds the write call: each source carries its
        # own construction marker.
        assert 'getattr(__builtins__, "op"+"en")' in BATTERY_1_SRC
        assert 'eval("op"+"en")' in BATTERY_2_SRC
        assert '__import__("os")' in BATTERY_3_SRC
        assert "__builtins__[\"op\" + \"en\"]" in BATTERY_4_SRC

    @pytest.mark.parametrize("label,src,exc", BATTERY)
    def test_battery_construction_blocked(self, label, src, exc):
        scratch = _mkdtemp()
        try:
            target = os.path.join(scratch, f"{label}.txt")
            report = run_code(src, "capability",
                              [{"path": target, "text": "pwn"}])
            case = _case(report)
            # Expected: denied -- the capability is absent under every
            # spelling; the denial type differs per construction.
            assert case["ok"] is False, \
                f"{label}: unexpectedly succeeded"
            assert exc in case["error"], \
                f"{label}: expected {exc}, got {case['error']}"
            # The file was genuinely never created.
            assert not os.path.exists(target), \
                f"{label}: wrote the file -- sandbox FAILED"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ---------------------------------------------------------------------------
# Proof 6 -- real enforcement spot-check (positive control)
# ---------------------------------------------------------------------------

class TestProof6PositiveControl:
    def test_trusted_bypass_genuinely_writes(self):
        """Positive control: the EXACT bypass bytes that Proofs 1/5 deny
        genuinely write a file when the sandbox grants full builtins.

        INVOLABLE-RULE JUSTIFICATION (effect_sandbox.py): BYPASS_SRC is
        fixed, reviewed test-procedure bytes in this file -- never
        synthesized or admitted candidate bytes -- so TRUSTED_POLICY is
        permitted here. This proves the denies above come from the
        sandbox's capability-absence, not from a broken test setup.
        """
        scratch = _mkdtemp()
        try:
            target = os.path.join(scratch, "trusted.txt")
            report = run_code(
                BYPASS_SRC, "capability",
                [{"path": target, "text": "trusted-control"}],
                effect_policy=TRUSTED_POLICY)
            case = _case(report)
            assert case["ok"] is True, case["error"]
            assert case["value"] == "wrote"
            assert os.path.exists(target)
            with open(target, encoding="utf-8") as fh:
                assert fh.read() == "trusted-control"
            # The audit hook still observed the real effect (records-only
            # under TRUSTED): zero violations, full observability.
            assert any(e["event"] == "open"
                       for e in report.observed_effects)
            assert report.effect_violations == []
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
