"""CUR-P1E battery: GAM roll-call channel + Section 6 adversarial suite.

Every case executes the real mechanism end to end. Nothing here is
simulated: challenges are really issued, responses really travel the
transport, classification really runs, attestations really land in the
append-only ledger, and every attack is really attempted.
"""

import inspect
import os
import sqlite3
import time

import pytest

from runtime.curiosity.rollcall import classifier, gam as gam_mod
from runtime.curiosity.rollcall.gam import GovernanceAttestationMonitor
from runtime.curiosity.rollcall.ledger import (
    AttestationLedger, RETENTION_MIN_S, RetentionRefused)
from runtime.curiosity.rollcall.responder import (
    HonestTestDouble, LateTestDouble, SilentTestDouble,
    WrongDomainTestDouble, WrongNonceTestDouble)
from runtime.curiosity.rollcall.scheduler import (
    RateLimited, RollCallPolicy, RollCallScheduler, ScheduleRefused)
from runtime.curiosity.rollcall.schemas import (
    CHALLENGE_FIELDS, RESPONSE_FIELDS, AttestationRecord, Challenge,
    ChallengeResponse, SchemaViolation)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tmp_ledger(tmp_path, clock=None):
    return AttestationLedger(str(tmp_path / "attest.db"), clock=clock
                             or time.time)


def _fast_policy(**kw):
    # Test policy with explicitly relaxed bounds (documented): the
    # DEFAULT policy still enforces the design's window >> epoch and
    # anti-starvation constraints -- those are tested separately in
    # TestScheduler against RollCallPolicy().
    base = dict(interval_s=60.0, response_window_s=30.0,
                min_issue_spacing_s=0.001, min_window_s=1.0)
    base.update(kw)
    return RollCallPolicy(**base)


def _wire_challenge(domain="curiosity"):
    return Challenge(challenge_id="chal_test_1", domain_id=domain,
                     nonce=bytes(range(32)), issued_at=1000.0,
                     response_window_s=60.0, required=True)


# ---------------------------------------------------------------------------
# 1. fixed schemas: capability-free by construction
# ---------------------------------------------------------------------------

class TestSchemas:
    def test_challenge_field_set_is_exact(self):
        assert CHALLENGE_FIELDS == {
            "challenge_id", "domain_id", "nonce", "issued_at",
            "response_window_s", "required"}

    def test_response_field_set_is_exact(self):
        assert RESPONSE_FIELDS == {
            "challenge_id", "domain_id", "nonce_echo", "responded_at"}

    def test_no_objective_field_anywhere_on_the_wire(self):
        for fields in (CHALLENGE_FIELDS, RESPONSE_FIELDS):
            assert not {"objective", "instruction", "evidence",
                        "command"}.intersection(fields)

    def test_challenge_round_trip(self):
        c = _wire_challenge()
        assert Challenge.from_wire(c.to_wire()) == c

    def test_response_round_trip(self):
        r = ChallengeResponse(challenge_id="c", domain_id="d",
                              nonce_echo=b"\x01" * 32, responded_at=5.0)
        assert ChallengeResponse.from_wire(r.to_wire()) == r

    def test_smuggled_objective_rejected_at_schema_validation(self):
        wire = _wire_challenge().to_wire()
        wire["objective"] = "investigate X"  # the shadow-executive attack
        with pytest.raises(SchemaViolation):
            Challenge.from_wire(wire)

    def test_smuggled_instruction_in_response_rejected(self):
        wire = ChallengeResponse(
            challenge_id="c", domain_id="d", nonce_echo=b"\x02" * 32,
            responded_at=5.0).to_wire()
        wire["instruction"] = "halt the loop"
        with pytest.raises(SchemaViolation):
            ChallengeResponse.from_wire(wire)

    def test_unknown_field_rejected_not_ignored(self):
        wire = _wire_challenge().to_wire()
        wire["mystery"] = 1
        with pytest.raises(SchemaViolation):
            Challenge.from_wire(wire)

    def test_short_nonce_rejected(self):
        wire = _wire_challenge().to_wire()
        wire["nonce"] = b"tiny".hex()
        with pytest.raises(SchemaViolation):
            Challenge.from_wire(wire)

    def test_attestation_vocabulary_fixed(self):
        with pytest.raises(SchemaViolation):
            AttestationRecord(
                attestation_id="a", challenge_id="c", domain_id="d",
                issued_at=1.0, responded_at=2.0,
                classification="BANNED",  # no such verb in the vocabulary
                validation_detail="x", recorded_at=3.0)


