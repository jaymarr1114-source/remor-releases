package com.remor.dispatchtarget.proto;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * RD-EASYPAIR-1 Java unit tests (pure JVM, no Android).
 * Run: javac + java (see gate_run.sh).
 */
public final class PairingServerTest {

    static int passed = 0;
    static int failed = 0;
    static final List<String> failures = new ArrayList<>();

    static void check(String name, boolean cond, String detail) {
        if (cond) {
            passed++;
            System.out.println("  ok: " + name);
        } else {
            failed++;
            failures.add(name);
            System.out.println("  FAIL: " + name + " " + detail);
        }
    }

    static void check(String name, boolean cond) {
        check(name, cond, "");
    }

    /** Recording callback: no Android needed. */
    static final class Rec implements PairingServer.PairingCallback {
        final List<PairingServer.PendingPairing> requests =
                new ArrayList<>();
        final List<String> settled = new ArrayList<>(); // id:approved?

        @Override
        public void onPairRequest(PairingServer.PendingPairing p) {
            requests.add(p);
        }

        @Override
        public void onPairSettled(PairingServer.PendingPairing p,
                                  boolean approved) {
            settled.add(p.pairingId + ":" + approved);
        }
    }

    static Map<String, Object> reqBody() {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("phone_name", "Test Phone");
        b.put("phone_id", "phone-1");
        b.put("device_id", "tab-1");
        b.put("agent_id", "agent-1");
        b.put("agent_token", "tok-1");
        b.put("phone_api_url", "http://192.168.1.5:8766");
        return b;
    }

    public static void main(String[] args) throws Exception {
        System.out.println("== PairingServer (Java) ==");
        testHappyPath();
        testRefusals();
        testFrameDispatch();

        System.out.println("== DiscoveryBeacon (Java) ==");
        testBeaconBuild();

        System.out.println("\n" + passed + " passed, " + failed
                + " failed");
        if (!failures.isEmpty()) {
            System.out.println("FAILURES:");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            System.exit(1);
        }
        System.out.println("ALL GREEN");
    }

    static void testHappyPath() throws Exception {
        Rec rec = new Rec();
        PairingServer ps = new PairingServer(rec);

        // request -> pending with a 6-digit code, callback fired.
        PairingServer.PendingPairing p = ps.requestPairing(reqBody());
        check("request -> pending pairing",
                p != null && p.pairingId.startsWith("pair-"));
        check("code is 6 digits",
                p.code.matches("[0-9]{6}"), p.code);
        check("onPairRequest fired",
                rec.requests.size() == 1
                        && rec.requests.get(0) == p);
        check("display code formatted",
                p.displayCode().matches("[0-9]{3} [0-9]{3}"),
                p.displayCode());

        // confirm before approval -> refused (waiting for approval).
        try {
            ps.confirmPairing(p.pairingId);
            check("confirm before approval refused", false,
                    "confirm unexpectedly succeeded");
        } catch (PairingServer.PairingRefused e) {
            check("confirm before approval refused",
                    e.getMessage().contains("waiting for approval"),
                    e.getMessage());
        }

        // approve -> confirm -> token, settled callback with approved=true.
        check("approve", ps.approvePairing(p.pairingId));
        String token = ps.confirmPairing(p.pairingId);
        check("approved confirm returns agent token",
                "tok-1".equals(token), token);
        check("onPairSettled(approved=true)",
                rec.settled.contains(p.pairingId + ":true"),
                rec.settled.toString());

        // replayed confirm -> refused (single-use).
        try {
            ps.confirmPairing(p.pairingId);
            check("replayed confirm refused", false);
        } catch (PairingServer.PairingRefused e) {
            check("replayed confirm refused",
                    e.getMessage().contains("unknown or expired"),
                    e.getMessage());
        }
    }

