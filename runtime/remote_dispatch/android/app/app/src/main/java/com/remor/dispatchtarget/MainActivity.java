package com.remor.dispatchtarget;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.Gravity;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * One-time setup for the dispatch target app.
 *
 * <p>The user must: (1) enable the REMOR dispatch AccessibilityService,
 * (2) grant "display over other apps" for the LIVE indicator overlay,
 * (3) paste the target-side user token (issued once by the Python
 * target's {@code register_user}) so the on-device KILL button can
 * authenticate its {@code user_kill} bridge event.
 *
 * <p>Hardening gap (stated, not hidden): the token is kept in
 * app-private SharedPreferences. It should move to
 * EncryptedSharedPreferences / Android Keystore before public release.
 */
public class MainActivity extends Activity {

    private static final int REQ_SCREEN_CAPTURE = 4101;
    private static final int REQ_POST_NOTIFICATIONS = 1;
    private static final String PREF_NOTIF_ASKED = "notif_asked";

    private TextView statusView;
    private EditText tokenField;
    private EditText controllerUrlField;
    private EditText deviceIdField;
    private EditText agentIdField;
    private EditText agentTokenField;
    private EditText listenerPortField;
    private TextView fingerprintView;
    private TextView listenerStatusView;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Crash diagnostics: every future crash in this process writes
        // its own trace to app-private storage (idempotent).
        CrashDiagnostics.install(this);

        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        layout.setPadding(pad, pad, pad, pad);
        scroll.addView(layout);

        TextView title = new TextView(this);
        title.setText("REMOR Dispatch Target — setup");
        title.setTextSize(20);
        layout.addView(title);

        statusView = new TextView(this);
        statusView.setPadding(0, pad, 0, pad);
        layout.addView(statusView);

        layout.addView(mkButton("Open Accessibility settings", v ->
                startActivity(new Intent(
                        Settings.ACTION_ACCESSIBILITY_SETTINGS))));
        layout.addView(mkButton("Grant overlay permission", v -> {
            if (Build.VERSION.SDK_INT >= 23 && !Settings.canDrawOverlays(this)) {
                Intent i = new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName()));
                startActivity(i);
            }
        }));
        // Screen capture for the session stream (BridgeProtocol.md v1).
        // The system shows its one-time consent dialog; on grant the
        // projection is held by CaptureService (foreground service,
        // required on API 29+). Until granted, capture_frame answers
        // CAPTURE_UNAVAILABLE -- fail-closed, never a placeholder.
        layout.addView(mkButton("Grant screen capture", v -> {
            MediaProjectionManager mpm = (MediaProjectionManager)
                    getSystemService(Context.MEDIA_PROJECTION_SERVICE);
            startActivityForResult(
                    mpm.createScreenCaptureIntent(), REQ_SCREEN_CAPTURE);
        }));
        if (Build.VERSION.SDK_INT >= 33) {
            layout.addView(mkButton("Allow notifications (kill switch)", v ->
                    onNotificationButton()));
        }

        layout.addView(mkButton("Copy diagnostics", v -> {
            String diag = CrashDiagnostics.readLatest(this);
            ClipboardManager cm = (ClipboardManager)
                    getSystemService(Context.CLIPBOARD_SERVICE);
            cm.setPrimaryClip(ClipData.newPlainText(
                    "dispatch-target-diagnostics", diag));
            tokenField.setHint("diagnostics copied to clipboard");
        }));

        TextView tokenLabel = new TextView(this);
        tokenLabel.setText("Target-side user token (for the KILL button):");
        tokenLabel.setPadding(0, pad, 0, 0);
        layout.addView(tokenLabel);

        tokenField = new EditText(this);
        tokenField.setHint("rduser_…");
        SharedPreferences prefs =
                getSharedPreferences(
                        DispatchAccessibilityService.PREFS, MODE_PRIVATE);
        String existing = prefs.getString(
                DispatchAccessibilityService.PREF_USER_TOKEN, "");
        if (!existing.isEmpty()) {
            tokenField.setHint("token saved ✓ (paste to replace)");
        }
        layout.addView(tokenField);

        layout.addView(mkButton("Save user token", v -> {
            String t = tokenField.getText().toString().trim();
            if (!t.isEmpty()) {
                prefs.edit().putString(
                        DispatchAccessibilityService.PREF_USER_TOKEN, t)
                        .apply();
                tokenField.setText("");
                tokenField.setHint("token saved ✓ (paste to replace)");
            }
        }));

        TextView bridgeInfo = new TextView(this);
        bridgeInfo.setPadding(0, pad, 0, 0);
        bridgeInfo.setGravity(Gravity.CENTER_HORIZONTAL);
        bridgeInfo.setText("Bridge: 127.0.0.1:"
                + DispatchAccessibilityService.BRIDGE_PORT
                + " (localhost only)");
        layout.addView(bridgeInfo);

        // ---------------------------------------------------------
        // Remote dispatch: LAN listener + controller pairing.
        // Out-of-band pairing ceremony:
        //   1. Start the listener below; note the TLS fingerprint.
        //   2. On the controller machine, run the pairing step with
        //      this device's fingerprint; it prints the agent token.
        //   3. Enter the controller URL, device id, agent id and the
        //      agent token here and save. The token is app-private
        //      (see the EncryptedSharedPreferences hardening gap).
        //   4. Press Announce: the controller learns this target's
        //      endpoint and can request sessions. Session requests
        //      appear via "Check for sessions" for explicit consent.
        // ---------------------------------------------------------
        TextView rdTitle = new TextView(this);
        rdTitle.setText("Remote dispatch (LAN)");
        rdTitle.setTextSize(20);
        rdTitle.setPadding(0, pad, 0, 0);
        layout.addView(rdTitle);

        listenerStatusView = new TextView(this);
        listenerStatusView.setPadding(0, pad / 2, 0, pad / 2);
        layout.addView(listenerStatusView);

        fingerprintView = new TextView(this);
        fingerprintView.setPadding(0, 0, 0, pad / 2);
        fingerprintView.setTextIsSelectable(true);
        layout.addView(fingerprintView);

        SharedPreferences rdPrefs =
                getSharedPreferences(RemoteListener.PREFS, MODE_PRIVATE);

        controllerUrlField = mkField(layout,
                "Controller URL (e.g. http://192.168.1.10:8080):",
                rdPrefs.getString(RemoteListener.PREF_CONTROLLER_URL,
                        ""));
        deviceIdField = mkField(layout, "Device ID:",
                rdPrefs.getString(RemoteListener.PREF_DEVICE_ID, ""));
        agentIdField = mkField(layout, "Agent ID:",
                rdPrefs.getString(RemoteListener.PREF_AGENT_ID, ""));
        agentTokenField = mkField(layout, "Agent token (from pairing):",
                "");
        String savedAgent = rdPrefs.getString(
                RemoteListener.PREF_AGENT_TOKEN, "");
        if (!savedAgent.isEmpty()) {
            agentTokenField.setHint("token saved ✓ (paste to replace)");
        }
        listenerPortField = mkField(layout,
                "Listener port (0 = ephemeral):",
                String.valueOf(rdPrefs.getInt(
                        RemoteListener.PREF_LISTENER_PORT, 0)));

        layout.addView(mkButton("Save pairing", v -> {
            SharedPreferences.Editor e = rdPrefs.edit();
            e.putString(RemoteListener.PREF_CONTROLLER_URL,
                    controllerUrlField.getText().toString().trim());
            e.putString(RemoteListener.PREF_DEVICE_ID,
                    deviceIdField.getText().toString().trim());
            e.putString(RemoteListener.PREF_AGENT_ID,
                    agentIdField.getText().toString().trim());
            String at = agentTokenField.getText().toString().trim();
            if (!at.isEmpty()) {
                e.putString(RemoteListener.PREF_AGENT_TOKEN, at);
                agentTokenField.setText("");
                agentTokenField.setHint(
                        "token saved ✓ (paste to replace)");
            }
            try {
                e.putInt(RemoteListener.PREF_LISTENER_PORT,
                        Integer.parseInt(listenerPortField.getText()
                                .toString().trim()));
            } catch (NumberFormatException nfe) {
                e.putInt(RemoteListener.PREF_LISTENER_PORT, 0);
            }
            e.apply();
            refreshStatus();
        }));

        layout.addView(mkButton("Start listener", v -> {
            Intent i = new Intent(this, RemoteListener.class)
                    .setAction(RemoteListener.ACTION_START);
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(i);
            } else {
                startService(i);
            }
            listenerStatusView.postDelayed(this::refreshStatus, 1500);
        }));

        layout.addView(mkButton("Stop listener", v -> {
            Intent i = new Intent(this, RemoteListener.class)
                    .setAction(RemoteListener.ACTION_STOP);
            startService(i);
            listenerStatusView.postDelayed(this::refreshStatus, 500);
        }));

        layout.addView(mkButton("Announce to controller", v ->
                onAnnounce()));

        layout.addView(mkButton("Check for sessions (consent)", v ->
                onCheckSessions()));

        setContentView(scroll);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode,
                                    Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_SCREEN_CAPTURE) {
            if (resultCode == RESULT_OK && data != null) {
                Intent svc = new Intent(this, CaptureService.class)
                        .setAction(CaptureService.ACTION_START)
                        .putExtra(CaptureService.EXTRA_RESULT_CODE,
                                resultCode)
                        .putExtra(CaptureService.EXTRA_RESULT_DATA, data);
                if (Build.VERSION.SDK_INT >= 26) {
                    startForegroundService(svc);
                } else {
                    startService(svc);
                }
            }
            refreshStatus();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        CaptureService.setStateListener(
                () -> runOnUiThread(this::refreshStatus));
        refreshStatus();
    }

    @Override
    protected void onPause() {
        CaptureService.setStateListener(null);
        super.onPause();
    }

    /**
     * The notification affordance must never silently do nothing.
     * Three truthful branches: already granted → say so; the system
     * would show the dialog → request it; the system would silently
     * swallow the request (denied twice / don't-ask-again / policy)
     * → deep-link to the app's notification settings instead.
     */
    private void onNotificationButton() {
        if (checkSelfPermission(
                "android.permission.POST_NOTIFICATIONS")
                == PackageManager.PERMISSION_GRANTED) {
            refreshStatus();
            return;
        }
        SharedPreferences prefs = getSharedPreferences(
                DispatchAccessibilityService.PREFS, MODE_PRIVATE);
        boolean askedBefore = prefs.getBoolean(PREF_NOTIF_ASKED, false);
        if (askedBefore && !shouldShowRequestPermissionRationale(
                "android.permission.POST_NOTIFICATIONS")) {
            Intent i = new Intent(
                    Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                    .putExtra(Settings.EXTRA_APP_PACKAGE,
                            getPackageName());
            startActivity(i);
            return;
        }
        prefs.edit().putBoolean(PREF_NOTIF_ASKED, true).apply();
        requestPermissions(
                new String[]{"android.permission.POST_NOTIFICATIONS"},
                REQ_POST_NOTIFICATIONS);
    }

    private Button mkButton(String label,
                            android.view.View.OnClickListener onClick) {
        Button b = new Button(this);
        b.setText(label);
        b.setOnClickListener(onClick);
        return b;
    }

    private EditText mkField(LinearLayout layout, String label,
                             String value) {
        TextView tv = new TextView(this);
        tv.setText(label);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        tv.setPadding(0, pad / 2, 0, 0);
        layout.addView(tv);
        EditText f = new EditText(this);
        f.setText(value);
        layout.addView(f);
        return f;
    }

    /** Announce this target's endpoint to the configured controller. */
    private void onAnnounce() {
        TargetApiClient api = new TargetApiClient(this);
        if (!api.configured()) {
            listenerStatusView.setText(
                    "Announce: save the pairing first (controller URL,"
                            + " device id, agent id, agent token).");
            return;
        }
        if (!RemoteListener.isRunning()) {
            listenerStatusView.setText(
                    "Announce: start the listener first.");
            return;
        }
        listenerStatusView.setText("Announcing…");
        new Thread(() -> {
            try {
                String fp = RemoteListener.currentFingerprint();
                int port = RemoteListener.boundPort();
                if (port <= 0) {
                    runOnUiThread(() -> listenerStatusView.setText(
                            "Announce: listener has no bound port."));
                    return;
                }
                org.json.JSONObject resp = api.announce(port, fp);
                runOnUiThread(() -> {
                    if (resp.optBoolean("ok", false)) {
                        listenerStatusView.setText(
                                "Announced OK: controller knows this"
                                        + " target.");
                    } else {
                        listenerStatusView.setText(
                                "Announce failed: "
                                        + resp.optString("error",
                                                "unknown"));
                    }
                });
            } catch (Exception e) {
                runOnUiThread(() -> listenerStatusView.setText(
                        "Announce failed: " + e.getMessage()));
            }
        }, "rd-announce").start();
    }

    /** Poll the controller for pending sessions and open consent. */
    private void onCheckSessions() {
        TargetApiClient api = new TargetApiClient(this);
        if (!api.configured()) {
            listenerStatusView.setText(
                    "Save the pairing first.");
            return;
        }
        if (!RemoteListener.isRunning()) {
            listenerStatusView.setText(
                    "Start the listener first.");
            return;
        }
        listenerStatusView.setText("Checking for sessions…");
        new Thread(() -> {
            try {
                java.util.List<TargetApiClient.PendingSession> pending =
                        api.pollPending();
                runOnUiThread(() -> {
                    if (pending.isEmpty()) {
                        listenerStatusView.setText(
                                "No pending sessions.");
                        return;
                    }
                    TargetApiClient.PendingSession s = pending.get(0);
                    Intent i = new Intent(this, ConsentActivity.class)
                            .putExtra(ConsentActivity.EXTRA_SESSION_ID,
                                    s.sessionId)
                            .putExtra(ConsentActivity.EXTRA_SCOPE_JSON,
                                    s.scope.toString())
                            .putExtra(ConsentActivity.EXTRA_CREATED_AT,
                                    s.createdAt);
                    startActivity(i);
                    listenerStatusView.setText(
                            pending.size() + " pending session(s).");
                });
            } catch (Exception e) {
                runOnUiThread(() -> listenerStatusView.setText(
                        "Session check failed: " + e.getMessage()));
            }
        }, "rd-session-poll").start();
    }

    private void refreshStatus() {
        boolean a11y = isAccessibilityEnabled();
        boolean overlay = Build.VERSION.SDK_INT < 23
                || Settings.canDrawOverlays(this);
        boolean notif = Build.VERSION.SDK_INT < 33
                || checkSelfPermission(
                        "android.permission.POST_NOTIFICATIONS")
                        == PackageManager.PERMISSION_GRANTED;
        SharedPreferences prefs = getSharedPreferences(
                DispatchAccessibilityService.PREFS, MODE_PRIVATE);
        boolean token = !prefs.getString(
                DispatchAccessibilityService.PREF_USER_TOKEN, "").isEmpty();
        // The terminal capture state, not a transient sample: the
        // worker thread may still be building the pipeline when this
        // runs, and a failure must show its reason, never a bare
        // MISSING that hides the cause.
        CaptureService.StateSnapshot snap = CaptureService.captureState();
        String captureMark;
        switch (snap.state) {
            case ACTIVE:
                captureMark = "OK";
                break;
            case STARTING:
                captureMark = "STARTING…";
                break;
            case FAILED:
                captureMark = "FAILED: " + snap.detail;
                break;
            default:
                captureMark = "MISSING";
                break;
        }
        statusView.setText(
                "Accessibility service: " + mark(a11y) + "\n"
                        + "Overlay permission: " + mark(overlay) + "\n"
                        + "Notification permission: " + mark(notif) + "\n"
                        + "User token saved: " + mark(token) + "\n"
                        + "Screen capture: " + captureMark);
        // Remote-dispatch listener status.
        boolean listening = RemoteListener.isRunning();
        listenerStatusView.setText(
                "Listener: " + (listening ? "RUNNING" : "stopped"));
        String fp = RemoteListener.currentFingerprint();
        fingerprintView.setText(listening
                ? "TLS fingerprint (pair with this):\n" + fp
                : "TLS fingerprint: (start the listener to mint/show)");
    }

    private String mark(boolean ok) {
        return ok ? "OK" : "MISSING";
    }

    /**
     * Real ComponentName semantics: the system stores fully-qualified
     * flattened names (pkg/pkg.Class) in ENABLED_ACCESSIBILITY_SERVICES.
     * Comparing a hand-built "pkg/.Class" string never matched, so the
     * setup screen reported MISSING for an enabled service. Each entry
     * is unflattened and compared as a ComponentName.
     */
    private boolean isAccessibilityEnabled() {
        ComponentName expected =
                new ComponentName(this, DispatchAccessibilityService.class);
        String enabled = Settings.Secure.getString(getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (enabled == null) {
            return false;
        }
        TextUtils.SimpleStringSplitter splitter =
                new TextUtils.SimpleStringSplitter(':');
        splitter.setString(enabled);
        while (splitter.hasNext()) {
            ComponentName entry =
                    ComponentName.unflattenFromString(splitter.next());
            if (expected.equals(entry)) {
                return true;
            }
        }
        return false;
    }
}
