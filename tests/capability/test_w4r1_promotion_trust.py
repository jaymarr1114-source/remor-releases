"""W4-R1 promotion-trust tests: PURE is granted by execution evidence only.

Every test below is real execution -- no mocks, no simulated denials.

(a) A PURE-declared artifact carrying the exact W4-R1 bypass
    (``__builtins__["op" + "en"]``), paired with a forged-looking admitted
    verdict row that LACKS effect evidence (pre-W4-R1 shape): promote()
    must refuse with VerificationFailed -- fail closed, never a grant.

(b) The advisory pre-screen still refuses an artifact with a bare
    open() call (ValueError, labelled "advisory pre-screen").

(c) The in-process enforcement is real: the bypass exec'd under
    build_namespace(PURE_POLICY) raises KeyError at call time -- and the
    control proves the bypass genuinely writes a file when the sandbox
    is absent, so the KeyError is a real denial, not a neutered test.
"""

from __future__ import annotations

import builtins
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pylib"))

from swarm_engine.agent_org.exceptions import VerificationFailed  # noqa: E402
from swarm_engine.agent_org.store import digest  # noqa: E402
from swarm_engine.capability.effect_sandbox import (  # noqa: E402
    PURE_POLICY,
    build_namespace,
)
from swarm_engine.capability.verdict_promotion import (  # noqa: E402
    VerdictPromotionBridge,
)
from swarm_engine.primitives.core import ANY, Effect  # noqa: E402

# The exact W4-R1 bypass shape: it defeated the AST PURE screen (no bare
# open() call, no import) and wrote a file during the bridge's in-process
# re-verification. The advisory screen must still MISS this -- that is the
# point: only execution evidence grants PURE.
BYPASS_CODE = '''
def capability(path):
    writer = __builtins__["op" + "en"](path, "w")
    writer.write("w4r1-bypass")
    writer.close()
    return "wrote"
'''

BARE_OPEN_CODE = '''
def capability(path):
    with open(path, "w") as fh:
        fh.write("x")
    return "wrote"
'''

HONEST_PURE_CODE = '''
def capability(x):
    return x * 2 + 1
'''


class _Review:
    """Fake ReviewBoard returning a canned latest admitted verdict row."""

    def __init__(self, code: str, *, with_effect_evidence: bool):
        bindings = {"n_findings": 3, "cases": "1/1"}
        if with_effect_evidence:
            bindings["effect_evidence"] = {
                "profile": "pure",
                "grants": [],
                "observed_effects": [],
            }
        self._row = {
            "admitted": "1",
            "execution_id": "vex_test",
            "verifier": "independent_validator_v1",
            "code_digest": digest(code),
            "artifact_kind": "synthesis",
            "artifact_ref": "test_artifact",
            "bindings": json.dumps(bindings),
        }

    def require_admitted_verdict(self, code_digest, artifact_kind):
        assert code_digest == self._row["code_digest"]
        assert artifact_kind == "synthesis"
        return self._row


class _FailRegistry:
    """Registry that explodes if anything is ever registered: a trust
    failure must fire before registration, so reaching register() is a
    test failure in itself."""

    def __contains__(self, name):
        return False

    def register(self, prim, overwrite=False):
        raise AssertionError("registered despite a trust failure")

    def mark_promoted(self, *args, **kwargs):
        raise AssertionError("marked promoted despite a trust failure")


def _bridge(code: str, *, with_effect_evidence: bool):
    return VerdictPromotionBridge(
        registry=_FailRegistry(),
        review=_Review(code, with_effect_evidence=with_effect_evidence),
    )


# ---------------------------------------------------------------------------
# (a) Fail closed: admitted verdict WITHOUT effect evidence
# ---------------------------------------------------------------------------

def test_pure_promote_without_effect_evidence_fails_closed():
    """Pre-W4-R1 verdict rows carry no effect_evidence: a PURE claim over
    them must raise VerificationFailed -- never silently grant PURE."""
    bridge = _bridge(BYPASS_CODE, with_effect_evidence=False)
    with pytest.raises(VerificationFailed) as excinfo:
        bridge.promote(
            BYPASS_CODE,
            artifact_ref="w4r1_bypass",
            inputs={"path": ANY},
            output=ANY,
            entrypoint="capability",
            effects=(Effect.PURE,),
        )
    assert "PURE requires verdict execution under the PURE effect policy" \
        in str(excinfo.value)


