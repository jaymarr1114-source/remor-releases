#!/usr/bin/env python3
"""QWEN3-ACQUIRE-1 end-to-end proof: the real Qwen3-8B teacher behind the
governed cognition inlet.

Proves: a borrow through GrantedCognitionProvider answered by the REAL
Qwen3-8B (Q4_K_M GGUF, llama.cpp) returns with provenance
borrowed:qwen3@<pinned-rev>, with grant-gating, native-first, the
FrmGrant-only gate, charging, and telemetry all holding UNCHANGED
relative to the stub-proven governance (BRAIN-SCAFFOLD-1).

Run in a fresh process from the tree root:
    python3 proofs/qwen3_acquire1_proof.py
Exit 0 iff every section passes. Requires the acquired weights at
~/workspace/models/qwen3-8b/*.gguf (QWEN3-ACQUIRE-1 acquisition) and the
llama.cpp binary at ~/workspace/tools/llama.cpp-b11284/llama-b11284/llama-cli.

Sections:
  Q1 license gate evidence (pinned rev, Apache 2.0, no extra terms)
  Q2 weight integrity (independent sha256 recompute vs the LFS pin)
  Q3 teacher slot (interface, documented estimate, standalone complete)
  Q4 end-to-end borrow: real inference, provenance, refusal, charge, telemetry
  Q5 grantless cognize refused (governance unchanged with real teacher)
  Q6 insufficient grant deferred, zero charge
  Q7 wrong-grant-type refused fail-closed (adversarial)
  Q8 exhausted pool refuses at spawn (adversarial)
  Q9 telemetry reflects the real borrow (ratio, deferral rate)
  Q10 no silent charge on any refused/deferred call
"""

import glob
import hashlib
import os
import sys
import time

TREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(TREE, "pylib"))

from swarm_engine.core.microcontroller.substrate import (  # noqa: E402
    CognitionProvider,
    MicrocontrollerSubstrate,
)
from swarm_engine.core.microcontroller.granted_cognition import (  # noqa: E402
    GrantedCognitionProvider,
    NativeRefusal,
)
from swarm_engine.core.microcontroller.qwen3_teacher import (  # noqa: E402
    QWEN3_GGUF_REVISION,
    QWEN3_GGUF_SHA256,
    Qwen3Teacher,
)
from swarm_engine.curiosity.frm.grant import (  # noqa: E402
    FrmGrant,
    LendingRecord,
)
from swarm_engine.primitives.core import Grant as PrimitiveGrant  # noqa: E402

PASS = []
FAIL = []

MODELS_DIR = "/home/hatch/workspace/models/qwen3-8b"
LLAMA_CLI = ("/home/hatch/workspace/tools/llama.cpp-b11284/"
             "llama-b11284/llama-cli")


def check(section, name, cond, detail=""):
    if cond:
        PASS.append(f"{section}:{name}")
    else:
        FAIL.append(f"{section}:{name} -- {detail}")


def make_grant(budget_s, note="qwen3-proof", enforcement="RUNNING"):
    return FrmGrant.issue(
        domain="curiosity", epoch_id=1, epoch_s=300.0,
        budget_s=budget_s, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue=enforcement,
        issued_at=time.time(), note=note)


class RefusingNative(CognitionProvider):
    def request_cognition(self, *, mc_id, prompt, context):
        raise NativeRefusal("precision_tier:cannot_reason_about_x",
                            "test refusal")


def gguf_path():
    hits = sorted(glob.glob(os.path.join(MODELS_DIR, "*.gguf")))
    assert hits, f"no .gguf under {MODELS_DIR}: acquisition missing?"
    return hits[0]


# ---------------------------------------------------------------- Q1 ----

