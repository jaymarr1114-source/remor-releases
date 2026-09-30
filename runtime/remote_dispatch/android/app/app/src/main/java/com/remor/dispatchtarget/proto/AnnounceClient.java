package com.remor.dispatchtarget.proto;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Target-side announcement client: POSTs the target's endpoint to the
 * controller's announce API, mirroring {@code announce.py}'s
 * {@code announce_endpoint}.
 *
 * <p>Body: {device_id, agent_token, host, port, cert_fingerprint,
 * ann_ts, ann_nonce}. Failure surfaces as the client's refusal-style
 * messages ("announcement transport failed: ..." etc.) exactly like
 * the Python client, so the same strings appear on both sides.
 */
public final class AnnounceClient {
    private AnnounceClient() {}

    public static Map<String, Object> announce(String apiUrl,
                                              String deviceId,
                                              String agentToken,
                                              String host, int port,
                                              String certFingerprint) {
        try {
            // The announce URL is a LAN controller address: it must
            // never be routed through an egress proxy (the body carries
            // the agent token).
            return announce(new URL(apiUrl),
                    deviceId, agentToken, host, port, certFingerprint);
        } catch (Exception e) {
            Map<String, Object> fail = new LinkedHashMap<>();
            fail.put("ok", false);
            fail.put("error", "announcement transport failed: "
                    + e.getMessage());
            return fail;
        }
    }

    /**
     * URL overload: the production path passes a plain URL (opened
     * with Proxy.NO_PROXY, see above); the bench passes a URL with a
     * capturing stream handler because this sandbox denies the JVM
     * outbound TCP. Same request bytes either way.
     */
    public static Map<String, Object> announce(java.net.URL url,
                                              String deviceId,
                                              String agentToken,
                                              String host, int port,
                                              String certFingerprint) {
        long ts = System.currentTimeMillis() / 1000L;
        String nonce = FrameCodec.newNonce();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("device_id", deviceId);
        body.put("agent_token", agentToken);
        body.put("host", host);
        body.put("port", (long) port);
        body.put("cert_fingerprint", certFingerprint);
        body.put("ann_ts", (double) ts);
        body.put("ann_nonce", nonce);
        byte[] raw = Json.write(body).getBytes(StandardCharsets.UTF_8);
        try {
            HttpURLConnection c = (HttpURLConnection)
                    url.openConnection(java.net.Proxy.NO_PROXY);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(5000);
            c.setReadTimeout(5000);
            c.setRequestProperty("Content-Type", "application/json");
            try (OutputStream out = c.getOutputStream()) {
                out.write(raw);
            }
            int status = c.getResponseCode();
            if (status != 200) {
                Map<String, Object> fail = new LinkedHashMap<>();
                fail.put("ok", false);
                fail.put("error", "announcement transport failed: "
                        + "http " + status);
                return fail;
            }
            byte[] resp = c.getInputStream().readAllBytes();
            Map<String, Object> m = Json.parseObject(
                    new String(resp, StandardCharsets.UTF_8));
            return m;
        } catch (Exception e) {
            Map<String, Object> fail = new LinkedHashMap<>();
            fail.put("ok", false);
            fail.put("error", "announcement transport failed: "
                    + e.getMessage());
            return fail;
        }
    }
}
