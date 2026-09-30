package com.remor.dispatchtarget;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Iterator;

/**
 * Target-side consent screen. Shows the pending session's scope
 * (actions, bounds, apps, caps) and lets the device owner Grant or
 * Deny. Grant registers the session in the {@link RemoteListener}'s
 * registry -- the consent/scope binding: the session only ever acts
 * within the scope shown here.
 *
 * <p>Launched from MainActivity's consent poll. The listener must be
 * running.
 */
public class ConsentActivity extends Activity {

    public static final String EXTRA_SESSION_ID = "session_id";
    public static final String EXTRA_SCOPE_JSON = "scope_json";
    public static final String EXTRA_CREATED_AT = "created_at";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Intent in = getIntent();
        final String sessionId =
                in.getStringExtra(EXTRA_SESSION_ID);
        final String scopeJson =
                in.getStringExtra(EXTRA_SCOPE_JSON);
        final String createdAt =
                in.getStringExtra(EXTRA_CREATED_AT);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);
        scroll.addView(root);

        TextView title = new TextView(this);
        title.setText("Remote session request");
        title.setTextSize(20);
        root.addView(title);

        TextView sub = new TextView(this);
        sub.setText("Session " + shortId(sessionId)
                + (createdAt != null && !createdAt.isEmpty()
                        ? "\nRequested " + createdAt : "")
                + "\n\nA controller is asking to operate this device."
                + " Review the scope, then grant or deny. Granting"
                + " shows the LIVE indicator and allows input only"
                + " inside this scope.");
        sub.setPadding(0, pad / 2, 0, pad / 2);
        root.addView(sub);

        TextView scopeView = new TextView(this);
        scopeView.setText(renderScope(scopeJson));
        scopeView.setPadding(0, 0, 0, pad);
        root.addView(scopeView);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        Button deny = new Button(this);
        deny.setText("DENY");
        deny.setOnClickListener(v -> {
            Toast.makeText(this, "Session denied", Toast.LENGTH_SHORT)
                    .show();
            finish();
        });
        row.addView(deny);

        Button grant = new Button(this);
        grant.setText("GRANT");
        grant.setOnClickListener(v ->
                onGrant(sessionId, scopeJson));
        row.addView(grant);

        root.addView(row);
        setContentView(scroll);
    }

    private void onGrant(String sessionId, String scopeJson) {
        if (!RemoteListener.isRunning()) {
            Toast.makeText(this,
                    "Listener not running -- start it first",
                    Toast.LENGTH_LONG).show();
            return;
        }
        new Thread(() -> {
            try {
                TargetApiClient api = new TargetApiClient(this);
                JSONObject consent;
                try {
                    consent = api.grantConsent(sessionId);
                } catch (Exception boundary) {
                    // Named boundary: the controller track has not
                    // added the target-consent endpoint yet. Fall back
                    // to manual session entry (see report).
                    runOnUiThread(() -> Toast.makeText(this,
                            "Controller consent endpoint unavailable: "
                                    + boundary.getMessage()
                                    + ". Use manual session entry.",
                            Toast.LENGTH_LONG).show());
                    return;
                }
                String tokenHash = consent.optString("token_hash");
                double exp = consent.optDouble("consent_expires_at",
                        System.currentTimeMillis() / 1000.0 + 3600);
                boolean ok = RemoteListener.grantConsentedSession(
                        sessionId, tokenHash, scopeJson, exp);
                runOnUiThread(() -> {
                    Toast.makeText(this,
                            ok ? "Session granted -- LIVE"
                                    : "Grant failed: registry unavailable",
                            Toast.LENGTH_LONG).show();
                    finish();
                });
            } catch (Exception e) {
                runOnUiThread(() -> Toast.makeText(this,
                        "Grant failed: " + e.getMessage(),
                        Toast.LENGTH_LONG).show());
            }
        }, "consent-grant").start();
    }

    /** Human-readable rendering of the scope dict. */
    private static String renderScope(String scopeJson) {
        StringBuilder sb = new StringBuilder("Scope:\n");
        try {
            JSONObject s = new JSONObject(
                    scopeJson != null ? scopeJson : "{}");
            Iterator<String> keys = s.keys();
            while (keys.hasNext()) {
                String k = keys.next();
                Object v = s.get(k);
                sb.append("  ").append(k).append(": ");
                if (v instanceof JSONArray) {
                    JSONArray a = (JSONArray) v;
                    for (int i = 0; i < a.length(); i++) {
                        if (i > 0) {
                            sb.append(", ");
                        }
                        sb.append(a.opt(i));
                    }
                } else {
                    sb.append(v);
                }
                sb.append('\n');
            }
        } catch (Exception e) {
            sb.append("  (unparseable: ").append(e.getMessage())
                    .append(')');
        }
        return sb.toString();
    }

    private static String shortId(String id) {
        if (id == null) {
            return "?";
        }
        return id.length() > 13 ? id.substring(0, 13) : id;
    }
}
