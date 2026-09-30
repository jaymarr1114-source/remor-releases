package com.remor.dispatchtarget.proto;

import java.io.EOFException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLServerSocketFactory;
import javax.net.ssl.SSLContext;
import java.security.KeyStore;

/**
 * Pure-Java target-side channel server: the target side of the canonical
 * wire protocol, mirroring {@code runtime/remote_dispatch/channel.py}'s
 * {@code TargetListener} and {@code target_agent.py}'s channel handlers.
 *
 * <p>Zero android.* imports: input execution, screen capture, and the
 * live-session indicator are substrate interfaces the Android glue (or
 * the bench) provides.
 *
 * <p>Semantics mirror the Python implementation exactly; where the bench
 * cannot decide a semantic, the Python code is authoritative:
 * <ul>
 *   <li>TLS before any protocol byte; a failed handshake drops the
 *       connection unanswered (no plaintext served).</li>
 *   <li>First message must be hello; hello verifies the session token
 *       (session live-able + consent live) and returns hello_ok with
 *       {@code {bound:true, identity_proof:{device_id,agent_id,
 *       agent_token}, next_seq:N}} at seq 0, or an error frame.</li>
 *   <li>One live connection per session; at most one live
 *       remote-control session per target; a kill-intent hello may
 *       preempt only its own session's connection and never drives the
 *       cursor.</li>
 *   <li>Every later message must carry the bound session_id; per-message
 *       enforcement is session-live + consent-live + exact seq + unseen
 *       nonce, with the seq consumed before application checks.</li>
 *   <li>kill is a fail-closed relay: it stops the remote work causally
 *       (U-9) — injection halted, session torn down, indicator cleared —
 *       never merely a dropped connection.</li>
 * </ul>
 */
public final class TargetServer {

    // -- substrate interfaces ------------------------------------------

    /** Executes one action; returns the substrate's result map. */
    public interface InputSink {
        Map<String, Object> execute(Map<String, Object> action,
                                   LivenessCheck live)
                throws RefusalException, Exception;
        /** Causal stop: no further executions after this returns. */
        void halt();
    }

    /** Called between actions of one batch; throws when the session died. */
    public interface LivenessCheck {
        void check() throws SessionRegistry.TargetRefusal;
    }

    public static final class RefusalException extends Exception {
        public RefusalException(String reason) { super(reason); }
    }

    public static final class CaptureException extends Exception {
        public CaptureException(String reason) { super(reason); }
    }

    /** Screen capture: returns a frame map {width,height,format:"png",
     *  data_b64,ts,synthesized}, clipped to the given rect first. */
    public interface ScreenCapture {
        Map<String, Object> capture(Map<String, Object> clip,
                                    Integer maxDim)
                throws RefusalException, CaptureException;
        void close();
    }

    /** The on-screen LIVE indicator. */
    public interface SessionIndicator {
        void show(String sessionId);
        void hide(String sessionId);
        boolean live();
    }

    // -- server --------------------------------------------------------

    private final String deviceId;
    private final String agentId;
    private final String agentToken;
    private final SessionRegistry registry;
    private final InputSink inputSink;
    private final ScreenCapture capture; // may be null: capture unavailable
    private final SessionIndicator indicator;
    private final boolean forgeProof; // adversarial: wrong token in proof

    private final SSLServerSocket serverSocket;
    private final Thread acceptThread;
    private volatile boolean stopped = false;

    /** One live connection per session (released on disconnect). */
    private final Map<String, Boolean> active = new HashMap<>();

    private ScreenCapture liveCapture; // lazily created, like the Python target

    public TargetServer(String deviceId, String agentId, String agentToken,
                        SessionRegistry registry, InputSink inputSink,
                        ScreenCapture capture, SessionIndicator indicator,
                        Path keystorePath, char[] keystorePassword,
                        String bindHost, int bindPort) throws Exception {
        this(deviceId, agentId, agentToken, registry, inputSink, capture,
             indicator, keystorePath, keystorePassword, bindHost, bindPort,
             false);
    }

    public TargetServer(String deviceId, String agentId, String agentToken,
                        SessionRegistry registry, InputSink inputSink,
                        ScreenCapture capture, SessionIndicator indicator,
                        Path keystorePath, char[] keystorePassword,
                        String bindHost, int bindPort, boolean forgeProof)
            throws Exception {
        this(deviceId, agentId, agentToken, registry, inputSink, capture,
             indicator,
             TlsUtil.loadKeyStore(keystorePath, keystorePassword),
             keystorePassword, bindHost, bindPort, forgeProof);
    }