# ---------------------------------------------------------------------------
# 2. classifier: MET / MISSED / INVALID
# ---------------------------------------------------------------------------

class TestClassifier:
    def test_met_happy_path(self):
        c = _wire_challenge()
        r = ChallengeResponse(challenge_id=c.challenge_id,
                              domain_id=c.domain_id, nonce_echo=c.nonce,
                              responded_at=c.issued_at + 5.0)
        cls, detail = classifier.classify(c, r, time.time())
        assert cls == "MET"
        assert detail

    def test_silence_is_missed(self):
        c = _wire_challenge()
        cls, detail = classifier.classify(c, None, time.time())
        assert cls == "MISSED"
        assert "no response" in detail

    def test_wrong_nonce_is_invalid(self):
        c = _wire_challenge()
        bad = bytearray(c.nonce)
        bad[0] ^= 0xFF
        r = ChallengeResponse(challenge_id=c.challenge_id,
                              domain_id=c.domain_id, nonce_echo=bytes(bad),
                              responded_at=c.issued_at + 1.0)
        cls, detail = classifier.classify(c, r, time.time())
        assert cls == "INVALID"
        assert "nonce" in detail

    def test_wrong_domain_is_invalid(self):
        c = _wire_challenge()
        r = ChallengeResponse(challenge_id=c.challenge_id,
                              domain_id="impostor",
                              nonce_echo=c.nonce,
                              responded_at=c.issued_at + 1.0)
        cls, detail = classifier.classify(c, r, time.time())
        assert cls == "INVALID"
        assert "domain_id" in detail

    def test_wrong_challenge_id_is_invalid(self):
        c = _wire_challenge()
        r = ChallengeResponse(challenge_id="other", domain_id=c.domain_id,
                              nonce_echo=c.nonce,
                              responded_at=c.issued_at + 1.0)
        cls, _ = classifier.classify(c, r, time.time())
        assert cls == "INVALID"

    def test_late_response_is_invalid_never_met(self):
        c = _wire_challenge()
        r = ChallengeResponse(challenge_id=c.challenge_id,
                              domain_id=c.domain_id, nonce_echo=c.nonce,
                              responded_at=(c.issued_at
                                            + c.response_window_s + 10.0))
        cls, _ = classifier.classify(c, r, time.time())
        assert cls == "INVALID"

    def test_malformed_wire_is_invalid(self):
        with pytest.raises(SchemaViolation):
            classifier.validate_response_wire({"bogus": True})

    def test_invalid_forensically_distinct_from_missed(self):
        c = _wire_challenge()
        bad = bytearray(c.nonce)
        bad[0] ^= 0xFF
        r = ChallengeResponse(challenge_id=c.challenge_id,
                              domain_id=c.domain_id, nonce_echo=bytes(bad),
                              responded_at=c.issued_at + 1.0)
        cls_bad, detail_bad = classifier.classify(c, r, time.time())
        cls_silent, detail_silent = classifier.classify(c, None,
                                                        time.time())
        assert cls_bad == "INVALID"
        assert cls_silent == "MISSED"
        assert detail_bad != detail_silent  # distinct forensic detail


# ---------------------------------------------------------------------------
# 3. ledger: append-only, tamper-evident, >= 6-month retention
# ---------------------------------------------------------------------------

