package com.remor.dispatchtarget.proto;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Target-side session authority: the pure-Java mirror of the target-side
 * view of {@code runtime/remote_dispatch/session_model.py}.
 *
 * <p>All refusal reasons are byte-identical to the Python
 * implementation's, because the controller surfaces them to the user.
 * The registry is the enforcement point: it does not trust the
 * controller.
 */
public final class SessionRegistry {

    // -- refusal types -------------------------------------------------

    /** A hello that must be refused (sent back as an error frame). */
    public static final class HelloRefused extends Exception {
        public HelloRefused(String reason) { super(reason); }
    }

    /** A per-message refusal (sent back as action_refused). */
    public static final class TargetRefusal extends Exception {
        public TargetRefusal(String reason) { super(reason); }
    }

    // -- scope ---------------------------------------------------------

    /** Mirrors session_model.Scope and its allows() rule exactly. */
    public static final class Scope {
        public final List<String> actions;
        public final long xMin, yMin, xMax, yMax;
        public final List<String> apps; // null = no allowlist
        public final long maxActions;
        public final double ttlS;
        public final boolean screenShare;
        public final boolean recordFrames;

        private static final Set<String> ACTION_TYPES = new HashSet<>();
        static {
            ACTION_TYPES.add("move");
            ACTION_TYPES.add("click");
            ACTION_TYPES.add("scroll");
            ACTION_TYPES.add("type");
            ACTION_TYPES.add("key");
            ACTION_TYPES.add("launch_app");
        }

        public Scope(List<String> actions, long xMin, long yMin,
                     long xMax, long yMax, List<String> apps,
                     long maxActions, double ttlS, boolean screenShare,
                     boolean recordFrames) {
            this.actions = actions;
            this.xMin = xMin; this.yMin = yMin;
            this.xMax = xMax; this.yMax = yMax;
            this.apps = apps;
            this.maxActions = maxActions;
            this.ttlS = ttlS;
            this.screenShare = screenShare;
            this.recordFrames = recordFrames;
        }

        @SuppressWarnings("unchecked")
        public static Scope fromDict(Map<String, Object> d) {
            List<String> actions = new ArrayList<>();
            for (Object a : (List<Object>) d.get("actions")) {
                actions.add((String) a);
            }
            List<String> apps = null;
            if (d.get("apps") != null) {
                apps = new ArrayList<>();
                for (Object a : (List<Object>) d.get("apps")) {
                    apps.add((String) a);
                }
            }
            return new Scope(actions,
                    num(d.get("x_min"), 0), num(d.get("y_min"), 0),
                    num(d.get("x_max"), 10_000), num(d.get("y_max"), 10_000),
                    apps, num(d.get("max_actions"), 200),
                    dbl(d.get("ttl_s"), 600.0),
                    bool(d.get("screen_share"), false),
                    bool(d.get("record_frames"), false));
        }

        private static long num(Object v, long dflt) {
            return v instanceof Number ? ((Number) v).longValue() : dflt;
        }

        private static double dbl(Object v, double dflt) {
            return v instanceof Number ? ((Number) v).doubleValue() : dflt;
        }

        private static boolean bool(Object v, boolean dflt) {
            return v instanceof Boolean ? (Boolean) v : dflt;
        }

        /**
         * Returns null when the action is allowed, else the exact
         * refusal reason (mirrors Scope.allows).
         */
        public String allows(Map<String, Object> action) {
            Object kindObj = action.get("type");
            String kind = kindObj instanceof String ? (String) kindObj : null;
            if (kind == null || !ACTION_TYPES.contains(kind)) {
                // Python renders f"{kind!r}" with kind=None as "None".
                return "unknown action type "
                        + (kind == null ? "None" : "'" + kind + "'");
            }
            if (!actions.contains(kind)) {
                return "action '" + kind + "' not in session scope";
            }
            if (kind.equals("move") || kind.equals("click")) {
                Object x = action.get("x"), y = action.get("y");
                if (!(x instanceof Number) || !(y instanceof Number)) {
                    return "move/click require numeric x,y";
                }
                double xd = ((Number) x).doubleValue();
                double yd = ((Number) y).doubleValue();
                if (!(xMin <= xd && xd <= xMax && yMin <= yd && yd <= yMax)) {
                    return "coordinates (" + fmtNum(x) + "," + fmtNum(y)
                            + ") outside session bounds ["
                            + xMin + "," + xMax + "]x["
                            + yMin + "," + yMax + "]";
                }
            }
            if (kind.equals("type")) {
                Object text = action.get("text");
                if (!(text instanceof String)
                        || ((String) text).length() > 2000) {
                    return "type: text must be str <= 2000 chars";
                }
            }
            if (kind.equals("launch_app")) {
                Object app = action.get("app");
                String appName = app instanceof String ? (String) app : null;
                // Python renders f"{app!r}" with app=None as "None".
                String shown = appName == null ? "None" : "'" + appName + "'";
                if (apps == null || appName == null
                        || !apps.contains(appName)) {
                    return "app " + shown
                            + " not in session app allowlist";
                }
            }
            return null;
        }

        /** Python f-string number rendering: 100 -> "100", 100.0 -> "100.0". */
        private static String fmtNum(Object v) {
            if (v instanceof Long || v instanceof Integer) {
                return Long.toString(((Number) v).longValue());
            }
            return Double.toString(((Number) v).doubleValue());
        }
    }

    // -- session state -------------------------------------------------

    private static final class Session {
        final String sessionId;
        final String tokenHash; // sha256 hex of the session token
        final Scope scope;
        String state;           // pending/consented/live/killed/ended/expired
        long seqNext = 2;       // hello is seq 1; first action is seq 2
        final Set<String> nonces = new HashSet<>();
        final double consentExpiresAt; // epoch seconds; 0 = no consent
        long actionsUsed = 0;