    /**
     * KeyStore overload: for Android, where the TLS identity lives in
     * the AndroidKeyStore and the private key is non-exportable (it
     * cannot be written to a PKCS12 file). The caller builds an
     * in-memory KeyStore holding the PrivateKeyEntry.
     */
    public TargetServer(String deviceId, String agentId, String agentToken,
                        SessionRegistry registry, InputSink inputSink,
                        ScreenCapture capture, SessionIndicator indicator,
                        KeyStore keyStore, char[] keyPassword,
                        String bindHost, int bindPort, boolean forgeProof)
            throws Exception {
        this.deviceId = deviceId;
        this.agentId = agentId;
        this.agentToken = agentToken;
        this.registry = registry;
        this.inputSink = inputSink;
        this.capture = capture;
        this.indicator = indicator;
        this.forgeProof = forgeProof;
        SSLContext ctx = TlsUtil.serverContext(keyStore, keyPassword);
        this.serverSocket = TlsUtil.serverSocket(ctx, bindHost, bindPort);
        this.serverSocket.setSoTimeout(500);
        this.acceptThread = new Thread(this::acceptLoop, "rd-target-accept");
        this.acceptThread.setDaemon(true);
    }

    public void start() {
        acceptThread.start();
    }

    public int boundPort() {
        return serverSocket.getLocalPort();
    }

    public String boundHost() {
        return serverSocket.getInetAddress().getHostAddress();
    }

    /** Idempotent stop: releases the port (the TargetListener.stop lesson). */
    public synchronized void stop() {
        stopped = true;
        try { serverSocket.close(); } catch (Exception ignored) {}
        closeCapture();
        try { indicator.hide(null); } catch (Exception ignored) {}
    }

    private void acceptLoop() {
        while (!stopped) {
            try {
                Socket conn = serverSocket.accept();
                Thread t = new Thread(() -> handle(conn),
                                      "rd-target-conn");
                t.setDaemon(true);
                t.start();
            } catch (java.net.SocketTimeoutException e) {
                // poll the stop flag
            } catch (Exception e) {
                if (stopped) return;
            }
        }
    }

    // -- connection handling -------------------------------------------

    private static final class ChannelException extends Exception {
        ChannelException(String reason) { super(reason); }
    }

    private void sendError(OutputStream out, String sessionId, String reason) {
        try {
            String r = reason.length() > 300 ? reason.substring(0, 300)
                                             : reason;
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("reason", r);
            FrameCodec.send(out, "error",
                    sessionId == null ? "" : sessionId, 0, body);
        } catch (Exception ignored) {}
    }

    private void handle(Socket conn) {
        String sessionId = null;
        // Not try-with-resources: the catch blocks must still be able
        // to send the error frame on the open socket. (Try-with-
        // resources would close the socket before the catch runs,
        // turning every hello refusal into a dropped connection.)
        try {
            InputStream in = conn.getInputStream();
            OutputStream out = conn.getOutputStream();
            try {
                Message hello;
                try {
                    hello = FrameCodec.decode(in);
                } catch (EOFException e) {
                    // Plaintext socket or bare connect to the TLS port:
                    // the handshake failed before any protocol byte --
                    // drop unanswered, exactly like the Python listener.
                    return;
                }
                if (!"hello".equals(hello.kind)) {
                    throw new ChannelException("first message must be hello");
                }
                HelloResult hr = onHello(hello);
                sessionId = hr.sessionId;
                Map<String, Object> proof = new LinkedHashMap<>();
                proof.put("device_id", deviceId);
                proof.put("agent_id", agentId);
                proof.put("agent_token",
                        forgeProof ? "forged-token" : agentToken);
                Map<String, Object> helloBody = new LinkedHashMap<>();
                helloBody.put("bound", true);
                helloBody.put("identity_proof", proof);
                helloBody.put("next_seq", hr.nextSeq);
                FrameCodec.send(out, "hello_ok", sessionId, 0, helloBody);
                while (true) {
                    Message m = FrameCodec.decode(in);
                    if (!sessionId.equals(m.sessionId)) {
                        throw new ChannelException(
                                "session_id mismatch: dropping");
                    }
                    if ("bye".equals(m.kind)) {
                        FrameCodec.send(out, "bye_ok", sessionId, 0,
                                new LinkedHashMap<>());
                        return;
                    }
                    Map<String, Object> reply = dispatch(m);
                    FrameCodec.send(out, (String) reply.get("kind"),
                            sessionId,
                            ((Number) reply.get("seq")).longValue(),
                            (Map<String, Object>) reply.get("body"));
                }
            } catch (ChannelException e) {
                try {
                    sendError(out, sessionId, e.getMessage());
                } catch (Exception ignored) {}
            } catch (Exception e) {
                try {
                    sendError(out, sessionId, e.getMessage());
                } catch (Exception ignored) {}
            }
        } catch (Exception outer) {
            // Could not even open the streams; nothing to answer on.
        } finally {
            if (sessionId != null) releaseSlot(sessionId);
            try { conn.close(); } catch (Exception ignored) {}
        }
    }