def test_pure_promote_with_observed_effects_fails_closed():
    """A verdict row whose recorded run OBSERVED an effect under PURE is a
    hard trust failure: VerificationFailed, not a grant."""
    bridge = _bridge(HONEST_PURE_CODE, with_effect_evidence=True)
    row = bridge.review._row
    bindings = json.loads(row["bindings"])
    bindings["effect_evidence"]["observed_effects"] = [
        {"event": "open", "args": ["/tmp/evil.txt", "w"]},
    ]
    row["bindings"] = json.dumps(bindings)
    with pytest.raises(VerificationFailed) as excinfo:
        bridge.promote(
            HONEST_PURE_CODE,
            artifact_ref="observed_effect",
            inputs={"x": ANY},
            output=ANY,
            entrypoint="capability",
            effects=(Effect.PURE,),
        )
    assert "hard trust failure" in str(excinfo.value)


def test_pure_promote_with_clean_pure_evidence_reaches_registration():
    """The positive control: clean PURE execution evidence passes the
    trust gate and reaches the registry (which explodes here by design,
    proving the gate did NOT refuse)."""
    bridge = _bridge(HONEST_PURE_CODE, with_effect_evidence=True)
    with pytest.raises(AssertionError, match="registered despite"):
        bridge.promote(
            HONEST_PURE_CODE,
            artifact_ref="honest_pure",
            inputs={"x": ANY},
            output=ANY,
            entrypoint="capability",
            effects=(Effect.PURE,),
        )


def test_nonpure_promote_needs_no_effect_evidence():
    """Declaring the real (non-pure) effects never touches the PURE gate:
    the promotion proceeds to the registry."""
    bridge = _bridge(BYPASS_CODE, with_effect_evidence=False)
    with pytest.raises(AssertionError, match="registered despite"):
        bridge.promote(
            BYPASS_CODE,
            artifact_ref="declared_write_fs",
            inputs={"path": ANY},
            output=ANY,
            entrypoint="capability",
            effects=(Effect.WRITE_FS,),
        )


# ---------------------------------------------------------------------------
# (b) Advisory pre-screen still refuses
# ---------------------------------------------------------------------------

def test_advisory_prescreen_refuses_bare_open():
    """An artifact with a bare open() call is refused by the advisory
    pre-screen (ValueError), which keeps its refusal teeth -- it just
    cannot grant."""
    bridge = _bridge(BARE_OPEN_CODE, with_effect_evidence=True)
    with pytest.raises(ValueError) as excinfo:
        bridge.promote(
            BARE_OPEN_CODE,
            artifact_ref="bare_open",
            inputs={"path": ANY},
            output=ANY,
            entrypoint="capability",
            effects=(Effect.PURE,),
        )
    assert "advisory pre-screen" in str(excinfo.value)


def test_advisory_prescreen_misses_obfuscated_bypass():
    """Documents WHY the screen is advisory-only: the W4-R1
    ``__builtins__["op" + "en"]`` shape shows no static markers, so the
    screen cannot be the safety authority. The evidence gate below is."""
    bridge = _bridge(BYPASS_CODE, with_effect_evidence=False)
    # The screen alone would let it through -- so promotion must still
    # refuse, and it must be the evidence gate (VerificationFailed), not
    # the screen, that fires.
    with pytest.raises(VerificationFailed):
        bridge.promote(
            BYPASS_CODE,
            artifact_ref="w4r1_bypass",
            inputs={"path": ANY},
            output=ANY,
            entrypoint="capability",
            effects=(Effect.PURE,),
        )


# ---------------------------------------------------------------------------
# (c) In-process enforcement is real
# ---------------------------------------------------------------------------

def test_bypass_blocked_in_process_under_pure_policy(tmp_path):
    """The W4-R1 write site: the bypass exec'd under build_namespace(
    PURE_POLICY) raises KeyError at call time -- capability-absence, not
    a blacklist -- and no file is created."""
    ns = build_namespace(PURE_POLICY)
    exec(compile(BYPASS_CODE, "<admitted-capability>", "exec"), ns)
    target = str(tmp_path / "w4r1.txt")
    with pytest.raises(KeyError):
        ns["capability"](target)
    assert not os.path.exists(target)


def test_bypass_is_genuinely_effectful_without_sandbox(tmp_path):
    """Control: the same bypass, exec'd with real builtins present,
    really does write the file. The KeyError above is a real denial of
    a real effect, not a neutered artifact."""
    ns = {
        "__name__": "unrestricted_module",
        "__builtins__": dict(builtins.__dict__),
    }
    exec(compile(BYPASS_CODE, "<bypass-control>", "exec"), ns)
    target = str(tmp_path / "w4r1_control.txt")
    assert ns["capability"](target) == "wrote"
    with open(target, "r", encoding="utf-8") as fh:
        assert fh.read() == "w4r1-bypass"
