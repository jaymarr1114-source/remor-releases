package com.remor.dispatchtarget.proto;

import java.util.Collections;
import java.util.Map;

/** One decoded protocol message. */
public final class Message {
    public final long v;
    public final String kind;
    public final String sessionId;
    public final long seq;
    public final String nonce;
    public final Map<String, Object> body;

    public Message(long v, String kind, String sessionId, long seq,
                   String nonce, Map<String, Object> body) {
        this.v = v;
        this.kind = kind;
        this.sessionId = sessionId;
        this.seq = seq;
        this.nonce = nonce;
        this.body = body;
    }

    @SuppressWarnings("unchecked")
    static Message fromMap(Map<String, Object> m) {
        Object body = m.get("body");
        return new Message(
                ((Number) m.get("v")).longValue(),
                (String) m.get("kind"),
                (String) m.get("session_id"),
                ((Number) m.get("seq")).longValue(),
                (String) m.get("nonce"),
                body instanceof Map ? (Map<String, Object>) body
                                    : Collections.emptyMap());
    }
}
