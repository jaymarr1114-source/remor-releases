package com.remor.dispatchtarget;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
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

    private TextView statusView;
    private EditText tokenField;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

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
        if (Build.VERSION.SDK_INT >= 33) {
            layout.addView(mkButton("Allow notifications (kill switch)", v ->
                    requestPermissions(
                            new String[]{
                                    "android.permission.POST_NOTIFICATIONS"},
                            1)));
        }

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
    protected void onResume() {
        super.onResume();
        refreshStatus();
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
        SharedPreferences prefs = getSharedPreferences(
                DispatchAccessibilityService.PREFS, MODE_PRIVATE);
        boolean token = !prefs.getString(
                DispatchAccessibilityService.PREF_USER_TOKEN, "").isEmpty();
        statusView.setText(
                "Accessibility service: " + mark(a11y) + "\n"
                        + "Overlay permission: " + mark(overlay) + "\n"
                        + "User token saved: " + mark(token));
    }

    private String mark(boolean ok) {
        return ok ? "OK" : "MISSING";
    }

    private boolean isAccessibilityEnabled() {
        String expected = getPackageName()
                + "/.DispatchAccessibilityService";
        String enabled = Settings.Secure.getString(getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES);
        if (enabled == null) {
            return false;
        }
        TextUtils.SimpleStringSplitter splitter =
                new TextUtils.SimpleStringSplitter(':');
        splitter.setString(enabled);
        while (splitter.hasNext()) {
            if (splitter.next().equalsIgnoreCase(expected)) {
                return true;
            }
        }
        return false;
    }
}