def q1_license_gate():
    lic_dir = os.path.join(TREE, "proofs", "qwen3_license")
    lic = os.path.join(lic_dir, "LICENSE")
    card = os.path.join(lic_dir, "README.md")
    check("Q1", "license_saved", os.path.isfile(lic), lic)
    check("Q1", "card_saved", os.path.isfile(card), card)
    with open(lic) as fh:
        text = fh.read()
    check("Q1", "apache_2_0_verbatim",
          "Apache License" in text and "Version 2.0, January 2004" in text
          and "Copyright 2025 Alibaba Cloud" in text,
          text[:80])
    with open(card) as fh:
        card_text = fh.read()
    check("Q1", "card_declares_apache",
          "license: apache-2.0" in card_text, card_text[:120])
    lowered = card_text.lower()
    check("Q1", "no_extra_terms",
          not any(w in lowered for w in
                  ["acceptable use", "prohibit", "forbidden",
                   "must not use", "usage policy"]),
          "restrictive term found in model card")
    check("Q1", "teacher_revision_is_pinned_rev",
          Qwen3Teacher.revision == QWEN3_GGUF_REVISION,
          f"{Qwen3Teacher.revision} vs {QWEN3_GGUF_REVISION}")


# ---------------------------------------------------------------- Q2 ----

def q2_weight_integrity():
    path = gguf_path()
    check("Q2", "weight_file_present", os.path.isfile(path), path)
    check("Q2", "weight_not_in_tree",
          not os.path.abspath(path).startswith(os.path.abspath(TREE)),
          path)
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    observed = digest.hexdigest()
    check("Q2", "sha256_matches_lfs_pin", observed == QWEN3_GGUF_SHA256,
          f"observed {observed[:16]}... vs pin {QWEN3_GGUF_SHA256[:16]}...")
    check("Q2", "size_matches_manifest", size == 5027783488, str(size))
    check("Q2", "model_not_committed",
          os.popen(f"cd {TREE} && git status --short -- "
                   f"{path} 2>/dev/null").read().strip() == ""
          and not os.path.abspath(path).startswith(os.path.abspath(TREE)),
          path)


# ---------------------------------------------------------------- Q3 ----

_TEACHER = None


def teacher():
    global _TEACHER
    if _TEACHER is None:
        _TEACHER = Qwen3Teacher(
            gguf_path=gguf_path(), llama_cli=LLAMA_CLI,
            threads=2, context_size=256, max_new_tokens=32)
    return _TEACHER


def q3_teacher_slot():
    t = teacher()
    check("Q3", "model_id", t.model_id == "qwen3", t.model_id)
    check("Q3", "revision_pinned", t.revision == QWEN3_GGUF_REVISION,
          t.revision)
    est = t.estimate_cost_s("short prompt")
    check("Q3", "estimate_documented_positive", est > 0, str(est))
    check("Q3", "estimate_grows_with_prompt",
          t.estimate_cost_s("x" * 1000) > est, "not monotonic")
    text, secs = t.complete("Reply with exactly: HELLO", {})
    check("Q3", "standalone_complete_ok", isinstance(text, str) and text,
          text[:60])
    check("Q3", "standalone_not_stub", "[STUB-TEACHER" not in text,
          text[:60])
    check("Q3", "actual_seconds_measured", secs > 0, str(secs))
    # Throughput sanity: the estimate must be conservative vs reality.
    toks = max(len(text.split()), 1)
    tps = toks / max(secs, 1e-9)
    est2 = t.estimate_cost_s("Reply with exactly: HELLO")
    check("Q3", "estimate_conservative_vs_measured",
          est2 >= secs, f"estimate {est2:.2f}s < measured {secs:.2f}s "
          f"({tps:.1f} tok/s)")
    print(f"    [measured throughput: {tps:.1f} tok/s on warm-up]")


# ---------------------------------------------------------------- Q4 ----

def fresh_provider():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=RefusingNative(), teacher=teacher())
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="qwen3-proof", budget_s=400.0)
    assert sr.ok, f"spawn refused: {sr.refusal}"
    return sub, prov, sr.mc.mc_id


