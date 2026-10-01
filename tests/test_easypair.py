#!/usr/bin/env python3
"""RD-EASYPAIR-1 bench battery: discovery + tap-to-pair + tablet routes.

Tests the REAL code (vendored rd_easypair modules, real TLS, real
remote_dispatch_inproc with a scratch DB) against fake tablets and
fake beacons. No mocks of the system under test; the fakes are the
*peers* (tablet/beacon), which is what the wire format is for.

Run: python3 tests/test_easypair.py
Exit 0 = all green; non-zero = failure with the failing case named.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time

# -- paths ---------------------------------------------------------------
_WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PAYLOAD = os.path.expanduser("~/workspace/remor_mobile/payload_src")
sys.path.insert(0, os.path.join(_PAYLOAD, "runtime", "vendor"))
sys.path.insert(0, os.path.join(_PAYLOAD, "app"))

from rd_easypair import discovery as disc  # noqa: E402
from rd_easypair import pairing_client as pc  # noqa: E402
from rd_easypair import protocol  # noqa: E402
from rd_easypair import tls  # noqa: E402

import remote_dispatch_inproc as inproc  # noqa: E402

_PASS = []
_FAIL = []


def check(name, cond, detail=""):
    if cond:
        _PASS.append(name)
        print("  ok: %s" % name)
    else:
        _FAIL.append(name)
        print("  FAIL: %s %s" % (name, detail))


# -- Part 1: discovery -----------------------------------------------------

def _beacon(device_id="tab-1", device_name="Test Tablet", port=41234,
            fp="a" * 64, ts=None):
    return json.dumps({
        "proto": "rd-disc/1",
        "device_id": device_id,
        "device_name": device_name,
        "port": port,
        "cert_fingerprint": fp,
        "ts": ts if ts is not None else time.time(),
    }).encode()


def test_discovery():
    print("== discovery ==")
    # Valid beacon parses.
    b = disc.parse_beacon(_beacon())
    check("valid beacon parses",
          b is not None and b["device_id"] == "tab-1"
          and b["port"] == 41234 and b["cert_fingerprint"] == "a" * 64)
    # Invalid beacons drop (fail-closed).
    bad_cases = {
        "wrong proto": _beacon().replace(b"rd-disc/1", b"rd-disc/9"),
        "bad device_id charset": _beacon(device_id="tab 1!"),
        "empty device_id": _beacon(device_id=""),
        "bad fingerprint": _beacon(fp="zz"),
        "short fingerprint": _beacon(fp="a" * 63),
        "bad port": _beacon(port=99999),
        "zero port": _beacon(port=0),
        "stale ts": _beacon(ts=time.time() - 120),
        "future ts": _beacon(ts=time.time() + 120),
        "not json": b"hello",
        "oversized": b"x" * 3000,
        "missing fields": json.dumps({"proto": "rd-disc/1"}).encode(),
    }
    for name, raw in bad_cases.items():
        check("drop: %s" % name, disc.parse_beacon(raw) is None)

    # Listener registry: device appears, expires. (Real UDP loopback
    # sendto is blocked in this sandbox, so beacons are injected via the
    # test hook; _inject feeds the same registry the socket loop writes.)
    listener = disc.DiscoveryListener(port=48761)
    listener.start()
    try:
        b = disc.parse_beacon(_beacon(device_id="live-tab"))
        b["host"] = "127.0.0.1"
        b["last_seen"] = time.time()
        listener._inject(b)
        near = listener.nearby()
        check("listener sees beacon",
              any(d["device_id"] == "live-tab" for d in near),
              ("near=%r" % (near,)))
        check("listener captures host",
              any(d.get("host") == "127.0.0.1" for d in near))
        # Expired device drops (inject a stale one).
        listener._inject({
            "device_id": "stale-tab", "device_name": "Stale",
            "host": "127.0.0.1", "port": 1,
            "cert_fingerprint": "b" * 64,
            "last_seen": time.time() - 60})
        near = listener.nearby()
        check("expired device dropped",
              not any(d["device_id"] == "stale-tab" for d in near))
    finally:
        listener.stop()


# -- Part 2: pairing protocol (fake tablet over real TLS) ------------------

class FakeTablet:
    """A Python tablet speaking the real pairing wire format over real
    TLS. The *peer* is fake; the channel and the phone's client are real.
    """

    def __init__(self, auto_approve=False):
        self.auto_approve = auto_approve
        self.cert_dir = tempfile.mkdtemp(prefix="fake-tablet-")
        self.cert_path, self.key_path = tls.ensure_cert(self.cert_dir)
        self.fingerprint = tls.fingerprint(self.cert_path)
        self._pending = {}  # pairing_id -> {code, approved}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        ctx = tls.server_context(self.cert_path, self.key_path)
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", 0))
        self._lsock.listen(5)
        self.port = self._lsock.getsockname()[1]
        self._ctx = ctx
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        self._lsock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                raw, _ = self._lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            t = threading.Thread(target=self._handle, args=(raw,),
                                 daemon=True)
            t.start()

    def _handle(self, raw):
        try:
            conn = self._ctx.wrap_socket(raw, server_side=True)
            conn.settimeout(10)
            msg = protocol.decode(conn)
            kind, body = msg.get("kind"), msg.get("body") or {}
            if kind == "pair_request":
                pid = "pair-test-%d" % int(time.time() * 1000)
                code = "482916"
                with self._lock:
                    self._pending[pid] = {"code": code,
                                          "approved": self.auto_approve}
                protocol.send(conn, "pair_pending", "", 0,
                              {"pairing_id": pid, "code": code})
            elif kind == "pair_confirm":
                pid = body.get("pairing_id", "")
                with self._lock:
                    p = self._pending.get(pid)
                if p is None:
                    protocol.send(conn, "pair_refused", "", 0,
                                  {"reason": "unknown pairing"})
                elif not p["approved"]:
                    protocol.send(conn, "pair_refused", "", 0,
                                  {"reason": "not approved"})
                else:
                    with self._lock:
                        del self._pending[pid]
                    protocol.send(conn, "pair_ok", "", 0,
                                  {"agent_token": "tok-for-test"})
            elif kind == "pair_abort":
                with self._lock:
                    self._pending.pop(body.get("pairing_id", ""), None)
                protocol.send(conn, "pair_aborted", "", 0, {})
            else:
                protocol.send(conn, "pair_refused", "", 0,
                              {"reason": "unknown kind"})
        except Exception:
            pass
        finally:
            try:
                raw.close()
            except OSError:
                pass

    def approve(self, pairing_id):
        with self._lock:
            if pairing_id in self._pending:
                self._pending[pairing_id]["approved"] = True
                return True
            return False

    def stop(self):
        self._stop.set()
        try:
            self._lsock.close()
        except OSError:
            pass
        shutil.rmtree(self.cert_dir, ignore_errors=True)


def test_pairing_protocol():
    print("== pairing protocol (real TLS) ==")
    tab = FakeTablet()
    try:
        # Happy path: request -> approve -> confirm -> pair_ok.
        c = pc.TapPairingClient("127.0.0.1", tab.port, tab.fingerprint)
        c.connect()
        try:
            r = c.request("Test Phone", "phone-1", "tab-1",
                          "agent-1", "tok-1", "http://1.2.3.4:8766")
            check("pair_request -> pair_pending with code",
                  r.get("pairing_id") and r.get("code") == "482916",
                  "r=%r" % (r,))
            # Rogue confirm without approval -> refused.
            c2 = pc.TapPairingClient("127.0.0.1", tab.port,
                                     tab.fingerprint)
            c2.connect()
            try:
                try:
                    c2.confirm(r["pairing_id"])
                    check("rogue confirm without approval refused", False,
                          "confirm unexpectedly succeeded")
                except pc.PairingRefused as e:
                    check("rogue confirm without approval refused", True)
            finally:
                c2.close()
            # Approve, then confirm on a FRESH connection -> pair_ok
            # (one pairing frame per TLS connection; the tablet closes
            # after answering).
            check("tablet approve", tab.approve(r["pairing_id"]))
            c.close()
            c = pc.TapPairingClient("127.0.0.1", tab.port, tab.fingerprint)
            c.connect()
            ok = c.confirm(r["pairing_id"])
            check("approved confirm -> pair_ok",
                  ok.get("agent_token") == "tok-for-test")
            # Replayed confirm -> refused (single-use).
            c3 = pc.TapPairingClient("127.0.0.1", tab.port,
                                     tab.fingerprint)
            c3.connect()
            try:
                try:
                    c3.confirm(r["pairing_id"])
                    check("replayed confirm refused", False,
                          "second confirm unexpectedly succeeded")
                except pc.PairingRefused:
                    check("replayed confirm refused", True)
            finally:
                c3.close()
        finally:
            c.close()

        # Wrong pin -> TLS fails closed (no frames exchanged).
        c4 = pc.TapPairingClient("127.0.0.1", tab.port, "c" * 64)
        try:
            c4.connect()
            check("wrong pin fails TLS", False, "connect succeeded")
        except pc.PairingError:
            check("wrong pin fails TLS", True)
        finally:
            c4.close()

        # Unknown pairing_id -> refused with reason.
        c5 = pc.TapPairingClient("127.0.0.1", tab.port, tab.fingerprint)
        c5.connect()
        try:
            try:
                c5.confirm("pair-nope")
                check("unknown pairing_id refused", False)
            except pc.PairingRefused as e:
                check("unknown pairing_id refused", "unknown" in str(e))
        finally:
            c5.close()

        # Abort -> pairing gone (fresh connection, like the real
        # phone code: one pairing frame per TLS connection).
        c6 = pc.TapPairingClient("127.0.0.1", tab.port, tab.fingerprint)
        c6.connect()
        try:
            r = c6.request("P", "p", "d", "a", "t", "http://1.2.3.4:8766")
        finally:
            c6.close()
        c7 = pc.TapPairingClient("127.0.0.1", tab.port, tab.fingerprint)
        c7.connect()
        try:
            c7.abort(r["pairing_id"])  # never raises
            check("abort does not raise", True)
        finally:
            c7.close()
        check("aborted pairing gone",
              not tab.approve(r["pairing_id"]))
    finally:
        tab.stop()


# -- Part 3: phone routes (real inproc, scratch DB) ------------------------

def _boot_inproc():
    tmp = tempfile.mkdtemp(prefix="easypair-inproc-")
    inproc.DATA_DIR = tmp
    inproc.configure(os.path.join(_WORKTREE, "pylib"))
    inproc.rd_reset()
    inproc.rd_boot()
    return tmp


def test_phone_routes():
    print("== phone routes ==")
    tmp = _boot_inproc()
    try:
        # nearby with no beacons -> empty list (honest, not an error).
        r = inproc.rd_handle("GET", "/api/remote/nearby", {})
        check("nearby empty -> ok with []",
              r.get("ok") and r.get("devices") == [], "r=%r" % (r,)[:120])

        # Fake tablet for tap-to-pair (auto-approve).
        tab = FakeTablet(auto_approve=True)
        try:
            # Inject a beacon so nearby finds it: use a real UDP send to
            # the discovery port is racy; instead call _pair_tap directly
            # with the tablet's real endpoint (what nearby would return).
            tap_body = {
                "device_id": "test-tablet-1",
                "device_name": "Test Tablet",
                "host": "127.0.0.1",
                "port": tab.port,
                "cert_fingerprint": tab.fingerprint,
                "phone_name": "Test Phone",
            }
            r = inproc.rd_handle("POST", "/api/remote/pair/tap", tap_body)
            check("pair/tap -> pairing_id + code",
                  r.get("ok") and r.get("pairing_id") and r.get("code"),
                  "r=%r" % (r,)[:200])
            if not r.get("ok"):
                return
            pid = r["pairing_id"]
            check("tap code is 6 digits",
                  len(str(r["code"])) == 6 and str(r["code"]).isdigit())

            # Confirm -> ok (tablet auto-approved).
            r2 = inproc.rd_handle("POST", "/api/remote/pair/confirm",
                                  {"pairing_id": pid})
            check("pair/confirm -> ok",
                  r2.get("ok") and r2.get("device_id") == "test-tablet-1",
                  "r2=%r" % (r2,)[:200])

            # The device is registered via the verified pair route.
            r3 = inproc.rd_handle("GET", "/api/remote/devices", {})
            devs = r3.get("devices") or []
            check("device registered after tap-pair",
                  any(d.get("device_id") == "test-tablet-1" for d in devs))

            # Confirm again -> refused (pairing consumed).
            r4 = inproc.rd_handle("POST", "/api/remote/pair/confirm",
                                  {"pairing_id": pid})
            check("second confirm refused",
                  not r4.get("ok"), "r4=%r" % (r4,)[:120])

            # Tap with missing fields -> honest error (not a crash).
            r5 = inproc.rd_handle("POST", "/api/remote/pair/tap",
                                  {"device_id": "x"})
            check("tap missing fields -> ok:false",
                  not r5.get("ok") and "required" in str(r5.get("error")))
        finally:
            tab.stop()

        # -- tablet-facing routes -------------------------------------
        # Get the agent credential for the paired device.
        import sqlite3
        conn = sqlite3.connect(inproc._rd["svc"].db_path)
        row = conn.execute(
            "SELECT agent_id FROM rd_devices WHERE device_id=?",
            ("test-tablet-1",)).fetchone()
        conn.close()
        # agent_id is stored; the token is in the authz registry. We
        # cannot read the token back (it's a secret), so exercise the
        # auth paths via rd_handle_tablet with a WRONG token (refused)
        # and no token (refused).
        hdr_none = {}
        r = inproc.rd_handle_tablet("GET", "/api/remote/sessions", {},
                                    hdr_none)
        check("tablet sessions without auth refused",
              not r.get("ok") and "required" in str(r.get("error")),
              "r=%r" % (r,)[:120])
        r = inproc.rd_handle_tablet(
            "GET", "/api/remote/sessions", {},
            {"X-Agent-Id": row[0], "X-Agent-Token": "wrong-token"})
        check("tablet sessions with wrong token refused",
              not r.get("ok") and "authentication failed" in str(r.get("error")),
              ("r=%r" % (r,))[:120])

        # Unknown route -> not found (no information).
        r = inproc.rd_handle_tablet("GET", "/api/remote/nope", {}, hdr_none)
        check("tablet unknown route -> not found",
              r == {"ok": False, "error": "not found"})

        # Consent on unknown session -> honest error (auth first: use a
        # syntactically valid but unknown agent -> refused at auth).
        r = inproc.rd_handle_tablet(
            "POST", "/api/remote/sessions/sess_nope/target-consent",
            {"agent_id": "nope", "agent_token": "nope"}, {})
        check("tablet consent unknown agent refused", not r.get("ok"))

        # -- positive tablet flow with a REAL agent credential ---------
        # Pair via the verified route to obtain a real agent token.
        rp = inproc.rd_handle("POST", "/api/remote/pair",
                              {"device_id": "tablet-pos-1",
                               "display_name": "Pos Tablet"})
        check("verified pair for tablet test",
              rp.get("ok") and rp.get("agent_token"))
        if rp.get("ok"):
            aid, atok = rp["agent_id"], rp["agent_token"]
            hdr = {"X-Agent-Id": aid, "X-Agent-Token": atok}
            # Request a session (phone side, verified route).
            rs = inproc.rd_handle(
                "POST", "/api/remote/sessions",
                {"device_id": "tablet-pos-1",
                 "scope": {"actions": ["click"], "ttl_s": 600}})
            check("session requested",
                  rs.get("ok") and rs.get("session_id"))
            if rs.get("ok"):
                sid = rs["session_id"]
                # Tablet polls: sees its pending session with scope.
                rl = inproc.rd_handle_tablet(
                    "GET", "/api/remote/sessions", {}, hdr)
                found = [x for x in (rl.get("sessions") or [])
                         if x.get("session_id") == sid]
                check("tablet poll sees pending session with scope",
                      rl.get("ok") and len(found) == 1
                      and found[0].get("state") == "pending"
                      and isinstance(found[0].get("scope"), dict))
                # Tablet grants consent.
                rc = inproc.rd_handle_tablet(
                    "POST",
                    "/api/remote/sessions/%s/target-consent" % sid,
                    {"agent_id": aid, "agent_token": atok}, {})
                check("tablet consent -> token_hash",
                      rc.get("ok") and rc.get("token_hash")
                      and rc.get("consent_expires_at", 0) > time.time())
                # Phone retrieves the session token (localhost).
                rt = inproc.rd_handle(
                    "GET", "/api/remote/sessions/%s/token" % sid, {})
                check("phone retrieves session token",
                      rt.get("ok") and rt.get("session_token", "")
                      .startswith("rdsess_"))
                # The hash the tablet got verifies the phone's token.
                import hashlib as _hl
                check("token_hash matches token",
                      _hl.sha256(
                          rt["session_token"].encode()).hexdigest()
                      == rc["token_hash"])
                # Second consent -> refused (no longer pending).
                rc2 = inproc.rd_handle_tablet(
                    "POST",
                    "/api/remote/sessions/%s/target-consent" % sid,
                    {"agent_id": aid, "agent_token": atok}, {})
                check("second consent refused",
                      not rc2.get("ok")
                      and "not pending" in str(rc2.get("error")))
                # Token for a pending (unconsented) session -> refused.
                rs2 = inproc.rd_handle(
                    "POST", "/api/remote/sessions",
                    {"device_id": "tablet-pos-1",
                     "scope": {"actions": ["click"]}})
                rt2 = inproc.rd_handle(
                    "GET", "/api/remote/sessions/%s/token" % rs2["session_id"],
                    {})
                check("token for pending session refused",
                      not rt2.get("ok"))
    finally:
        inproc.rd_reset()
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("RD-EASYPAIR-1 bench battery")
    test_discovery()
    test_pairing_protocol()
    test_phone_routes()
    print("\n%d passed, %d failed" % (len(_PASS), len(_FAIL)))
    if _FAIL:
        print("FAILURES:")
        for f in _FAIL:
            print("  - %s" % f)
        sys.exit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
