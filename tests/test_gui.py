#!/usr/bin/env python3
"""RD-EASYPAIR-1 GUI test: tap-to-pair UI in headless Chromium.

Backend: the REAL remote_dispatch_inproc (scratch DB) + a REAL FakeTablet
(Python tablet speaking the real pairing wire format over real TLS).
The discovery listener is stubbed at the nearby() boundary to return the
fake tablet (UDP loopback send is blocked in this sandbox); everything
from the "Tap to pair" click onward is the real code path.

Verifies:
  1. Scan renders the nearby tablet with a "Tap to pair" button.
  2. Tapping it shows the 6-digit code-comparison step (phone never
     auto-confirms).
  3. Confirm pairs the device; it appears under Paired devices.
  4. Cancel aborts cleanly.
"""
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.expanduser(
    "~/workspace/remor_mobile/payload_src/runtime/vendor"))
sys.path.insert(0, os.path.expanduser(
    "~/workspace/remor_mobile/payload_src/app"))

from rd_easypair import tls  # noqa: E402
import socket, ssl  # noqa: E402
from rd_easypair import protocol  # noqa: E402
import shutil  # noqa: E402

import remote_dispatch_inproc as inproc  # noqa: E402

_STATIC = os.path.expanduser(
    "~/workspace/remor_mobile/payload_src/app/static")

# ---- Fake tablet (same as the bench battery) ---------------------------
import time  # noqa: E402


class FakeTablet:
    def __init__(self, auto_approve=False):
        self.auto_approve = auto_approve
        self.cert_dir = tempfile.mkdtemp(prefix="fake-tablet-gui-")
        self.cert_path, self.key_path = tls.ensure_cert(self.cert_dir)
        self.fingerprint = tls.fingerprint(self.cert_path)
        self._pending = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ctx = tls.server_context(self.cert_path, self.key_path)
        self._lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._lsock.bind(("127.0.0.1", 0))
        self._lsock.listen(5)
        self.port = self._lsock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        self._lsock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                raw, _ = self._lsock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(raw,),
                             daemon=True).start()

    def _handle(self, raw):
        try:
            conn = self._ctx.wrap_socket(raw, server_side=True)
            conn.settimeout(10)
            msg = protocol.decode(conn)
            kind, body = msg.get("kind"), msg.get("body") or {}
            if kind == "pair_request":
                pid = "pair-gui-%d" % int(time.time() * 1000)
                code = "739251"
                with self._lock:
                    self._pending[pid] = {"code": code,
                                          "approved": self.auto_approve}
                protocol.send(conn, "pair_pending", "", 0,
                              {"pairing_id": pid, "code": code})
            elif kind == "pair_confirm":
                pid = body.get("pairing_id", "")
                with self._lock:
                    p = self._pending.get(pid)
                if p is None or not p["approved"]:
                    protocol.send(conn, "pair_refused", "", 0,
                                  {"reason": "not approved"})
                else:
                    with self._lock:
                        del self._pending[pid]
                    protocol.send(conn, "pair_ok", "", 0,
                                  {"agent_token": "tok-gui"})
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

    def approve_all(self):
        with self._lock:
            for p in self._pending.values():
                p["approved"] = True

    def stop(self):
        self._stop.set()
        try:
            self._lsock.close()
        except OSError:
            pass
        shutil.rmtree(self.cert_dir, ignore_errors=True)


TABLET = FakeTablet(auto_approve=False)
TABLET2 = FakeTablet(auto_approve=False)  # fresh device for the cancel path

# ---- Boot the real inproc with a scratch DB -----------------------------
TMP = tempfile.mkdtemp(prefix="easypair-gui-")
inproc.DATA_DIR = TMP
inproc.configure(os.path.expanduser(
    "~/workspace/worktrees/rd-easypair-1/pylib"))
inproc.rd_reset()
inproc.rd_boot()

# Stub the discovery listener class: _nearby_devices() constructs its own
# DiscoveryListener, so patch the class to return the fake tablet instantly
# (no 6s UDP scan; UDP loopback send is blocked in this sandbox).
FAKE_DEVICE = {
    "device_id": "gui-tablet-1",
    "device_name": "GUI Test Tablet",
    "host": "127.0.0.1",
    "port": TABLET.port,
    "cert_fingerprint": TABLET.fingerprint,
    "last_seen": time.time(),
}
FAKE_DEVICE2 = {
    "device_id": "gui-tablet-2",
    "device_name": "GUI Test Tablet 2",
    "host": "127.0.0.1",
    "port": TABLET2.port,
    "cert_fingerprint": TABLET2.fingerprint,
    "last_seen": time.time(),
}