def q4_end_to_end_borrow():
    sub, prov, mc_id = fresh_provider()
    # Budget covers the calibrated real-teacher estimate (~220 s on this
    # host); a smaller budget would correctly defer, which Q6 proves.
    grant = make_grant(300.0)
    before = sub.get(mc_id).remaining_s
    prompt = "In one short sentence, what is 7 times 6?"
    res = prov.request_cognition(
        mc_id=mc_id, prompt=prompt,
        context={"frm_grant": grant, "purpose": "reasoning",
                 "target_profile": "laptop-cpu"})
    check("Q4", "borrow_ok", res.ok, res.error)
    check("Q4", "provenance_real_teacher",
          res.provenance == f"borrowed:qwen3@{QWEN3_GGUF_REVISION}",
          res.provenance)
    check("Q4", "native_refusal_on_record",
          res.native_refusal == "precision_tier:cannot_reason_about_x",
          res.native_refusal)
    check("Q4", "real_text_not_stub",
          res.text and "[STUB-TEACHER" not in res.text, res.text[:80])
    consumed = prov.grant_consumed_s(grant.grant_id)
    check("Q4", "grant_consumed", consumed > 0, str(consumed))
    after = sub.get(mc_id).remaining_s
    check("Q4", "mc_charged_exactly_consumed",
          abs((before - after) - consumed) < 1e-9,
          f"before={before} after={after} consumed={consumed}")
    tel = prov.telemetry
    check("Q4", "telemetry_borrowed",
          tel.counts().get("borrowed", 0) == 1, str(tel.counts()))
    check("Q4", "borrow_ratio_one",
          tel.borrow_ratio(purpose="reasoning",
                           target_profile="laptop-cpu") == 1.0,
          str(tel.borrow_ratio()))
    check("Q4", "result_id_present", bool(res.result_id))
    print(f"    [teacher said: {res.text[:100]!r}]")


# ---------------------------------------------------------------- Q5 ----

def q5_grantless_refused():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=RefusingNative(), teacher=teacher())
    tel = prov.telemetry
    res = prov.request_cognition(
        mc_id="mc-x", prompt="reason about X",
        context={"purpose": "p", "target_profile": "t"})
    check("Q5", "refused", not res.ok)
    check("Q5", "no_grant_reason",
          "cognition_refused:no_grant" in res.error, res.error)
    check("Q5", "telemetry_refused_no_grant",
          tel.counts().get("refused_no_grant", 0) == 1,
          str(tel.counts()))


# ---------------------------------------------------------------- Q6 ----

def q6_insufficient_deferred():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=RefusingNative(), teacher=teacher())
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="qwen3-proof", budget_s=200.0)
    mc_id = sr.mc.mc_id
    before = sub.get(mc_id).remaining_s
    grant = make_grant(0.05)  # far below any real-teacher estimate
    tel = prov.telemetry
    res = prov.request_cognition(
        mc_id=mc_id, prompt="x" * 500,
        context={"frm_grant": grant, "purpose": "p",
                 "target_profile": "t"})
    check("Q6", "deferred", not res.ok and "deferred" in res.error,
          res.error)
    check("Q6", "zero_charge",
          sub.get(mc_id).remaining_s == before,
          f"{before} -> {sub.get(mc_id).remaining_s}")
    check("Q6", "grant_unconsumed",
          prov.grant_consumed_s(grant.grant_id) == 0.0,
          str(prov.grant_consumed_s(grant.grant_id)))
    check("Q6", "telemetry_deferred",
          tel.counts().get("deferred_insufficient_grant", 0) == 1,
          str(tel.counts()))


# ---------------------------------------------------------------- Q7 ----