        Session(String sessionId, String tokenHash, Scope scope,
                String state, double consentExpiresAt) {
            this.sessionId = sessionId;
            this.tokenHash = tokenHash;
            this.scope = scope;
            this.state = state;
            this.consentExpiresAt = consentExpiresAt;
        }
    }

    private final Map<String, Session> sessions = new HashMap<>();

    private static boolean isTerminal(String state) {
        return "killed".equals(state) || "ended".equals(state)
                || "expired".equals(state);
    }

    private static double nowS() {
        return System.currentTimeMillis() / 1000.0;
    }

    /** Register a session the target's own consent flow granted. */
    public synchronized void registerSession(String sessionId,
                                             String tokenHash,
                                             Map<String, Object> scopeDict,
                                             String state,
                                             double consentExpiresAt) {
        sessions.put(sessionId, new Session(sessionId, tokenHash,
                Scope.fromDict(scopeDict), state, consentExpiresAt));
    }

    public synchronized String stateOf(String sessionId) {
        Session s = sessions.get(sessionId);
        return s == null ? "gone" : s.state;
    }

    public synchronized Scope scopeOf(String sessionId) {
        Session s = sessions.get(sessionId);
        return s == null ? null : s.scope;
    }

    public synchronized long seqNext(String sessionId) {
        Session s = sessions.get(sessionId);
        return s == null ? 2 : s.seqNext;
    }

    private static String sha256Hex(String token) throws Exception {
        MessageDigest sha = MessageDigest.getInstance("SHA-256");
        byte[] d = sha.digest(token.getBytes(StandardCharsets.UTF_8));
        StringBuilder sb = new StringBuilder(64);
        for (byte b : d) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    private static boolean constantTimeEquals(String a, String b) {
        byte[] ab = a.getBytes(StandardCharsets.UTF_8);
        byte[] bb = b.getBytes(StandardCharsets.UTF_8);
        return MessageDigest.isEqual(ab, bb);
    }

    /**
     * Verify a hello's session token: session live-able + token matches
     * + consent live. Mirrors RemoteDispatchStore.check_session_token,
     * wrapped as the target does ("hello refused: ...").
     */
    public synchronized void verifyHello(String sessionId,
                                         String sessionToken)
            throws HelloRefused {
        Session s = sessions.get(sessionId);
        if (s == null) {
            throw new HelloRefused(
                    "hello refused: unknown session '" + sessionId + "'");
        }
        if (isTerminal(s.state)) {
            throw new HelloRefused(
                    "hello refused: session " + s.state + ": refused");
        }
        String got;
        try {
            got = sha256Hex(sessionToken == null ? "" : sessionToken);
        } catch (Exception e) {
            throw new HelloRefused("hello refused: invalid session token");
        }
        if (s.tokenHash == null || !constantTimeEquals(s.tokenHash, got)) {
            throw new HelloRefused("hello refused: invalid session token");
        }
        if (!(s.consentExpiresAt > nowS())) {
            throw new HelloRefused(
                    "hello refused: consent not live: refused");
        }
    }

    /**
     * Per-message enforcement: session live, consent live, exact seq +
     * unseen nonce. The seq is consumed and the nonce recorded BEFORE
     * any application check, so a refusal cannot desync the channel
     * (mirrors RemoteDispatchTarget._enforce_channel).
     */
    public synchronized void enforceMessage(String sessionId, long seq,
                                            String nonce, String what)
            throws TargetRefusal {
        Session s = sessions.get(sessionId);
        if (s == null) {
            throw new TargetRefusal(
                    "session gone: " + what + " refused");
        }
        if (!"live".equals(s.state)) {
            throw new TargetRefusal(
                    "session " + s.state + ": " + what + " refused");
        }
        if (!(s.consentExpiresAt > nowS())) {
            throw new TargetRefusal(
                    "consent no longer live: " + what + " refused");
        }
        if (seq != s.seqNext) {
            throw new TargetRefusal(
                    "seq mismatch: got " + seq + ", expected " + s.seqNext
                    + " (replay or reorder refused)");
        }
        if (nonce == null || !s.nonces.add(nonce)) {
            throw new TargetRefusal("duplicate nonce: replay refused");
        }
        s.seqNext++;
    }

    public synchronized void markLive(String sessionId) {
        Session s = sessions.get(sessionId);
        if (s != null && "consented".equals(s.state)) {
            s.state = "live";
        }
    }

    public synchronized boolean consentLive(String sessionId) {
        Session s = sessions.get(sessionId);
        return s != null && s.consentExpiresAt > nowS();
    }

    /** Kill: terminal from any non-terminal state (mirrors kill_session).
     *  Returns null when the session is unknown (the Python store
     *  raises SessionError, which the channel turns into an error
     *  frame -- never a kill_ok for a session that does not exist). */
    public synchronized String killSession(String sessionId) {
        Session s = sessions.get(sessionId);
        if (s == null) return null;
        if (!isTerminal(s.state)) {
            s.state = "killed";
        }
        return s.state;
    }

    /** Returns false when the session is unknown. */
    public synchronized boolean endSession(String sessionId) {
        Session s = sessions.get(sessionId);
        if (s == null) return false;
        if (!isTerminal(s.state)) {
            s.state = "ended";
        }
        return true;
    }

    public synchronized void chargeBudget(String sessionId, int n) {
        Session s = sessions.get(sessionId);
        if (s != null) s.actionsUsed += n;
    }

    public synchronized long actionsUsed(String sessionId) {
        Session s = sessions.get(sessionId);
        return s == null ? 0 : s.actionsUsed;
    }
}
