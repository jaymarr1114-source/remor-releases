package com.remor.dispatchtarget;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.io.PrintWriter;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Localhost JSON-line server implementing the app side of
 * BridgeProtocol.md.
 *
 * <p>Python -> app requests ({@code id/cdn/params}) get synchronous
 * {@code ok/result|error} replies. App -> Python events
 * ({@code event,...}) are written on a live client connection and the
 * server waits for the {@code event_ack} before returning, so the
 * sender knows whether Python honored the event (e.g. user_kill).
 *
 * <p>Binds 127.0.0.1 only. The bridge grants input injection to
 * whoever connects; on-device that is the Python target agent under
 * the same device owner. Never forward off-device except by the owner
 * (adb forward) for bench testing.
 */
public class BridgeServer {

    private static final long ACK_TIMEOUT_S = 15;

    private final DispatchAccessibilityService service;
    private final int port;
    private ServerSocket serverSocket;
    private final List<ClientHandler> clients = new CopyOnWriteArrayList<>();

    // Single in-flight app->python event (kills are rare; one slot is
    // enough and keeps the ack routing trivially correct).
    private final Object eventLock = new Object();
    private String pendingEvent;
    private CountDownLatch ackLatch;
    private final AtomicReference<JSONObject> ackResult =
            new AtomicReference<>(null);

    public BridgeServer(DispatchAccessibilityService service, int port) {
        this.service = service;
        this.port = port;
    }

    public void start() {
        Thread t = new Thread(this::acceptLoop, "bridge-accept");
        t.setDaemon(true);
        t.start();
    }

    public void stop() {
        try {
            if (serverSocket != null) {
                serverSocket.close();
            }
        } catch (Exception ignored) {
        }
        for (ClientHandler c : clients) {
            c.closeQuietly();
        }
    }

    private void acceptLoop() {
        try {
            serverSocket = new ServerSocket();
            serverSocket.bind(new InetSocketAddress("127.0.0.1", port));
            while (!serverSocket.isClosed()) {
                Socket s = serverSocket.accept();
                ClientHandler h = new ClientHandler(s);
                clients.add(h);
                Thread t = new Thread(h, "bridge-client");
                t.setDaemon(true);
                t.start();
            }
        } catch (Exception ignored) {
            // Closed on stop(); nothing to report.
        }
    }

    /**
     * Send an app->Python event on a live client connection and wait
     * for the event_ack. Returns the ack frame.
     */
    public JSONObject sendEventToPython(JSONObject event) throws Exception {
        synchronized (eventLock) {
            if (pendingEvent != null) {
                throw new Exception("event already in flight");
            }
            pendingEvent = event.optString("event", "");
            ackLatch = new CountDownLatch(1);
            ackResult.set(null);
        }
        boolean sent = false;
        for (ClientHandler c : clients) {
            if (c.sendLine(event.toString())) {
                sent = true;
                break;
            }
        }
        if (!sent) {
            clearPending();
            throw new Exception("no python client connected");
        }
        if (!ackLatch.await(ACK_TIMEOUT_S, TimeUnit.SECONDS)) {
            clearPending();
            throw new Exception("event_ack timeout");
        }
        JSONObject ack = ackResult.get();
        clearPending();
        if (ack == null) {
            throw new Exception("no event_ack received");
        }
        return ack;
    }

    private void clearPending() {
        synchronized (eventLock) {
            pendingEvent = null;
            ackLatch = null;
        }
    }

    void onAck(JSONObject ack) {
        synchronized (eventLock) {
            if (pendingEvent != null
                    && pendingEvent.equals(ack.optString("event_ack", ""))) {
                ackResult.set(ack);
                if (ackLatch != null) {
                    ackLatch.countDown();
                }
            }
        }
    }

    // ------------------------------------------------------------------
    private class ClientHandler implements Runnable {
        private final Socket socket;
        private volatile PrintWriter out;

        ClientHandler(Socket socket) {
            this.socket = socket;
        }

        boolean sendLine(String line) {
            try {
                PrintWriter w = out;
                if (w == null) {
                    return false;
                }
                w.println(line);
                w.flush();
                return !w.checkError();
            } catch (Exception e) {
                return false;
            }
        }

        void closeQuietly() {
            try {
                socket.close();
            } catch (Exception ignored) {
            }
        }

        @Override
        public void run() {
            try (BufferedReader in = new BufferedReader(
                    new InputStreamReader(socket.getInputStream(),
                            StandardCharsets.UTF_8))) {
                out = new PrintWriter(new OutputStreamWriter(
                        socket.getOutputStream(), StandardCharsets.UTF_8),
                        true);
                String line;
                while ((line = in.readLine()) != null) {
                    JSONObject frame = new JSONObject(line);
                    if (frame.has("event_ack")) {
                        onAck(frame);
                        continue;
                    }
                    JSONObject reply = handleRequest(frame);
                    sendLine(reply.toString());
                }
            } catch (Exception ignored) {
            } finally {
                clients.remove(this);
                closeQuietly();
            }
        }

        private JSONObject handleRequest(JSONObject req) {
            JSONObject reply = new JSONObject();
            try {
                reply.put("id", req.getInt("id"));
                String cmd = req.getString("cmd");
                JSONObject params = req.optJSONObject("params");
                if (params == null) {
                    params = new JSONObject();
                }
                JSONObject result = dispatch(cmd, params);
                reply.put("ok", true);
                reply.put("result", result);
            } catch (Exception e) {
                try {
                    reply.put("ok", false);
                    String msg = e.getMessage();
                    reply.put("error",
                            msg != null ? msg : e.toString());
                } catch (Exception ignored) {
                }
            }
            return reply;
        }

        private JSONObject dispatch(String cmd, JSONObject p)
                throws Exception {
            // Fail-closed user kill latch: while the user has pressed KILL
            // and no fresh consented session has rearmed the app, every
            // actuator command is refused here -- independent of whether
            // the user_kill event reached the Python target. Indicator
            // commands intentionally bypass the latch (hide/show must work
            // during and after a kill; indicator_live is read-only).
            if (cmd.equals("tap") || cmd.equals("swipe")
                    || cmd.equals("scroll") || cmd.equals("set_text")
                    || cmd.equals("global_action") || cmd.equals("launch")) {
                service.checkKillLatch();
            }
            switch (cmd) {
                case "ping":
                    return service.ping();
                case "tap":
                    return service.doTap(p.getInt("x"), p.getInt("y"));
                case "swipe":
                    return service.doSwipe(
                            p.getInt("x1"), p.getInt("y1"),
                            p.getInt("x2"), p.getInt("y2"),
                            p.optInt("duration_ms", 300));
                case "scroll":
                    return service.doScroll(
                            p.getInt("x"), p.getInt("y"),
                            p.getInt("dx"), p.getInt("dy"));
                case "set_text":
                    return service.doSetText(p.getString("text"));
                case "global_action":
                    return service.doGlobalAction(p.getString("action"));
                case "launch":
                    return service.doLaunch(p.getString("package"));
                case "indicator_show":
                    service.showIndicator(p.getString("session_id"));
                    return new JSONObject();
                case "indicator_hide":
                    service.hideIndicator(p.optString("session_id", ""));
                    return new JSONObject();
                case "indicator_live":
                    JSONObject r = new JSONObject();
                    r.put("live", service.isIndicatorLive());
                    return r;
                default:
                    throw new Exception("unknown cmd " + cmd);
            }
        }
    }
}
