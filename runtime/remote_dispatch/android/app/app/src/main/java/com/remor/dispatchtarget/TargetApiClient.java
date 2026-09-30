package com.remor.dispatchtarget;

import android.content.Context;
import android.content.SharedPreferences;

import com.remor.dispatchtarget.proto.AnnounceClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * HTTP client for the LAN controller's remote-dispatch API.
 *
 * <p>Endpoints used (see runtime/services/remote_dispatch_api.py):
 * <ul>
 *   <li>POST /api/remote/announce -- via {@link AnnounceClient}
 *   <li>GET  /api/remote/sessions  -- poll for pending sessions
 *   <li>POST /api/remote/sessions/&lt;id&gt;/target-consent -- target-side
 *       consent grant (NAMED BOUNDARY: the controller track has not
 *       implemented this endpoint yet; the consent UI degrades to
 *       manual session entry until it exists).
 * </ul>
 *
 * <p>All requests carry the agent token from pairing. The token is
 * never logged.
 */
public class TargetApiClient {

    private final String baseUrl;
    private final String deviceId;
    private final String agentId;
    private final String agentToken;

    public TargetApiClient(Context ctx) {
        SharedPreferences prefs = ctx.getSharedPreferences(
                RemoteListener.PREFS, Context.MODE_PRIVATE);
        baseUrl = prefs.getString(RemoteListener.PREF_CONTROLLER_URL, "")
                .replaceAll("/+$", "");
        deviceId = prefs.getString(RemoteListener.PREF_DEVICE_ID, "");
        agentId = prefs.getString(RemoteListener.PREF_AGENT_ID, "");
        agentToken = prefs.getString(RemoteListener.PREF_AGENT_TOKEN, "");
    }

    public boolean configured() {
        return !baseUrl.isEmpty() && !deviceId.isEmpty()
                && !agentId.isEmpty() && !agentToken.isEmpty();
    }

    public String deviceId() {
        return deviceId;
    }

    // -- announce ------------------------------------------------------

    /**
     * Announce this target's endpoint to the controller. The listener
     * must be running (the port is read from the live server).
     */
    public JSONObject announce(int port, String fingerprint)
            throws Exception {
        Map<String, Object> out = AnnounceClient.announce(
                baseUrl + "/api/remote/announce", deviceId, agentToken,
                localHost(), port, fingerprint);
        return new JSONObject(out);
    }

    /** Best-effort local LAN address for the announce body. */
    private String localHost() {
        try {
            java.util.Enumeration<java.net.NetworkInterface> ifs =
                    java.net.NetworkInterface.getNetworkInterfaces();
            while (ifs.hasMoreElements()) {
                java.net.NetworkInterface ni = ifs.nextElement();
                if (!ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                java.util.Enumeration<java.net.InetAddress> addrs =
                        ni.getInetAddresses();
                while (addrs.hasMoreElements()) {
                    java.net.InetAddress a = addrs.nextElement();
                    if (a instanceof java.net.Inet4Address
                            && !a.isLoopbackAddress()) {
                        return a.getHostAddress();
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return "0.0.0.0";
    }

    // -- session polling ------------------------------------------------

    /** A pending session awaiting target-side consent. */
    public static final class PendingSession {
        public final String sessionId;
        public final JSONObject scope;
        public final String createdAt;

        PendingSession(String sessionId, JSONObject scope,
                       String createdAt) {
            this.sessionId = sessionId;
            this.scope = scope;
            this.createdAt = createdAt;
        }
    }

    /**
     * List sessions for this device that are awaiting consent.
     * Filters client-side on device_id + state.
     */
    public List<PendingSession> pollPending() throws Exception {
        JSONObject resp = get("/api/remote/sessions");
        List<PendingSession> out = new ArrayList<>();
        if (!resp.optBoolean("ok", false)) {
            return out;
        }
        JSONArray arr = resp.optJSONArray("sessions");
        if (arr == null) {
            return out;
        }
        for (int i = 0; i < arr.length(); i++) {
            JSONObject s = arr.optJSONObject(i);
            if (s == null) {
                continue;
            }
            if (!deviceId.equals(s.optString("device_id"))) {
                continue;
            }
            String state = s.optString("state");
            if (!"requested".equals(state)
                    && !"pending".equals(state)) {
                continue;
            }
            out.add(new PendingSession(
                    s.optString("session_id"),
                    s.optJSONObject("scope") != null
                            ? s.optJSONObject("scope")
                            : new JSONObject(),
                    s.optString("created_at")));
        }
        return out;
    }

    // -- target-side consent (named boundary) ---------------------------

    /**
     * Grant consent for a session from the target side.
     *
     * <p>NAMED BOUNDARY: requires the controller track to add
     * {@code POST /api/remote/sessions/<id>/target-consent}, which
     * authenticates the agent token, marks the session consented,
     * and returns the session token hash + consent expiry the target
     * must pin in its registry. Until that endpoint exists this call
     * throws and ConsentActivity falls back to manual session entry.
     *
     * @return {session_id, token_hash, scope, consent_expires_at}
     */
    public JSONObject grantConsent(String sessionId) throws Exception {
        JSONObject body = new JSONObject();
        body.put("agent_id", agentId);
        body.put("agent_token", agentToken);
        JSONObject resp = post(
                "/api/remote/sessions/" + sessionId + "/target-consent",
                body);
        if (!resp.optBoolean("ok", false)) {
            throw new Exception("consent refused: "
                    + resp.optString("error", "unknown"));
        }
        return resp;
    }

    // -- HTTP plumbing ---------------------------------------------------

    private JSONObject get(String path) throws Exception {
        URL url = new URL(baseUrl + path);
        HttpURLConnection c = (HttpURLConnection) url.openConnection(
                Proxy.NO_PROXY);
        try {
            c.setRequestMethod("GET");
            c.setRequestProperty("X-Agent-Id", agentId);
            c.setRequestProperty("X-Agent-Token", agentToken);
            c.setConnectTimeout(10000);
            c.setReadTimeout(15000);
            return readJson(c);
        } finally {
            c.disconnect();
        }
    }

    private JSONObject post(String path, JSONObject body)
            throws Exception {
        URL url = new URL(baseUrl + path);
        HttpURLConnection c = (HttpURLConnection) url.openConnection(
                Proxy.NO_PROXY);
        try {
            byte[] bytes = body.toString()
                    .getBytes(StandardCharsets.UTF_8);
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("X-Agent-Id", agentId);
            c.setRequestProperty("X-Agent-Token", agentToken);
            c.setConnectTimeout(10000);
            c.setReadTimeout(15000);
            try (OutputStream os = c.getOutputStream()) {
                os.write(bytes);
            }
            return readJson(c);
        } finally {
            c.disconnect();
        }
    }

    private JSONObject readJson(HttpURLConnection c) throws Exception {
        int code = c.getResponseCode();
        InputStream in = code >= 400 ? c.getErrorStream()
                : c.getInputStream();
        if (in == null) {
            throw new Exception("HTTP " + code + " (no body)");
        }
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = in.read(buf)) > 0) {
            bos.write(buf, 0, n);
            if (bos.size() > 1_000_000) {
                throw new Exception("response too large");
            }
        }
        String raw = new String(bos.toByteArray(),
                StandardCharsets.UTF_8);
        if (code == 404) {
            throw new Exception("endpoint not found (HTTP 404): "
                    + "the controller may predate this client");
        }
        if (code >= 400) {
            throw new Exception("HTTP " + code + ": " + raw);
        }
        return new JSONObject(raw);
    }
}
