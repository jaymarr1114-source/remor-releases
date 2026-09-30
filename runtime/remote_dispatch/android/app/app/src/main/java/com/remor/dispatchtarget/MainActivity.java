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
        refreshStatus();
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
        boolean capture = CaptureService.hasProjection();
        statusView.setText(
                "Accessibility service: " + mark(a11y) + "\n"
                        + "Overlay permission: " + mark(overlay) + "\n"
                        + "Notification permission: " + mark(notif) + "\n"
                        + "User token saved: " + mark(token) + "\n"
                        + "Screen capture: " + mark(capture));
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