class _StubListener:
    def start(self):
        pass

    def stop(self):
        pass

    def nearby(self):
        out = []
        for base in (FAKE_DEVICE, FAKE_DEVICE2):
            d = dict(base)
            d["last_seen"] = time.time()
            out.append(d)
        return out


import rd_easypair.discovery as _disc_mod
_disc_mod.DiscoveryListener = _StubListener


# ---- Minimal HTTP server: static files + /api/remote/* ------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, name, ctype):
        path = os.path.join(_STATIC, name)
        if not os.path.exists(path):
            self.send_response(404)
            self.end_headers()
            return
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/" or self.path == "/test":
            self._send_file("test_harness.html", "text/html")
        elif self.path == "/view_dispatch.js":
            self._send_file("view_dispatch.js",
                            "application/javascript")
        elif self.path.startswith("/api/remote/"):
            self._send_json(inproc.rd_handle("GET", self.path, {}))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            body = {}
        if self.path.startswith("/api/remote/"):
            self._send_json(inproc.rd_handle("POST", self.path, body))
        else:
            self.send_response(404)
            self.end_headers()


HARNESS = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>easypair GUI test</title></head>
<body>
<div id="dispatch-body"></div>
<script>
// Minimal stubs for the app.js globals view_dispatch.js uses.
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;",
              '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(m) { window.__toast = m; }
</script>
<script src="/view_dispatch.js"></script>
<script>
  renderDispatchView($("dispatch-body"));
  window.__ready = true;
</script>
</body></html>
"""

open(os.path.join(_STATIC, "test_harness.html"), "w").write(HARNESS)


def main():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright

    results = []

    def check(name, cond, detail=""):
        results.append((name, cond))
        print(("  ok: " if cond else "  FAIL: ") + name
              + (" " + str(detail) if detail and not cond else ""))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto("http://127.0.0.1:%d/test" % port)
        page.wait_for_function("window.__ready === true", timeout=10000)

        # 1. Scan -> the fake tablet appears with a Tap to pair button.
        page.click("#rd-scan-btn")
        page.wait_for_selector("[data-nearby-idx]", timeout=10000)
        body_text = page.inner_text("#rd-nearby")
        check("scan lists the tablet",
              "GUI Test Tablet" in body_text, body_text[:200])

        # 2. Tap to pair -> code-comparison step (phone never auto-confirms).
        page.click("[data-nearby-idx]")
        page.wait_for_selector("#rd-tap-confirm", timeout=15000)
        tap_text = page.inner_text("#rd-tap-out")
        check("code comparison shown",
              "Does this code match the tablet?" in tap_text,
              tap_text[:200])
        check("6-digit code shown",
              "739 251" in tap_text, tap_text[:200])
        check("no auto-confirm (Confirm button waits for user)",
              page.is_visible("#rd-tap-confirm"))

        # 3. Tablet approves (simulating the tablet owner tapping ALLOW),
        #    then the user confirms -> paired.
        TABLET.approve_all()
        page.click("#rd-tap-confirm")
        page.wait_for_selector("text=Paired with", timeout=15000)
        done_text = page.inner_text("#rd-tap-out")
        check("confirm -> paired",
              "Paired with" in done_text and "GUI Test Tablet" in done_text,
              done_text[:200])

        # 4. Device appears under Paired devices.
        page.wait_for_selector("text=gui-tablet-1", timeout=10000)
        check("device listed under Paired devices", True)

        # 5. Cancel path: tap the SECOND (unpaired) tablet, then cancel.
        page.click("#rd-scan-btn")
        page.wait_for_selector("[data-nearby-idx='1']", timeout=10000)
        page.click("[data-nearby-idx='1']")
        page.wait_for_selector("#rd-tap-cancel", timeout=15000)
        page.click("#rd-tap-cancel")
        page.wait_for_selector("text=Pairing cancelled", timeout=10000)
        check("cancel -> pairing cancelled", True)

        # 6. No JS page errors.
        check("no JS page errors", not errors, errors[:3])

        browser.close()

    srv.shutdown()
    TABLET.stop()
    TABLET2.stop()
    inproc.rd_reset()
    os.remove(os.path.join(_STATIC, "test_harness.html"))
    shutil.rmtree(TMP, ignore_errors=True)

    failed = [n for n, c in results if not c]
    print("\n%d passed, %d failed" % (len(results) - len(failed),
                                     len(failed)))
    if failed:
        print("FAILURES:")
        for f in failed:
            print("  - %s" % f)
        sys.exit(1)
    print("ALL GREEN")


if __name__ == "__main__":
    main()
