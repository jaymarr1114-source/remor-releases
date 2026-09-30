package com.remor.dispatchtarget.proto;

import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

import javax.imageio.ImageIO;

/**
 * Bench-only Java target main (NOT shipped in the APK): a target
 * implementation of the canonical protocol against the real Python
 * controller, for the interop proof.
 *
 * <p>Stdin admin commands (the harness's honest substitute for the
 * target's own consent UI — the harness reads the REAL Python store
 * row and registers it here, and "the target's own consent UI granted
 * consent and minted the token" is exactly what that models):
 * <ul>
 *   <li>{@code register <session_id> <token_hash> <scope_json>
 *       <consent_expires_at>} — register a session.</li>
 *   <li>{@code announce <api_url> <port> <fingerprint> <host>}
 *       — POST an endpoint announcement to the real Python API.</li>
 *   <li>{@code stop} — stop the server.</li>
 * </ul>
 *
 * <p>Input sink records executed actions into {@code ACTIONS_FILE} for
 * the harness to assert on; kill halts further execution (causal stop).
 * Screen capture synthesizes a real PNG (javax.imageio) clipped to the
 * session's scope bounds, flagged {@code synthesized:true} — real PNG
 * bytes, never fake.
 */
public final class BenchTarget {

    private BenchTarget() {}

    /** Recording sink: executes by logging; halt() stops all execution. */
    static final class RecordingSink implements TargetServer.InputSink {
        private final List<Map<String, Object>> executed =
                new CopyOnWriteArrayList<>();
        private volatile boolean halted = false;

        @Override
        public Map<String, Object> execute(Map<String, Object> action,
                                          TargetServer.LivenessCheck live)
                throws Exception {
            live.check();
            if (halted) {
                throw new TargetServer.RefusalException(
                        "session killed/expired during action: interrupted");
            }
            Map<String, Object> rec = new LinkedHashMap<>(action);
            executed.add(rec);
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("executed", true);
            r.put("action", action.get("type"));
            return r;
        }

        @Override
        public void halt() {
            halted = true;
        }

        /**
         * A newly registered (consented) session is new remote work: the
         * previous kill's halt applied to the previous session's work,
         * not to this one. Production (Android) has the same scoping --
         * the injection pipeline serves the live session.
         */
        public void resetHalt() {
            halted = false;
        }

        public List<Map<String, Object>> executed() {
            return new ArrayList<>(executed);
        }
    }

    /** Synthetic PNG capture, clipped to the session's scope bounds. */
    static final class SyntheticCapture implements TargetServer.ScreenCapture {
        @Override
        public Map<String, Object> capture(Map<String, Object> clip,
                                          Integer maxDim)
                throws TargetServer.RefusalException,
                       TargetServer.CaptureException {
            long w = num(clip.get("w"), 640);
            long h = num(clip.get("h"), 480);
            if (maxDim != null && maxDim > 0) {
                long m = Math.max(w, h);
                if (m > maxDim) {
                    w = w * maxDim / m;
                    h = h * maxDim / m;
                }
            }
            w = Math.max(1, Math.min(w, 2048));
            h = Math.max(1, Math.min(h, 2048));
            BufferedImage img = new BufferedImage(
                    (int) w, (int) h, BufferedImage.TYPE_INT_RGB);
            Graphics2D g = img.createGraphics();
            g.setColor(Color.DARK_GRAY);
            g.fillRect(0, 0, (int) w, (int) h);
            g.setColor(Color.WHITE);
            g.drawString("synthetic frame " + w + "x" + h, 20, 40);
            g.dispose();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            try {
                ImageIO.write(img, "png", bos);
            } catch (java.io.IOException e) {
                throw new TargetServer.CaptureException(
                        "png encode failed: " + e.getMessage());
            }
            String b64 = Base64.getEncoder().encodeToString(bos.toByteArray());
            Map<String, Object> frame = new LinkedHashMap<>();
            frame.put("width", w);
            frame.put("height", h);
            frame.put("format", "png");
            frame.put("data_b64", b64);
            frame.put("ts", (double) (System.currentTimeMillis() / 1000L));
            frame.put("synthesized", true);
            return frame;
        }

        @Override
        public void close() {}

        private static long num(Object v, long dflt) {
            return v instanceof Number ? ((Number) v).longValue() : dflt;
        }
    }

    /**
     * Bench-only URLStreamHandler that captures the exact request bytes
     * AnnounceClient would emit, without touching the network (this
     * sandbox denies the JVM outbound TCP). The production path uses a
     * plain URL; the request construction is identical.
     */
    static final class CapturingHandler
            extends java.net.URLStreamHandler {
        String method = null;
        final Map<String, String> headers = new LinkedHashMap<>();
        byte[] body = new byte[0];

        @Override
        protected java.net.URLConnection openConnection(java.net.URL u) {
            return new CapturingConnection(u, this);
        }

        @Override
        protected java.net.URLConnection openConnection(
                java.net.URL u, java.net.Proxy p) {
            // URL.openConnection(Proxy) calls this two-arg form; the
            // default throws UnsupportedOperationException instead of
            // delegating, so route it to the capturing connection.
            return openConnection(u);
        }
    }