    private static final class HelloResult {
        final String sessionId;
        final long nextSeq;
        HelloResult(String sessionId, long nextSeq) {
            this.sessionId = sessionId;
            this.nextSeq = nextSeq;
        }
    }

    private HelloResult onHello(Message hello)
            throws SessionRegistry.HelloRefused {
        Map<String, Object> body = hello.body;
        String sessionId = hello.sessionId == null ? "" : hello.sessionId;
        Object tok = body.get("session_token");
        String sessionToken = tok instanceof String ? (String) tok : "";
        boolean killIntent = "kill".equals(body.get("intent"));
        registry.verifyHello(sessionId, sessionToken);
        synchronized (active) {
            if (active.containsKey(sessionId) && !killIntent) {
                throw new SessionRegistry.HelloRefused(
                        "hello refused: session already has a live"
                        + " connection (concurrent duplicate refused)");
            }
            for (String other : new ArrayList<>(active.keySet())) {
                if (other.equals(sessionId)) continue;
                String st = registry.stateOf(other);
                if ("live".equals(st) || "consented".equals(st)) {
                    throw new SessionRegistry.HelloRefused(
                            "hello refused: target busy with another live"
                            + " session ("
                            + other.substring(0,
                                    Math.min(13, other.length()))
                            + "...): one live remote-control session"
                            + " per target");
                }
                active.remove(other);
            }
            if (!killIntent) active.put(sessionId, Boolean.TRUE);
        }
        // A kill-only connection neither marks the session live nor shows
        // the indicator: it exists solely to deliver the kill.
        if (!killIntent) {
            registry.markLive(sessionId);
            try { indicator.show(sessionId); } catch (Exception ignored) {}
        }
        return new HelloResult(sessionId, registry.seqNext(sessionId));
    }

    private void releaseSlot(String sessionId) {
        synchronized (active) {
            active.remove(sessionId);
        }
    }

    // -- message dispatch ----------------------------------------------

    @SuppressWarnings("unchecked")
    private Map<String, Object> dispatch(Message m)
            throws ChannelException {
        String kind = m.kind;
        String sessionId = m.sessionId;
        Map<String, Object> body = m.body;
        try {
            if ("kill".equals(kind)) {
                // Fail-closed relay convenience: killing only ever stops.
                String state = registry.killSession(sessionId);
                if (state == null) {
                    throw new ChannelException(
                            "unknown session '" + sessionId + "'");
                }
                inputSink.halt();
                closeCapture();
                try { indicator.hide(sessionId); }
                catch (Exception ignored) {}
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("state", state);
                b.put("processes_terminated", 0L);
                return reply("kill_ok", 0, b);
            }
            if ("end".equals(kind)) {
                if (!registry.endSession(sessionId)) {
                    throw new ChannelException(
                            "unknown session '" + sessionId + "'");
                }
                closeCapture();
                try { indicator.hide(sessionId); }
                catch (Exception ignored) {}
                Map<String, Object> b = new LinkedHashMap<>();
                b.put("state", registry.stateOf(sessionId));
                return reply("end_ok", 0, b);
            }
            if ("get_frame".equals(kind)) {
                return onGetFrame(m);
            }
            if ("action".equals(kind) || "action_batch".equals(kind)) {
                return onAction(m);
            }
            throw new ChannelException("unknown kind '" + kind + "'");
        } catch (SessionRegistry.TargetRefusal | RefusalException e) {
            // Refusals are explicit action_refused replies (the controller
            // distinguishes refusal from transport error).
            Map<String, Object> b = new LinkedHashMap<>();
            b.put("reason", e.getMessage());
            return reply("action_refused", 0, b);
        }
    }