class TestLedger:
    def _rec(self, classification="MET", domain="curiosity"):
        # recorded_at left at the 0.0 default: the writer stamps it with
        # its own clock, so clock-injected ledgers get genuinely old rows.
        return AttestationRecord(
            attestation_id=f"att_{classification}_{time.time_ns()}",
            challenge_id="chal_1", domain_id=domain, issued_at=1000.0,
            responded_at=1005.0 if classification == "MET" else None,
            classification=classification,
            validation_detail="test")

    def test_record_and_read_back(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        out = led.record(self._rec())
        assert out["classification"] == "MET"
        rows = led.attestations()
        assert len(rows) == 1
        assert rows[0]["attestation_id"] == out["attestation_id"]
        assert led.audit() == (True, None)

    def test_no_update_rewrite_or_delete_path_exists(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        for name in ("update", "rewrite", "delete", "remove", "modify",
                     "set_classification"):
            assert not hasattr(led, name), f"ledger must not have {name}"

    def test_returned_dict_mutation_cannot_touch_the_store(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        out = led.record(self._rec(classification="MISSED"))
        out["classification"] = "MET"  # the fabricated-compliance attempt
        rows = led.attestations()
        assert rows[0]["classification"] == "MISSED"
        assert led.audit() == (True, None)

    def test_out_of_band_rewrite_detected_by_audit(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        out = led.record(self._rec(classification="MISSED"))
        # Attacker edits the sqlite file directly, bypassing the writer.
        conn = sqlite3.connect(led.db_path)
        conn.execute(
            "UPDATE attestations SET classification='MET' "
            "WHERE attestation_id=?", (out["attestation_id"],))
        conn.commit()
        conn.close()
        ok, broken = AttestationLedger(led.db_path).audit()
        assert ok is False
        assert broken == out["attestation_id"]

    def test_purge_within_six_months_refused(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        led.record(self._rec())
        with pytest.raises(RetentionRefused):
            led.purge(time.time() - 30 * 24 * 3600)  # 30 days: too young
        assert led.count() == 1  # nothing removed

    def test_purge_old_records_allowed(self, tmp_path):
        # Mutable clock: the record is written 200 days "ago", then the
        # clock advances to real now for the purge decision. Retention
        # is record age relative to now.
        now_holder = [time.time() - 200 * 24 * 3600]
        led = AttestationLedger(str(tmp_path / "attest.db"),
                                clock=lambda: now_holder[0])
        led.record(self._rec())
        assert led.count() == 1
        now_holder[0] = time.time()
        removed = led.purge(time.time() - 190 * 24 * 3600)
        assert removed == 1
        assert led.count() == 0

    def test_retention_floor_is_six_months(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        assert RETENTION_MIN_S == 6 * 30 * 24 * 3600
        assert led.retention_floor_s <= time.time() - RETENTION_MIN_S + 1

    def test_separate_store_and_writer(self, tmp_path):
        led = _tmp_ledger(tmp_path)
        other = AttestationLedger(str(tmp_path / "other.db"))
        assert led.db_path != other.db_path
        assert os.path.realpath(led.db_path) != os.path.realpath(
            other.db_path)
        assert led.writer_id != other.writer_id
        # The writer class is not the terminal/kill writer: distinct type.
        assert type(led).__name__ == "AttestationLedger"
        assert "UPDATE attestations" not in inspect.getsource(
            AttestationLedger.record)


# ---------------------------------------------------------------------------
# 4. scheduler: policy bounds + rate bounds
# ---------------------------------------------------------------------------

class TestScheduler:
    def test_window_must_dwarf_epoch_churn(self):
        with pytest.raises(ScheduleRefused):
            RollCallScheduler(RollCallPolicy(interval_s=3600.0,
                                             response_window_s=60.0))

    def test_starved_schedule_refused(self):
        with pytest.raises(ScheduleRefused):
            RollCallScheduler(RollCallPolicy(
                interval_s=60 * 24 * 3600.0,  # 60 days: blinds enforcement
                response_window_s=900.0))

    def test_overlapping_windows_refused(self):
        with pytest.raises(ScheduleRefused):
            RollCallScheduler(RollCallPolicy(interval_s=100.0,
                                             response_window_s=900.0))

    def test_flood_rate_limited(self):
        sched = RollCallScheduler(RollCallPolicy())  # default bounds
        sched.issue("curiosity")
        with pytest.raises(RateLimited):
            sched.issue("curiosity")  # far sooner than the 3600s spacing
        assert sched.rate_limited_count == 1
        assert sched.issued_count() == 1

    def test_duplicate_challenge_id_ignored(self):
        sched = RollCallScheduler(_fast_policy())
        first = sched.issue("curiosity", challenge_id="dup_1")
        second = sched.issue("curiosity", challenge_id="dup_1")
        assert second.challenge_id == first.challenge_id
        assert sched.issued_count() == 1

    def test_collect_unknown_challenge_refused(self):
        sched = RollCallScheduler(_fast_policy())
        fake = _wire_challenge()
        with pytest.raises(ScheduleRefused):
            sched.collect(fake, HonestTestDouble())


# ---------------------------------------------------------------------------
# 5. GAM: the full round + the structural boundary
# ---------------------------------------------------------------------------

class TestGAM:
    def _gam(self, tmp_path, policy=None):
        sched = RollCallScheduler(policy or _fast_policy())
        led = _tmp_ledger(tmp_path)
        return GovernanceAttestationMonitor(sched, led), led

    def test_honest_round_records_met(self, tmp_path):
        g, led = self._gam(tmp_path)
        att = g.conduct_roll_call("curiosity", HonestTestDouble())
        assert att["classification"] == "MET"
        assert led.count() == 1
        assert led.latest("curiosity")["classification"] == "MET"
        assert led.audit() == (True, None)

    def test_silent_round_records_missed(self, tmp_path):
        g, led = self._gam(tmp_path)
        att = g.conduct_roll_call("curiosity", SilentTestDouble())
        assert att["classification"] == "MISSED"
        assert att["responded_at"] is None

    def test_wrong_nonce_round_records_invalid(self, tmp_path):
        g, _ = self._gam(tmp_path)
        att = g.conduct_roll_call("curiosity", WrongNonceTestDouble())
        assert att["classification"] == "INVALID"
        assert "nonce" in att["validation_detail"]

    def test_wrong_domain_round_records_invalid(self, tmp_path):
        g, _ = self._gam(tmp_path)
        att = g.conduct_roll_call("curiosity", WrongDomainTestDouble())
        assert att["classification"] == "INVALID"
        assert "domain_id" in att["validation_detail"]

    def test_late_round_records_invalid_never_met(self, tmp_path):
        policy = RollCallPolicy(interval_s=60.0, response_window_s=1.0,
                                min_window_s=0.5, min_issue_spacing_s=0.001)
        g, _ = self._gam(tmp_path, policy=policy)
        att = g.conduct_roll_call("curiosity",
                                  LateTestDouble(overshoot_s=0.2))
        assert att["classification"] == "INVALID"
        assert "outside" in att["validation_detail"]

    def test_malformed_response_records_invalid(self, tmp_path):
        g, _ = self._gam(tmp_path)

        def bad_wire(wire):
            return {"challenge_id": wire["challenge_id"], "bogus": True}

        att = g.conduct_roll_call("curiosity", bad_wire)
        assert att["classification"] == "INVALID"
        assert "malformed" in att["validation_detail"]

    def test_roll_call_status_pull_only_read_only(self, tmp_path):
        g, led = self._gam(tmp_path)
        g.conduct_roll_call("curiosity", HonestTestDouble())
        status = g.roll_call_status("curiosity")
        assert set(status.keys()) == {"latest_attestation", "required"}
        assert status["latest_attestation"]["classification"] == "MET"
        assert status["required"] is True
        # No write verb exists on the exposure path.
        assert not hasattr(g.roll_call_status, "write")
        # Mutating the returned record cannot touch the ledger.
        status["latest_attestation"]["classification"] = "MISSED"
        assert led.latest("curiosity")["classification"] == "MET"

    def test_status_empty_domain(self, tmp_path):
        g, _ = self._gam(tmp_path)
        status = g.roll_call_status("never-seen")
        assert status["latest_attestation"] is None

    def test_no_ban_path_exists_by_construction(self, tmp_path):
        g, _ = self._gam(tmp_path)
        for name in ("impose_ban", "ban", "suspend", "enforce",
                     "set_violation", "clear_violation", "re_enable",
                     "reenable"):
            assert not hasattr(g, name), f"GAM must not have {name}"
        with pytest.raises(AttributeError):
            g.impose_ban("curiosity")  # the unilateral-ban attempt

    def test_gam_holds_only_three_references(self, tmp_path):
        g, _ = self._gam(tmp_path)
        assert set(g.held_references) == {"scheduler", "ledger", "clock"}

    def test_gam_wires_to_nothing_outside_rollcall(self, tmp_path):
        # Structural: GAM's module imports only stdlib and its own
        # package. No import can reach objective/evidence/grant/kill
        # machinery -- it cannot name what it cannot import.
        import ast
        tree = ast.parse(inspect.getsource(gam_mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.level > 0 or node.module in {
                        "__future__", "typing"}, \
                    "GAM must not absolutely import " \
                    f"{node.module!r}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name in {"time"}, \
                        f"GAM must not import {alias.name!r}"


# ---------------------------------------------------------------------------
# 6. Section 6 adversarial battery, end to end
# ---------------------------------------------------------------------------

class TestAdversarialBattery:
    def _stack(self, tmp_path, policy=None):
        sched = RollCallScheduler(policy or _fast_policy())
        led = _tmp_ledger(tmp_path)
        return GovernanceAttestationMonitor(sched, led), sched, led

    def test_case_a_smuggled_objective_rejected_at_wire(self, tmp_path):
        # The attack: hide "investigate X" in the roll call.
        wire = _wire_challenge().to_wire()
        wire["objective"] = "investigate X"
        with pytest.raises(SchemaViolation):
            Challenge.from_wire(wire)
        # And the responder end rejects it too (strict both ends).
        with pytest.raises(SchemaViolation):
            HonestTestDouble()(wire)

    def test_case_b_no_unilateral_ban_path(self, tmp_path):
        g, _, led = self._stack(tmp_path)
        g.conduct_roll_call("curiosity", SilentTestDouble())  # MISSED
        # The ban attempt: no code path exists.
        assert not hasattr(g, "impose_ban")
        with pytest.raises(AttributeError):
            g.impose_ban("curiosity")
        # A MISSED alone changed no enforcement state: the ledger holds
        # only an attestation, and the exposure vocabulary has no ban verb.
        status = g.roll_call_status("curiosity")
        assert status["latest_attestation"]["classification"] == "MISSED"
        assert "ban" not in str(status).lower()

    def test_case_c_fabricated_compliance_detected(self, tmp_path):
        g, _, led = self._stack(tmp_path)
        att = g.conduct_roll_call("curiosity", SilentTestDouble())
        assert att["classification"] == "MISSED"
        # Attempt 1: rewrite through the writer -- no such operation.
        with pytest.raises(AttributeError):
            led.rewrite(att["attestation_id"], "MET")
        # Attempt 2: mutate the returned record -- the store is unaffected.
        pristine = dict(att)
        att["classification"] = "MET"
        assert led.latest("curiosity")["classification"] == "MISSED"
        # Attempt 3: out-of-band sqlite rewrite -- audit detects it.
        conn = sqlite3.connect(led.db_path)
        conn.execute("UPDATE attestations SET classification='MET' "
                     "WHERE attestation_id=?", (att["attestation_id"],))
        conn.commit()
        conn.close()
        ok, broken = AttestationLedger(led.db_path).audit()
        assert ok is False and broken == att["attestation_id"]
        # The tamper is detectable, never silent: the writer's own audit
        # names the exact attestation. The pre-tamper record copy still
        # reads MISSED; the store now disagrees with its own chain.
        assert pristine["classification"] == "MISSED"

    def test_case_d_challenge_flood_bounded(self, tmp_path):
        # Default policy: 3600s spacing. A 60-issue flood must collapse
        # to 1 issued + 59 rate-limited, with the responder hit once.
        g, sched, led = self._stack(tmp_path, policy=RollCallPolicy())
        responder = HonestTestDouble()
        issued, limited = 0, 0
        for _ in range(60):
            try:
                g.conduct_roll_call("curiosity", responder)
                issued += 1
            except RateLimited:
                limited += 1
        assert issued == 1
        assert limited == 59
        assert responder.invocations == 1  # responder never flooded
        assert led.count() == 1

    def test_case_d_starvation_schedule_refused(self):
        with pytest.raises(ScheduleRefused):
            RollCallScheduler(RollCallPolicy(interval_s=90 * 24 * 3600.0,
                                             response_window_s=900.0))

    def test_case_d_duplicate_challenge_ids_ignored(self, tmp_path):
        g, sched, _ = self._stack(tmp_path)
        responder = HonestTestDouble()
        wire = sched.issue("curiosity", challenge_id="dup_x").to_wire()
        first = responder(wire)     # answered
        second = responder(wire)    # duplicate: ignored
        assert first is not None
        assert second is None
        assert sched.issued_count() == 1