    static void testRefusals() throws Exception {
        Rec rec = new Rec();
        PairingServer ps = new PairingServer(rec);

        // Unknown pairing id.
        try {
            ps.confirmPairing("pair-nope");
            check("unknown pairing refused", false);
        } catch (PairingServer.PairingRefused e) {
            check("unknown pairing refused",
                    e.getMessage().contains("unknown or expired"));
        }

        // Deny then confirm -> refused. (deny() removes the pairing,
        // so confirm sees "unknown or expired" -- this is deliberate:
        // a refused confirm does not leak whether the pairing existed.)
        PairingServer.PendingPairing p = ps.requestPairing(reqBody());
        check("deny", ps.denyPairing(p.pairingId));
        try {
            ps.confirmPairing(p.pairingId);
            check("denied confirm refused", false);
        } catch (PairingServer.PairingRefused e) {
            check("denied confirm refused",
                    e.getMessage().contains("unknown or expired"),
                    e.getMessage());
        }
        check("onPairSettled(approved=false)",
                rec.settled.contains(p.pairingId + ":false"));

        // Invalid request fields -> refused with exact reason.
        Map<String, Object> bad = reqBody();
        bad.remove("agent_token");
        try {
            ps.requestPairing(bad);
            check("missing agent_token refused", false);
        } catch (PairingServer.PairingRefused e) {
            check("missing agent_token refused",
                    e.getMessage().contains("agent_token"),
                    e.getMessage());
        }
        bad = reqBody();
        bad.put("phone_api_url", "not a url");
        try {
            ps.requestPairing(bad);
            check("bad phone_api_url refused", false);
        } catch (PairingServer.PairingRefused e) {
            check("bad phone_api_url refused",
                    e.getMessage().contains("phone_api_url"),
                    e.getMessage());
        }

        // Abort -> gone.
        PairingServer.PendingPairing q = ps.requestPairing(reqBody());
        ps.abortPairing(q.pairingId);
        check("aborted pairing not approvable",
                !ps.approvePairing(q.pairingId));
    }

    static void testFrameDispatch() throws Exception {
        Rec rec = new Rec();
        PairingServer ps = new PairingServer(rec);

        // pair_request frame -> pair_pending frame.
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        ps.handleFirstFrame("pair_request", reqBody(), out);
        Message m = FrameCodec.decode(
                new ByteArrayInputStream(out.toByteArray()));
        check("pair_request frame -> pair_pending",
                "pair_pending".equals(m.kind)
                        && m.body.get("pairing_id") != null
                        && ((String) m.body.get("code"))
                                .matches("[0-9]{6}"),
                m.kind + " " + m.body);
        String pid = (String) m.body.get("pairing_id");

        // pair_confirm before approval -> pair_refused frame (exact reason).
        out.reset();
        Map<String, Object> cb = new LinkedHashMap<>();
        cb.put("pairing_id", pid);
        ps.handleFirstFrame("pair_confirm", cb, out);
        m = FrameCodec.decode(
                new ByteArrayInputStream(out.toByteArray()));
        check("unapproved pair_confirm -> pair_refused",
                "pair_refused".equals(m.kind)
                        && ((String) m.body.get("reason"))
                                .contains("waiting for approval"),
                m.kind + " " + m.body);

        // approve + pair_confirm -> pair_ok with the agent token.
        ps.approvePairing(pid);
        out.reset();
        ps.handleFirstFrame("pair_confirm", cb, out);
        m = FrameCodec.decode(
                new ByteArrayInputStream(out.toByteArray()));
        check("approved pair_confirm -> pair_ok",
                "pair_ok".equals(m.kind)
                        && "tok-1".equals(m.body.get("agent_token")),
                m.kind + " " + m.body);

        // unknown pairing kind -> pair_refused (never a crash).
        out.reset();
        ps.handleFirstFrame("pair_bogus", new LinkedHashMap<>(), out);
        m = FrameCodec.decode(
                new ByteArrayInputStream(out.toByteArray()));
        check("bogus kind -> pair_refused",
                "pair_refused".equals(m.kind));
    }

    static void testBeaconBuild() {
        byte[] raw = DiscoveryBeacon.build("tab-1", "Test Tablet", 41234,
                "ab".repeat(32));
        String s = new String(raw,
                java.nio.charset.StandardCharsets.UTF_8);
        check("beacon builds valid JSON",
                s.contains("\"proto\":\"rd-disc/1\"")
                        && s.contains("\"device_id\":\"tab-1\"")
                        && s.contains("\"port\":41234"), s);
        // Invalid inputs throw (fail-closed, never a malformed beacon).
        boolean threw = false;
        try {
            DiscoveryBeacon.build("bad id!", "x", 1, "ab".repeat(32));
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        check("bad device_id rejected", threw);
        threw = false;
        try {
            DiscoveryBeacon.build("tab-1", "x", 1, "short");
        } catch (IllegalArgumentException e) {
            threw = true;
        }
        check("bad fingerprint rejected", threw);
    }
}