    private Map<String, Object> reply(String kind, long seq,
                                      Map<String, Object> body) {
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("kind", kind);
        r.put("seq", seq);
        r.put("body", body);
        return r;
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> onAction(Message m)
            throws SessionRegistry.TargetRefusal, RefusalException,
                   ChannelException {
        String sessionId = m.sessionId;
        Map<String, Object> body = m.body;
        Object actionsObj = body.get("actions");
        List<Map<String, Object>> actions = new ArrayList<>();
        if (actionsObj instanceof List) {
            for (Object a : (List<Object>) actionsObj) {
                if (a instanceof Map) actions.add((Map<String, Object>) a);
            }
        } else if (body.get("action") instanceof Map) {
            actions.add((Map<String, Object>) body.get("action"));
        }
        if (actions.isEmpty()) {
            throw new ChannelException("no actions");
        }
        // --- enforcement: every action, every time. The seq is consumed
        // and the nonce recorded BEFORE application checks, so a refusal
        // cannot desync the channel.
        registry.enforceMessage(sessionId, m.seq, m.nonce, "action");
        SessionRegistry.Scope scope = registry.scopeOf(sessionId);
        if (registry.actionsUsed(sessionId) + actions.size()
                > scope.maxActions) {
            throw new SessionRegistry.TargetRefusal(
                    "session action budget exhausted");
        }
        // Validate every action BEFORE executing any: a batch is
        // all-or-nothing on scope.
        for (Map<String, Object> action : actions) {
            String reason = scope.allows(action);
            if (reason != null) {
                throw new SessionRegistry.TargetRefusal(
                        "scope refused action: " + reason);
            }
        }
        List<Object> results = new ArrayList<>();
        for (Map<String, Object> action : actions) {
            // A kill landing between two actions of one batch must stop
            // the later actions.
            assertLive(sessionId);
            try {
                results.add(inputSink.execute(action,
                        () -> assertLive(sessionId)));
            } catch (RefusalException e) {
                throw new SessionRegistry.TargetRefusal(e.getMessage());
            } catch (SessionRegistry.TargetRefusal e) {
                throw e;
            } catch (Exception e) {
                throw new SessionRegistry.TargetRefusal(
                        "input failed: " + e.getMessage());
            }
        }
        registry.chargeBudget(sessionId, actions.size());
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("results", results);
        return reply("action_ok", 0, b);
    }

    private void assertLive(String sessionId)
            throws SessionRegistry.TargetRefusal {
        if (!"live".equals(registry.stateOf(sessionId))
                || !registry.consentLive(sessionId)) {
            throw new SessionRegistry.TargetRefusal(
                    "session killed/expired during action: interrupted");
        }
    }

    private Map<String, Object> onGetFrame(Message m)
            throws SessionRegistry.TargetRefusal, RefusalException,
                   ChannelException {
        String sessionId = m.sessionId;
        registry.enforceMessage(sessionId, m.seq, m.nonce, "frame");
        SessionRegistry.Scope scope = registry.scopeOf(sessionId);
        if (!scope.screenShare) {
            throw new SessionRegistry.TargetRefusal(
                    "screen sharing not in session scope: frame refused");
        }
        ScreenCapture cap = currentCapture();
        Map<String, Object> clip = new LinkedHashMap<>();
        clip.put("x", scope.xMin);
        clip.put("y", scope.yMin);
        clip.put("w", scope.xMax - scope.xMin);
        clip.put("h", scope.yMax - scope.yMin);
        Object md = m.body.get("max_dim");
        Integer maxDim = (md instanceof Number
                && ((Number) md).intValue() > 0)
                ? ((Number) md).intValue() : null;
        Map<String, Object> frame;
        try {
            frame = cap.capture(clip, maxDim);
        } catch (RefusalException e) {
            throw e;
        } catch (CaptureException e) {
            throw new SessionRegistry.TargetRefusal(
                    "screen capture unavailable: " + e.getMessage());
        }
        return reply("frame_ok", 0, frame);
    }

    private synchronized ScreenCapture currentCapture()
            throws SessionRegistry.TargetRefusal {
        if (liveCapture == null) {
            if (capture == null) {
                throw new SessionRegistry.TargetRefusal(
                        "screen capture unavailable: no capture substrate");
            }
            liveCapture = capture;
        }
        return liveCapture;
    }

    private synchronized void closeCapture() {
        if (liveCapture != null) {
            try { liveCapture.close(); } catch (Exception ignored) {}
            liveCapture = null;
        }
    }
}
