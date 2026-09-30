package com.remor.dispatchtarget.proto;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON writer/parser for the remote-dispatch wire protocol.
 *
 * <p>Pure Java, zero android.* imports. Compact output (no whitespace),
 * matching Python's {@code json.dumps(..., separators=(",", ":"))}.
 * Numbers: the parser returns {@link Long} for plain integers and
 * {@link Double} for fraction/exponent forms, mirroring Python's
 * int/float split; the writer renders integral values without a decimal
 * point. This keeps refusal-reason strings (e.g. coordinates) identical
 * to the Python side's for realistic values.
 */
public final class Json {
    private Json() {}

    // -- writer -------------------------------------------------------

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeInto(sb, v);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeInto(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v) ? "true" : "false");
        } else if (v instanceof Long || v instanceof Integer
                || v instanceof Short || v instanceof Byte) {
            sb.append(((Number) v).longValue());
        } else if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException(
                        "non-finite double not allowed in JSON");
            }
            sb.append(Double.toString(d));
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e
                    : ((Map<String, Object>) v).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                writeInto(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object e : (List<Object>) v) {
                if (!first) sb.append(',');
                first = false;
                writeInto(sb, e);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException(
                    "unwritable JSON value: " + v.getClass());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    // -- parser -------------------------------------------------------

    public static Object parse(String s) {
        Parser p = new Parser(s);
        Object v = p.value();
        p.ws();
        if (!p.eof()) {
            throw new IllegalArgumentException(
                    "trailing characters after JSON value");
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String s) {
        Object v = parse(s);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("not a JSON object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        private final String s;
        private int i;

        Parser(String s) { this.s = s; }

        boolean eof() { return i >= s.length(); }

        void ws() {
            while (!eof()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++;
                else break;
            }
        }

        Object value() {
            ws();
            if (eof()) throw new IllegalArgumentException("empty JSON");
            char c = s.charAt(i);
            switch (c) {
                case '{': return object();
                case '[': return array();
                case '"': return string();
                case 't': return literal("true", Boolean.TRUE);
                case 'f': return literal("false", Boolean.FALSE);
                case 'n': return literal("null", null);
                default: return number();
            }
        }

        private Object literal(String word, Object v) {
            if (s.startsWith(word, i)) {
                i += word.length();
                return v;
            }
            throw new IllegalArgumentException("bad literal at " + i);
        }

        private Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; // {
            ws();
            if (!eof() && s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                if (eof() || s.charAt(i) != '"') {
                    throw new IllegalArgumentException(
                            "expected string key at " + i);
                }
                String k = string();
                ws();
                if (eof() || s.charAt(i) != ':') {
                    throw new IllegalArgumentException(
                            "expected ':' at " + i);
                }
                i++;
                m.put(k, value());
                ws();
                if (eof()) throw new IllegalArgumentException(
                        "unterminated object");
                char c = s.charAt(i++);
                if (c == '}') return m;
                if (c != ',') throw new IllegalArgumentException(
                        "expected ',' or '}' at " + (i - 1));
            }
        }

        private List<Object> array() {
            List<Object> l = new ArrayList<>();
            i++; // [
            ws();
            if (!eof() && s.charAt(i) == ']') { i++; return l; }
            while (true) {
                l.add(value());
                ws();
                if (eof()) throw new IllegalArgumentException(
                        "unterminated array");
                char c = s.charAt(i++);
                if (c == ']') return l;
                if (c != ',') throw new IllegalArgumentException(
                        "expected ',' or ']' at " + (i - 1));
            }
        }

        private String string() {
            i++; // opening quote
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw new IllegalArgumentException(
                        "unterminated string");
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw new IllegalArgumentException(
                            "unterminated escape");
                    char e = s.charAt(i++);
                    switch (e) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'u':
                            if (i + 4 > s.length()) {
                                throw new IllegalArgumentException(
                                        "bad \\u escape");
                            }
                            sb.append((char) Integer.parseInt(
                                    s.substring(i, i + 4), 16));
                            i += 4;
                            break;
                        default: throw new IllegalArgumentException(
                                "bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        private Number number() {
            int start = i;
            if (!eof() && s.charAt(i) == '-') i++;
            while (!eof() && Character.isDigit(s.charAt(i))) i++;
            boolean isFloat = false;
            if (!eof() && s.charAt(i) == '.') {
                isFloat = true; i++;
                while (!eof() && Character.isDigit(s.charAt(i))) i++;
            }
            if (!eof() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
                isFloat = true; i++;
                if (!eof() && (s.charAt(i) == '+' || s.charAt(i) == '-')) i++;
                while (!eof() && Character.isDigit(s.charAt(i))) i++;
            }
            String tok = s.substring(start, i);
            try {
                return isFloat ? (Number) Double.parseDouble(tok)
                               : (Number) Long.parseLong(tok);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("bad number: " + tok);
            }
        }
    }
}