    static final class CapturingConnection
            extends java.net.HttpURLConnection {
        private final CapturingHandler cap;
        private final ByteArrayOutputStream buf =
                new ByteArrayOutputStream();

        CapturingConnection(java.net.URL u, CapturingHandler cap) {
            super(u);
            this.cap = cap;
        }

        @Override public void connect() {}
        @Override public void disconnect() {}
        @Override public boolean usingProxy() { return false; }

        @Override
        public void setRequestMethod(String m)
                throws java.net.ProtocolException {
            cap.method = m;
        }

        @Override
        public void setRequestProperty(String k, String v) {
            cap.headers.put(k, v);
        }

        @Override
        public java.io.OutputStream getOutputStream() {
            return buf;
        }

        @Override
        public int getResponseCode() {
            cap.body = buf.toByteArray();
            return 200;
        }

        @Override
        public java.io.InputStream getInputStream() {
            cap.body = buf.toByteArray();
            return new java.io.ByteArrayInputStream(
                    "{\"ok\":true,\"captured\":true}".getBytes(
                            StandardCharsets.UTF_8));
        }
    }

    static final class BenchIndicator implements TargetServer.SessionIndicator {        private volatile String liveId = null;

        @Override
        public void show(String sessionId) {
            liveId = sessionId;
            System.out.println("INDICATOR_ON " + sessionId);
            System.out.flush();
        }

        @Override
        public void hide(String sessionId) {
            if (sessionId == null || sessionId.equals(liveId)) {
                System.out.println("INDICATOR_OFF "
                        + (sessionId == null ? "" : sessionId));
                System.out.flush();
                liveId = null;
            }
        }

        @Override
        public boolean live() {
            return liveId != null;
        }
    }

    public static void main(String[] argv) throws Exception {
        String deviceId = System.getenv("RD_DEVICE_ID");
        String agentId = System.getenv("RD_AGENT_ID");
        String agentToken = System.getenv("RD_AGENT_TOKEN");
        String ksPath = System.getenv("RD_KEYSTORE");
        String ksPass = System.getenv("RD_KEYSTORE_PASS");
        String portStr = System.getenv("RD_PORT");
        boolean forgeProof = "1".equals(System.getenv("RD_FORGE_PROOF"));
        if (deviceId == null || agentId == null || agentToken == null
                || ksPath == null || ksPass == null) {
            System.err.println("missing RD_* env");
            System.exit(2);
        }
        int port = portStr == null ? 0 : Integer.parseInt(portStr);
        Path keystore = Paths.get(ksPath);
        SessionRegistry registry = new SessionRegistry();
        RecordingSink sink = new RecordingSink();
        SyntheticCapture cap = new SyntheticCapture();
        BenchIndicator indicator = new BenchIndicator();
        TargetServer server = new TargetServer(
                deviceId, agentId, agentToken, registry, sink, cap,
                indicator, keystore, ksPass.toCharArray(), "0.0.0.0", port,
                forgeProof);
        server.start();
        System.out.println("READY port=" + server.boundPort()
                + " host=" + server.boundHost());
        System.out.println("FINGERPRINT "
                + TlsUtil.keystoreFingerprint(
                        TlsUtil.loadKeyStore(keystore,
                                             ksPass.toCharArray())));
        System.out.flush();
        BufferedReader br = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8));
        String line;
        while ((line = br.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            String[] head = line.split(" ", 5);
            switch (head[0]) {
                case "register": {
                    // register <session_id> <token_hash> <scope_json>
                    //          <consent_expires_at>
                    String[] parts = line.split(" ", 5);
                    String sid = parts[1];
                    String th = parts[2];
                    String scopeJson = parts[3];
                    double exp = Double.parseDouble(parts[4]);
                    sink.resetHalt();
                    registry.registerSession(sid, th,
                            Json.parseObject(scopeJson), "consented", exp);
                    System.out.println("REGISTERED " + sid);
                    System.out.flush();
                    break;
                }
                case "announce": {
                    String[] parts = line.split(" ", 6);
                    String apiUrl = parts[1];
                    int aport = Integer.parseInt(parts[2]);
                    String fp = parts[3];
                    String host = parts[4];
                    boolean capture = parts.length > 5
                            && "capture".equals(parts[5]);
                    if (capture) {
                        // Bench-only: this sandbox denies the JVM
                        // outbound TCP, so capture the exact request
                        // bytes the client would emit; the harness
                        // delivers them to the real API.
                        CapturingHandler ch = new CapturingHandler();
                        java.net.URL url = new java.net.URL(
                                null, apiUrl, ch);
                        Map<String, Object> r = AnnounceClient.announce(
                                url, deviceId, agentToken, host, aport,
                                fp);
                        Map<String, Object> capOut = new LinkedHashMap<>();
                        capOut.put("method", ch.method);
                        capOut.put("path", url.getPath());
                        capOut.put("content_type",
                                ch.headers.get("Content-Type"));
                        capOut.put("body", new String(ch.body,
                                StandardCharsets.UTF_8));
                        capOut.put("client_result", r);
                        System.out.println("ANNOUNCE_CAPTURE "
                                + Json.write(capOut));
                    } else {
                        Map<String, Object> r = AnnounceClient.announce(
                                apiUrl, deviceId, agentToken, host, aport,
                                fp);
                        System.out.println("ANNOUNCE "
                                + Json.write(r));
                    }
                    System.out.flush();
                    break;
                }
                case "dump": {
                    // dump the recorded executed actions for the harness
                    System.out.println("EXECUTED " + Json.write(
                            new ArrayList<Object>(sink.executed())));
                    System.out.flush();
                    break;
                }
                case "stop":
                    server.stop();
                    System.out.println("STOPPED");
                    System.out.flush();
                    System.exit(0);
                    return;
                default:
                    System.out.println("UNKNOWN " + head[0]);
                    System.out.flush();
            }
        }
    }
}
