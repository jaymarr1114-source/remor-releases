"""LAN discovery for tap-to-pair dispatch (RD-EASYPAIR-1).

The tablet broadcasts UDP beacons on the LAN; the phone listens and builds
the nearby-devices list the tap-to-pair UI shows. Beacons carry NO secrets:
{device_id, device_name, port, cert_fingerprint} -- the fingerprint is a
public identity pin (TOFU), not a credential. Authentication happens later
over the pinned TLS channel (pair_request) plus the human consent + numeric
comparison on the tablet.

Wire format (JSON datagram, UTF-8):
  {"proto":"rd-disc/1","device_id":"...","device_name":"...",
   "port":41234,"cert_fingerprint":"<64 hex>","ts":1699999999.0}

Validation (fail-closed): wrong proto, missing/empty fields, bad charset on
device_id, non-hex or wrong-length fingerprint, port out of range, or a
stale ts (>30s skew) all drop the datagram silently. A spoofed beacon can
at most put a row in the nearby list -- it cannot pair (TLS pin fails) and
cannot obtain a session (tablet-side consent + code).
"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Dict, List, Optional

DISCOVERY_PORT = 48766
DISCOVERY_PROTO = "rd-disc/1"
BEACON_INTERVAL_S = 5.0
DEVICE_TTL_S = 16.0        # device drops from the list after this without a beacon
MAX_TS_SKEW_S = 30.0

_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9_:.\-]{1,118}")
_FINGERPRINT_RE = re.compile(r"[0-9a-fA-F]{64}")


def parse_beacon(raw: bytes) -> Optional[Dict]:
    """Parse and validate one beacon datagram. None = drop (fail-closed)."""
    try:
        if len(raw) > 2048:
            return None
        m = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(m, dict) or m.get("proto") != DISCOVERY_PROTO:
        return None
    device_id = m.get("device_id")
    device_name = m.get("device_name") or device_id
    port = m.get("port")
    fp = m.get("cert_fingerprint")
    ts = m.get("ts")
    if (not isinstance(device_id, str)
            or not _DEVICE_ID_RE.fullmatch(device_id)):
        return None
    if not isinstance(device_name, str) or not device_name.strip() \
            or len(device_name) > 120:
        return None
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    if not isinstance(fp, str) or not _FINGERPRINT_RE.fullmatch(fp):
        return None
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        return None
    if abs(time.time() - ts_f) > MAX_TS_SKEW_S:
        return None
    return {
        "device_id": device_id,
        "device_name": device_name.strip(),
        "port": port,
        "cert_fingerprint": fp.lower(),
        "beacon_ts": ts_f,
    }


class DiscoveryListener:
    """UDP beacon listener maintaining the nearby-devices registry.

    Thread-safe. Devices expire after DEVICE_TTL_S without a fresh beacon.
    """

    def __init__(self, port: int = DISCOVERY_PORT):
        self._port = port
        self._devices: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        self._sock.bind(("0.0.0.0", self._port))
        self._sock.settimeout(0.5)
        self._thread = threading.Thread(target=self._loop,
                                        name="rd-discovery",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5.0)
        self._thread = None

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                raw, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            dev = parse_beacon(raw)
            if dev is None:
                continue
            dev["host"] = addr[0]
            dev["last_seen"] = time.time()
            with self._lock:
                self._devices[dev["device_id"]] = dev

    def nearby(self) -> List[Dict]:
        """Currently-visible devices, freshest first. Expired ones dropped."""
        now = time.time()
        with self._lock:
            live = {k: v for k, v in self._devices.items()
                    if now - v["last_seen"] <= DEVICE_TTL_S}
            self._devices = live
            return sorted(
                ({"device_id": v["device_id"],
                  "device_name": v["device_name"],
                  "host": v["host"],
                  "port": v["port"],
                  "cert_fingerprint": v["cert_fingerprint"],
                  "last_seen": v["last_seen"]} for v in live.values()),
                key=lambda d: d["last_seen"], reverse=True)

    # -- test hook ------------------------------------------------------
    def _inject(self, dev: Dict) -> None:
        with self._lock:
            self._devices[dev["device_id"]] = dev
