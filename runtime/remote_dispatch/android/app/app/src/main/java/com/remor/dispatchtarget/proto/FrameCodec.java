package com.remor.dispatchtarget.proto;

import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Wire framing for the remote-dispatch protocol, mirroring
 * {@code runtime/remote_dispatch/protocol.py} exactly:
 *
 * <p>Frame: 4-byte big-endian length + UTF-8 JSON. Every message:
 * {@code {"v":1,"kind":...,"session_id":...,"seq":...,"nonce":...,
 * "body":{...}}}.
 */
public final class FrameCodec {
    public static final int VERSION = 1;
    public static final int MAX_FRAME = 4 * 1024 * 1024;

    private static final SecureRandom RANDOM = new SecureRandom();

    private FrameCodec() {}

    /** 16 random bytes as 32 hex chars (Python: secrets.token_hex(16)). */
    public static String newNonce() {
        byte[] b = new byte[16];
        RANDOM.nextBytes(b);
        StringBuilder sb = new StringBuilder(32);
        for (byte x : b) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    public static byte[] encode(String kind, String sessionId, long seq,
                               Map<String, Object> body) {
        Map<String, Object> msg = new LinkedHashMap<>();
        msg.put("v", (long) VERSION);
        msg.put("kind", kind);
        msg.put("session_id", sessionId);
        msg.put("seq", seq);
        msg.put("nonce", newNonce());
        msg.put("body", body);
        byte[] raw = Json.write(msg).getBytes(StandardCharsets.UTF_8);
        if (raw.length > MAX_FRAME) {
            throw new IllegalArgumentException("frame too large");
        }
        byte[] frame = new byte[4 + raw.length];
        frame[0] = (byte) (raw.length >>> 24);
        frame[1] = (byte) (raw.length >>> 16);
        frame[2] = (byte) (raw.length >>> 8);
        frame[3] = (byte) raw.length;
        System.arraycopy(raw, 0, frame, 4, raw.length);
        return frame;
    }

    /** Encode a message whose nonce is fixed (adversarial replays). */
    public static byte[] encodeWithNonce(String kind, String sessionId,
                                         long seq, String nonce,
                                         Map<String, Object> body) {
        Map<String, Object> msg = new LinkedHashMap<>();
        msg.put("v", (long) VERSION);
        msg.put("kind", kind);
        msg.put("session_id", sessionId);
        msg.put("seq", seq);
        msg.put("nonce", nonce);
        msg.put("body", body);
        byte[] raw = Json.write(msg).getBytes(StandardCharsets.UTF_8);
        if (raw.length > MAX_FRAME) {
            throw new IllegalArgumentException("frame too large");
        }
        byte[] frame = new byte[4 + raw.length];
        frame[0] = (byte) (raw.length >>> 24);
        frame[1] = (byte) (raw.length >>> 16);
        frame[2] = (byte) (raw.length >>> 8);
        frame[3] = (byte) raw.length;
        System.arraycopy(raw, 0, frame, 4, raw.length);
        return frame;
    }

    public static void send(OutputStream out, String kind, String sessionId,
                            long seq, Map<String, Object> body)
            throws IOException {
        byte[] frame = encode(kind, sessionId, seq, body);
        out.write(frame);
        out.flush();
    }

    public static Message decode(InputStream in) throws IOException {
        byte[] hdr = readExact(in, 4);
        long n = ((hdr[0] & 0xFFL) << 24) | ((hdr[1] & 0xFFL) << 16)
                | ((hdr[2] & 0xFFL) << 8) | (hdr[3] & 0xFFL);
        if (n > MAX_FRAME || n == 0) {
            throw new IOException("bad frame length " + n);
        }
        byte[] raw = readExact(in, (int) n);
        Object parsed;
        try {
            parsed = Json.parse(new String(raw, StandardCharsets.UTF_8));
        } catch (IllegalArgumentException e) {
            throw new IOException("bad frame JSON: " + e.getMessage());
        }
        if (!(parsed instanceof Map)) {
            throw new IOException("frame is not a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) parsed;
        Object v = m.get("v");
        if (!(v instanceof Long) || ((Long) v) != VERSION) {
            throw new IOException("unsupported protocol version " + v);
        }
        return Message.fromMap(m);
    }

    private static byte[] readExact(InputStream in, int n) throws IOException {
        byte[] buf = new byte[n];
        int off = 0;
        while (off < n) {
            int r = in.read(buf, off, n - off);
            if (r < 0) throw new EOFException("peer closed mid-frame");
            off += r;
        }
        return buf;
    }
}