def q7_wrong_grant_refused():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=RefusingNative(), teacher=teacher())
    tel = prov.telemetry
    from swarm_engine.primitives.core import Effect
    bad = PrimitiveGrant(effect=Effect.READ_FS, pattern="*")
    res = prov.request_cognition(
        mc_id="mc-x", prompt="reason",
        context={"frm_grant": bad, "purpose": "p"})
    check("Q7", "refused", not res.ok)
    check("Q7", "bad_grant_reason",
          "cognition_refused:grant_not_frmgrant" in res.error, res.error)
    check("Q7", "telemetry_refused_bad_grant",
          tel.counts().get("refused_bad_grant", 0) == 1,
          str(tel.counts()))


# ---------------------------------------------------------------- Q8 ----

def q8_exhausted_pool_spawn_refused():
    sub = MicrocontrollerSubstrate()
    sub.register_loop("run", budget_s=1.0)
    first = sub.spawn("run", purpose="fill", budget_s=1.0)
    check("Q8", "first_spawn_ok", first.ok,
          getattr(first, "refusal", ""))
    second = sub.spawn("run", purpose="overfill", budget_s=1.0)
    check("Q8", "exhausted_pool_refused", not second.ok,
          "spawn admitted past the pool")


# ---------------------------------------------------------------- Q9 ----

def q9_telemetry_real_borrow():
    sub, prov, mc_id = fresh_provider()
    tel = prov.telemetry
    # Budget covers the calibrated real-teacher estimate (~215 s on this
    # host); the 0.05 s grant below proves the deferral contrast.
    grant = make_grant(300.0)
    res = prov.request_cognition(
        mc_id=mc_id, prompt="Say OK.",
        context={"frm_grant": grant, "purpose": "reasoning",
                 "target_profile": "laptop-cpu"})
    assert res.ok, res.error
    check("Q9", "borrow_ratio_reflects_borrow",
          tel.borrow_ratio(purpose="reasoning",
                           target_profile="laptop-cpu") == 1.0,
          str(tel.borrow_ratio()))
    small = make_grant(0.05)
    res2 = prov.request_cognition(
        mc_id=mc_id, prompt="x" * 500,
        context={"frm_grant": small, "purpose": "reasoning",
                 "target_profile": "laptop-cpu"})
    assert not res2.ok
    check("Q9", "deferral_rate_reflects_deferral",
          tel.deferral_rate(purpose="reasoning") == 0.5,
          str(tel.deferral_rate()))
    check("Q9", "counts_both", tel.counts().get("borrowed", 0) == 1
          and tel.counts().get("deferred_insufficient_grant", 0) == 1,
          str(tel.counts()))


# ---------------------------------------------------------------- Q10 ----

def q10_no_silent_charge():
    sub = MicrocontrollerSubstrate()
    prov = GrantedCognitionProvider(
        substrate=sub, native=RefusingNative(), teacher=teacher())
    sub.register_loop("run", budget_s=600.0)
    sr = sub.spawn("run", purpose="qwen3-proof", budget_s=200.0)
    mc_id = sr.mc.mc_id
    before = sub.get(mc_id).remaining_s
    # Refused: no grant. Deferred: tiny grant. Neither may charge.
    prov.request_cognition(mc_id=mc_id, prompt="a",
                           context={"purpose": "p"})
    prov.request_cognition(
        mc_id=mc_id, prompt="b" * 500,
        context={"frm_grant": make_grant(0.05), "purpose": "p"})
    check("Q10", "no_silent_charge",
          sub.get(mc_id).remaining_s == before,
          f"{before} -> {sub.get(mc_id).remaining_s}")


def main():
    q1_license_gate()
    q2_weight_integrity()
    q3_teacher_slot()
    q4_end_to_end_borrow()
    q5_grantless_refused()
    q6_insufficient_deferred()
    q7_wrong_grant_refused()
    q8_exhausted_pool_spawn_refused()
    q9_telemetry_real_borrow()
    q10_no_silent_charge()
    print(f"\nQWEN3-ACQUIRE-1 proof: {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("FAIL:", f)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
