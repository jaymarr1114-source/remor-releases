package com.remor.dispatchtarget.proto;

import java.io.InputStream;
import java.io.OutputStream;
import java.security.SecureRandom;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * Tablet-side tap-to-pair state machine (RD-EASYPAIR-1): the human-friendly
 * replacement for the manual agent-token shuffle.
 *
 * <p>Pairing frames arrive as the FIRST frame on the target TLS channel
 * (see {@link TargetServer}): {@code pair_request} /
 * {@code pair_confirm} / {@code pair_abort}. The phone's certificate is
 * already pinned at the TLS layer before any frame is read, so a spoofed
 * beacon alone cannot reach this code.
 *
 * <p>Ceremony:
 * <ol>
 *   <li>Phone sends {@code pair_request {phone_name, phone_id, device_id,
 *       agent_id, agent_token, phone_api_url}}. The tablet validates the
 *       fields, mints a 6-digit code, stores a pending pairing (120s
 *       expiry), and fires {@link PairingCallback#onPairRequest} -- the
 *       Android glue shows "&lt;phone&gt; wants to pair. Code: X. Allow?"
 *   <li>Phone shows the code it received: "Does this match your tablet?"
 *   <li>Both humans confirm. Phone sends {@code pair_confirm
 *       {pairing_id}}. If the tablet owner approved, the tablet stores
 *       the phone's credentials + API URL and answers {@code pair_ok
 *       {agent_token}}. Otherwise {@code pair_refused {reason}}.
 * </ol>
 *
 * <p>Security properties:
 * <ul>
 *   <li>Approval happens ONLY via {@link #approvePairing} (the tablet
 *       owner's explicit Allow). The phone can never self-approve.
 *   <li>Every refusal carries its exact reason; unknown/expired/denied/
 *       already-settled pairings all fail closed.
 *   <li>Codes are 6 digits from SecureRandom; pairing ids are random
 *       UUIDs (unguessable); confirm is single-use.
 * </ul>
 *
 * <p>Pure Java: no android.* imports (testable on the JVM).
 */
public final class PairingServer {

    /** Pairing requests expire this long after creation. */
    public static final long PAIRING_TTL_S = 120L;
    private static final int MAX_PENDING = 8;

    private static final Pattern DEVICE_ID_RE =
            Pattern.compile("[A-Za-z0-9_:.\\-]{1,118}");
    private static final Pattern URL_RE =
            Pattern.compile("https?://[^\\s]{1,200}");

    /** Substrate: the Android glue shows consent UI and stores prefs. */
    public interface PairingCallback {
        /** A new pairing request arrived: show the consent screen. */
        void onPairRequest(PendingPairing pairing);
        /**
         * The pairing settled (approved or denied/aborted/expired): persist
         * credentials on approval, clear UI otherwise.
         */
        void onPairSettled(PendingPairing pairing, boolean approved);
    }

    /** One pending pairing request. Immutable except for state. */
    public static final class PendingPairing {
        public final String pairingId;
        public final String code; // 6 digits, e.g. "482916"
        public final String phoneName;
        public final String phoneId;
        public final String deviceId;
        public final String agentId;
        public final String agentToken;
        public final String phoneApiUrl;
        public final long expiresAt; // epoch seconds

        volatile String state; // "pending", "approved", "denied", "settled"

        PendingPairing(String pairingId, String code, String phoneName,
                       String phoneId, String deviceId, String agentId,
                       String agentToken, String phoneApiUrl,
                       long expiresAt) {
            this.pairingId = pairingId;
            this.code = code;
            this.phoneName = phoneName;
            this.phoneId = phoneId;
            this.deviceId = deviceId;
            this.agentId = agentId;
            this.agentToken = agentToken;
            this.phoneApiUrl = phoneApiUrl;
            this.expiresAt = expiresAt;
            this.state = "pending";
        }

        /** Display form: "482 916". */
        public String displayCode() {
            return code.substring(0, 3) + " " + code.substring(3);
        }
    }

    /** Refusal with an exact, user-showable reason. */
    public static final class PairingRefused extends Exception {
        PairingRefused(String reason) {
            super(reason);
        }
    }

    private final PairingCallback callback;
    private final SecureRandom random = new SecureRandom();
    private final Map<String, PendingPairing> pending =
            new LinkedHashMap<>();

    public PairingServer(PairingCallback callback) {
        if (callback == null) {
            throw new IllegalArgumentException("callback required");
        }
        this.callback = callback;
    }

    private synchronized void sweepExpired() {
        long now = System.currentTimeMillis() / 1000L;
        Iterator<Map.Entry<String, PendingPairing>> it =
                pending.entrySet().iterator();
        while (it.hasNext()) {
            PendingPairing p = it.next().getValue();
            if ("pending".equals(p.state) && p.expiresAt <= now) {
                p.state = "denied";
                it.remove();
                callback.onPairSettled(p, false);
            }
        }
    }

    private static String str(Map<String, Object> body, String key,
                              int maxLen) throws PairingRefused {
        Object v = body.get(key);
        if (!(v instanceof String) || ((String) v).trim().isEmpty()
                || ((String) v).length() > maxLen) {
            throw new PairingRefused(
                    "pairing refused: missing or invalid '" + key + "'");
        }
        return ((String) v).trim();
    }

    /**
     * Handle a pair_request body. Returns the pending pairing (the caller
     * answers pair_pending with its id + code).
     */
    public synchronized PendingPairing requestPairing(
            Map<String, Object> body) throws PairingRefused {
        sweepExpired();
        String phoneName = str(body, "phone_name", 120);
        String phoneId = str(body, "phone_id", 120);
        String deviceId = str(body, "device_id", 118);
        if (!DEVICE_ID_RE.matcher(deviceId).matches()) {
            throw new PairingRefused(
                    "pairing refused: invalid device_id");
        }
        String agentId = str(body, "agent_id", 128);
        String agentToken = str(body, "agent_token", 256);
        String phoneApiUrl = str(body, "phone_api_url", 200);
        if (!URL_RE.matcher(phoneApiUrl).matches()) {
            throw new PairingRefused(
                    "pairing refused: invalid phone_api_url");
        }
        if (pending.size() >= MAX_PENDING) {
            throw new PairingRefused(
                    "pairing refused: too many pending requests,"
                            + " try again shortly");
        }
        String pairingId = "pair-" + UUID.randomUUID().toString()
                .replace("-", "").substring(0, 16);
        String code = String.format("%06d",
                random.nextInt(1000000));
        long expiresAt = System.currentTimeMillis() / 1000L + PAIRING_TTL_S;
        PendingPairing p = new PendingPairing(pairingId, code, phoneName,
                phoneId, deviceId, agentId, agentToken, phoneApiUrl,
                expiresAt);
        pending.put(pairingId, p);
        callback.onPairRequest(p);
        return p;
    }

    /** Tablet owner allowed the request (from the consent UI). */
    public synchronized boolean approvePairing(String pairingId) {
        sweepExpired();
        PendingPairing p = pending.get(pairingId);
        if (p == null || !"pending".equals(p.state)) {
            return false;
        }
        p.state = "approved";
        return true;
    }

    /** Tablet owner denied the request (from the consent UI). */
    public synchronized boolean denyPairing(String pairingId) {
        sweepExpired();
        PendingPairing p = pending.get(pairingId);
        if (p == null || !"pending".equals(p.state)) {
            return false;
        }
        p.state = "denied";
        pending.remove(pairingId);
        callback.onPairSettled(p, false);
        return true;
    }

    /**
     * Handle pair_confirm: single-use. Returns the agent token when the
     * tablet owner approved; otherwise throws with the exact reason.
     */
    public synchronized String confirmPairing(String pairingId)
            throws PairingRefused {
        sweepExpired();
        PendingPairing p = pending.get(pairingId);
        if (p == null) {
            throw new PairingRefused(
                    "pairing refused: unknown or expired pairing request");
        }
        if ("denied".equals(p.state)) {
            pending.remove(pairingId);
            throw new PairingRefused(
                    "pairing refused: the tablet owner denied this request");
        }
        if (!"approved".equals(p.state)) {
            throw new PairingRefused(
                    "pairing refused: waiting for approval on the tablet");
        }
        // Approved: single-use settle.
        p.state = "settled";
        pending.remove(pairingId);
        callback.onPairSettled(p, true);
        return p.agentToken;
    }

    /** Phone aborted: drop the pending request. */
    public synchronized void abortPairing(String pairingId) {
        sweepExpired();
        PendingPairing p = pending.remove(pairingId);
        if (p != null && "pending".equals(p.state)) {
            p.state = "denied";
            callback.onPairSettled(p, false);
        }
    }

    /** For tests: current pending count. */
    synchronized int pendingCount() {
        sweepExpired();
        return pending.size();
    }

    /** Look up a pending pairing (for the consent UI). Null if none. */
    public synchronized PendingPairing getPending(String pairingId) {
        sweepExpired();
        PendingPairing p = pending.get(pairingId);
        return (p != null && "pending".equals(p.state)) ? p : null;
    }

    // -- frame-level dispatch (called by TargetServer pre-hello) --------

    /**
     * Handle one pairing frame as the first frame on a fresh TLS
     * connection. Answers exactly one frame and returns; the caller closes
     * the connection afterwards (pairing never opens a control session).
     */
    @SuppressWarnings("unchecked")
    public void handleFirstFrame(String kind, Map<String, Object> body,
                                 OutputStream out) {
        try {
            if ("pair_request".equals(kind)) {
                PendingPairing p = requestPairing(body);
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("pairing_id", p.pairingId);
                b.put("code", p.code);
                FrameCodec.send(out, "pair_pending", "", 0, b);
            } else if ("pair_confirm".equals(kind)) {
                Object id = body.get("pairing_id");
                String token = confirmPairing(
                        id instanceof String ? (String) id : "");
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("agent_token", token);
                FrameCodec.send(out, "pair_ok", "", 0, b);
            } else if ("pair_abort".equals(kind)) {
                Object id = body.get("pairing_id");
                abortPairing(id instanceof String ? (String) id : "");
                FrameCodec.send(out, "pair_aborted", "", 0,
                        new LinkedHashMap<>());
            } else {
                sendRefused(out, "pairing refused: unknown pairing frame '"
                        + kind + "'");
            }
        } catch (PairingRefused r) {
            sendRefused(out, r.getMessage());
        } catch (Exception e) {
            sendRefused(out, "pairing failed: " + e.getMessage());
        }
    }

    private static void sendRefused(OutputStream out, String reason) {
        try {
            String r = reason.length() > 300 ? reason.substring(0, 300)
                                             : reason;
            Map<String, Object> b = new LinkedHashMap<>();
            b.put("reason", r);
            FrameCodec.send(out, "pair_refused", "", 0, b);
        } catch (Exception ignored) {}
    }
}
