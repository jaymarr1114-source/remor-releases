package com.remor.dispatchtarget.proto;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.regex.Pattern;

/**
 * Tablet-side LAN discovery beacon (RD-EASYPAIR-1): UDP broadcasts so the
 * phone can show a "nearby devices" list for tap-to-pair, replacing the
 * manual controller-URL + token shuffle.
 *
 * <p>Datagram (JSON, UTF-8):
 * {@code {"proto":"rd-disc/1","device_id":"...","device_name":"...",
 * "port":41234,"cert_fingerprint":"<64 hex>","ts":1699999999.0}}
 *
 * <p>The beacon carries NO secrets: the fingerprint is a public identity
 * pin (TOFU), not a credential. Authentication happens over the pinned TLS
 * channel (pair_request) plus human consent + numeric comparison on the
 * tablet. A spoofed beacon can at most add a row to the phone's list.
 *
 * <p>Pure Java: no android.* imports (testable on the JVM).
 */
public final class DiscoveryBeacon {

    public static final int DISCOVERY_PORT = 48766;
    public static final String DISCOVERY_PROTO = "rd-disc/1";
    public static final double BEACON_INTERVAL_S = 5.0;

    private static final Pattern DEVICE_ID_RE =
            Pattern.compile("[A-Za-z0-9_:.\\-]{1,118}");
    private static final Pattern FINGERPRINT_RE =
            Pattern.compile("[0-9a-fA-F]{64}");

    private DiscoveryBeacon() {}

    /** Build one beacon datagram body. Throws on invalid input. */
    public static byte[] build(String deviceId, String deviceName,
                               int port, String certFingerprint) {
        if (deviceId == null || !DEVICE_ID_RE.matcher(deviceId).matches()) {
            throw new IllegalArgumentException(
                    "invalid device_id for beacon");
        }
        String name = (deviceName == null || deviceName.trim().isEmpty())
                ? deviceId : deviceName.trim();
        if (name.length() > 120) {
            throw new IllegalArgumentException(
                    "device_name too long for beacon");
        }
        if (port < 1 || port > 65535) {
            throw new IllegalArgumentException(
                    "port out of range for beacon");
        }
        if (certFingerprint == null
                || !FINGERPRINT_RE.matcher(certFingerprint).matches()) {
            throw new IllegalArgumentException(
                    "cert_fingerprint must be 64 hex chars");
        }
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("proto", DISCOVERY_PROTO);
        m.put("device_id", deviceId);
        m.put("device_name", name);
        m.put("port", (long) port);
        m.put("cert_fingerprint", certFingerprint.toLowerCase());
        m.put("ts", (double) (System.currentTimeMillis() / 1000L));
        return Json.write(m).getBytes(StandardCharsets.UTF_8);
    }

    /**
     * Broadcast loop: sends a beacon every BEACON_INTERVAL_S until
     * {@code stop} is set. Runs on its own daemon thread; call
     * {@link BeaconHandle#stop()} to end it.
     */
    public static BeaconHandle start(String deviceId, String deviceName,
                                     int port, String certFingerprint) {
        BeaconHandle h = new BeaconHandle(deviceId, deviceName, port,
                certFingerprint);
        h.start();
        return h;
    }

    public static final class BeaconHandle {
        private final String deviceId;
        private final String deviceName;
        private final int port;
        private final String certFingerprint;
        private volatile boolean stopped = false;
        private Thread thread;

        BeaconHandle(String deviceId, String deviceName, int port,
                     String certFingerprint) {
            this.deviceId = deviceId;
            this.deviceName = deviceName;
            this.port = port;
            this.certFingerprint = certFingerprint;
        }

        public synchronized void start() {
            if (thread != null) {
                return;
            }
            thread = new Thread(this::loop, "rd-discovery-beacon");
            thread.setDaemon(true);
            thread.start();
        }

        private void loop() {
            while (!stopped) {
                try {
                    byte[] raw = build(deviceId, deviceName, port,
                            certFingerprint);
                    try (DatagramSocket sock = new DatagramSocket()) {
                        sock.setBroadcast(true);
                        DatagramPacket p = new DatagramPacket(raw,
                                raw.length,
                                InetAddress.getByName("255.255.255.255"),
                                DISCOVERY_PORT);
                        sock.send(p);
                    }
                } catch (Exception ignored) {
                    // A failed broadcast is not fatal: retry next interval.
                }
                try {
                    Thread.sleep((long) (BEACON_INTERVAL_S * 1000));
                } catch (InterruptedException e) {
                    return;
                }
            }
        }

        public void stop() {
            stopped = true;
            Thread t;
            synchronized (this) {
                t = thread;
            }
            if (t != null) {
                t.interrupt();
            }
        }
    }
}
