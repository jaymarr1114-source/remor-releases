package com.remor.dispatchtarget;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.graphics.PixelFormat;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;
import android.util.DisplayMetrics;
import android.view.Gravity;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/**
 * The Android input substrate for remote dispatch.
 *
 * <p>This service performs ONLY what the bridge commands ask: tap,
 * swipe, scroll (as swipe), set-text on the focused editable node,
 * global actions (back/home/recents), and app launch. It performs NO
 * authorization of its own: sessions, consent, scope, replay guards,
 * budgets, and kill authority live in the Python RemoteDispatchTarget,
 * which is the enforcement point. The bridge protocol (see
 * BridgeProtocol.md) carries primitives, never session tokens.
 *
 * <p>User-local kill: the overlay banner and the persistent
 * notification both carry a KILL control. Pressing it hides the
 * overlay immediately (fail-closed, no waiting) and sends a
 * {@code user_kill} bridge event to the Python target, which runs its
 * target-local {@code user_kill(session_id, user_token)} entry point.
 * The controller has no path to this.
 */
public class DispatchAccessibilityService extends AccessibilityService {

    public static final int BRIDGE_PORT = 47631;
    static final String PREFS = "dispatch_target_prefs";
    static final String PREF_USER_TOKEN = "user_token";

    /** Live instance for the RemoteListener's input sink/indicator. */
    private static volatile DispatchAccessibilityService instance;

    public static DispatchAccessibilityService instance() {
        return instance;
    }
    private static final String ACTION_LOCAL_KILL =
            "com.remor.dispatchtarget.LOCAL_KILL";
    private static final int NOTIF_ID = 3101;
    private static final long GESTURE_TIMEOUT_S = 15;

    private Handler mainHandler;
    private BridgeServer bridge;
    private WindowManager windowManager;
    private android.view.View overlayView;
    private String overlaySessionId;
    private BroadcastReceiver killReceiver;

    // Fail-closed user kill latch. Set the instant the user presses KILL
    // (before any network I/O). While non-null it holds the killed
    // session's id and every actuator command is refused at the app --
    // even if the user_kill bridge event never reached the Python target.
    // Rearm: only via showIndicator for a DIFFERENT session
    // (indicator_show is emitted by the Python target exactly once per
    // newly consented session, and consent is granted target-side),
    // never by a remote command.
    private volatile String killLatchedSession = null;

    /**
     * Refuse actuator input while the user's kill latch is set.
     * BridgeServer calls this before dispatching any input command;
     * the SecurityException becomes an ok:false bridge reply.
     */
    void checkKillLatch() throws SecurityException {
        String latched = killLatchedSession;
        if (latched != null) {
            throw new SecurityException(
                    "KILL_LATCHED: local kill latched for session "
                    + latched + ": input blocked until a fresh"
                    + " consented session");
        }
    }

    // ------------------------------------------------------------------
    @Override
    public void onServiceConnected() {
        instance = this;
        CrashDiagnostics.install(this);
        mainHandler = new Handler(Looper.getMainLooper());
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
        bridge = new BridgeServer(this, BRIDGE_PORT);
        bridge.start();

        killReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent intent) {
                String sid = intent.getStringExtra("session_id");
                onLocalKill(sid != null ? sid : "");
            }
        };
        IntentFilter filter = new IntentFilter(ACTION_LOCAL_KILL);
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(killReceiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            // API 26-32: no NOT_EXPORTED flag; the action string is
            // app-specific and the receiver only kills the session
            // (fail-closed), so export is harmless.
            registerReceiver(killReceiver, filter);
        }
        postKillNotification("");
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // No event processing needed: this service is an actuator,
        // not an observer.
    }

    @Override
    public void onInterrupt() {
    }

    @Override
    public void onDestroy() {
        instance = null;
        try {
            unregisterReceiver(killReceiver);
        } catch (Exception ignored) {
        }
        hideOverlay();
        if (bridge != null) {
            bridge.stop();
        }
        super.onDestroy();
    }

    // ------------------------------------------------------------------
    // Bridge command implementations. Called on bridge client threads;
    // everything touching the framework is marshaled to the main thread
    // and waited on, so results are real, not assumed.
    // ------------------------------------------------------------------

    JSONObject ping() throws Exception {
        JSONObject display = new JSONObject();
        DisplayMetrics dm = getResources().getDisplayMetrics();
        display.put("width", dm.widthPixels);
        display.put("height", dm.heightPixels);
        JSONObject r = new JSONObject();
        r.put("service_connected", true);
        r.put("display", display);
        return r;
    }

    JSONObject doTap(int x, int y) throws Exception {
        Path path = new Path();
        path.moveTo(x, y);
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, 80))
                .build();
        dispatchSync(gesture);
        JSONObject r = new JSONObject();
        r.put("x", x);
        r.put("y", y);
        return r;
    }

    JSONObject doSwipe(int x1, int y1, int x2, int y2, int durationMs)
            throws Exception {
        int dur = Math.max(10, Math.min(2000, durationMs));
        Path path = new Path();
        path.moveTo(x1, y1);
        path.lineTo(x2, y2);
        GestureDescription gesture = new GestureDescription.Builder()
                .addStroke(new GestureDescription.StrokeDescription(path, 0, dur))
                .build();
        dispatchSync(gesture);
        JSONObject r = new JSONObject();
        r.put("x1", x1);
        r.put("y1", y1);
        r.put("x2", x2);
        r.put("y2", y2);
        r.put("duration_ms", dur);
        return r;
    }

    JSONObject doScroll(int x, int y, int dx, int dy) throws Exception {
        // One swipe per scroll action, finger moving opposite the
        // content delta; 150px per unit, clamped to the display.
        DisplayMetrics dm = getResources().getDisplayMetrics();
        int x2 = clamp(x - dx * 150, 0, dm.widthPixels - 1);
        int y2 = clamp(y - dy * 150, 0, dm.heightPixels - 1);
        doSwipe(x, y, x2, y2, 300);
        JSONObject r = new JSONObject();
        r.put("x", x);
        r.put("y", y);
        r.put("dx", dx);
        r.put("dy", dy);
        return r;
    }

    JSONObject doSetText(String text) throws Exception {
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicReference<String> err = new AtomicReference<>(null);
        final AtomicInteger entered = new AtomicInteger(0);
        mainHandler.post(() -> {
            AccessibilityNodeInfo root = null;
            try {
                root = getRootInActiveWindow();
                AccessibilityNodeInfo node = findFocusedEditable(root);
                if (node == null) {
                    err.set("no focused editable node");
                } else {
                    Bundle args = new Bundle();
                    args.putCharSequence(
                            AccessibilityNodeInfo
                                    .ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            text);
                    if (node.performAction(
                            AccessibilityNodeInfo.ACTION_SET_TEXT, args)) {
                        entered.set(text.length());
                    } else {
                        err.set("ACTION_SET_TEXT rejected by node");
                    }
                    node.recycle();
                }
            } catch (Exception e) {
                err.set(String.valueOf(e.getMessage()));
            } finally {
                if (root != null) {
                    root.recycle();
                }
                latch.countDown();
            }
        });
        await(latch, "set_text");
        if (err.get() != null) {
            throw new Exception(err.get());
        }
        JSONObject r = new JSONObject();
        r.put("entered_chars", entered.get());
        return r;
    }

    JSONObject doGlobalAction(String action) throws Exception {
        final int code;
        switch (action) {
            case "back": code = GLOBAL_ACTION_BACK; break;
            case "home": code = GLOBAL_ACTION_HOME; break;
            case "recents": code = GLOBAL_ACTION_RECENTS; break;
            case "notifications": code = GLOBAL_ACTION_NOTIFICATIONS; break;
            case "quick_settings": code = GLOBAL_ACTION_QUICK_SETTINGS; break;
            case "power_dialog": code = GLOBAL_ACTION_POWER_DIALOG; break;
            default: throw new Exception("unknown global action " + action);
        }
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicBoolean ok = new AtomicBoolean(false);
        mainHandler.post(() -> {
            try {
                ok.set(performGlobalAction(code));
            } finally {
                latch.countDown();
            }
        });
        await(latch, "global_action");
        if (!ok.get()) {
            throw new Exception("global action " + action + " refused by system");
        }
        return new JSONObject();
    }

    JSONObject doLaunch(String pkg) throws Exception {
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicReference<String> err = new AtomicReference<>(null);
        mainHandler.post(() -> {
            try {
                PackageManager pm = getPackageManager();
                Intent intent = pm.getLaunchIntentForPackage(pkg);
                if (intent == null) {
                    err.set("no launch intent for package " + pkg);
                } else {
                    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(intent);
                }
            } catch (Exception e) {
                err.set(String.valueOf(e.getMessage()));
            } finally {
                latch.countDown();
            }
        });
        await(latch, "launch");
        if (err.get() != null) {
            throw new Exception(err.get());
        }
        JSONObject r = new JSONObject();
        r.put("package", pkg);
        r.put("launched", true);
        return r;
    }

    // ------------------------------------------------------------------
    // Screen capture (session stream)
    // ------------------------------------------------------------------

    /**
     * Serve one capture_frame bridge command via the app's
     * MediaProjection flow ({@link CaptureService}).
     *
     * <p>Fail-closed, per BridgeProtocol.md v1:
     * <ul>
     *   <li>No media-projection grant → {@code CAPTURE_UNAVAILABLE}
     *       (never a stale or placeholder image).
     *   <li>The user kill latch gates capture like every actuator
     *       command (enforced by {@code BridgeServer} before this
     *       runs): a latched kill stops the stream even in the
     *       lost-event case.
     *   <li>The requested {@code clip} is intersected with the real
     *       display at capture; {@code max_w}/{@code max_h} (0 = none)
     *       bound the returned size, downscaling preserving aspect.
     *   <li>No frame is persisted: the Bitmap is recycled before
     *       return, the Image is closed on acquire.
     *   <li>{@code synthesized} is false: these are real captures.
     * </ul>
     */
    JSONObject doCaptureFrame(JSONObject p) throws Exception {
        CaptureService.Frame frame = CaptureService.acquireLatestFrame();
        if (frame == null) {
            if (CaptureService.hasProjection()) {
                throw new Exception("CAPTURE_FAILED: projection held but"
                        + " the pipeline produced no frame");
            }
            throw new Exception("CAPTURE_UNAVAILABLE: media projection"
                    + " permission not granted (grant screen capture in"
                    + " the target app's setup screen)");
        }
        Bitmap full = frame.bitmap;
        try {
            // Intersect the requested clip with the real display.
            int cx = 0, cy = 0, cw = full.getWidth(), ch = full.getHeight();
            JSONObject clip = p.optJSONObject("clip");
            if (clip != null) {
                int x = clip.optInt("x", 0);
                int y = clip.optInt("y", 0);
                int w = clip.optInt("w", cw);
                int h = clip.optInt("h", ch);
                int x2 = Math.min(x + w, cw);
                int y2 = Math.min(y + h, ch);
                cx = Math.max(x, 0);
                cy = Math.max(y, 0);
                cw = Math.max(x2 - cx, 0);
                ch = Math.max(y2 - cy, 0);
                if (cw <= 0 || ch <= 0) {
                    throw new Exception(
                            "clip lies outside the display");
                }
            }
            Bitmap cropped = (cx == 0 && cy == 0
                    && cw == full.getWidth() && ch == full.getHeight())
                    ? full
                    : Bitmap.createBitmap(full, cx, cy, cw, ch);
            // Downscale only, preserving aspect.
            int maxW = p.optInt("max_w", 0);
            int maxH = p.optInt("max_h", 0);
            double scale = 1.0;
            if (maxW > 0) {
                scale = Math.min(scale, (double) maxW / cw);
            }
            if (maxH > 0) {
                scale = Math.min(scale, (double) maxH / ch);
            }
            Bitmap out = cropped;
            if (scale < 1.0) {
                int sw = Math.max(1, (int) (cw * scale));
                int sh = Math.max(1, (int) (ch * scale));
                out = Bitmap.createScaledBitmap(cropped, sw, sh, true);
            }
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            if (!out.compress(Bitmap.CompressFormat.PNG, 100, bos)) {
                throw new Exception("PNG encode failed");
            }
            String b64 = Base64.encodeToString(
                    bos.toByteArray(), Base64.NO_WRAP);
            JSONObject r = new JSONObject();
            r.put("width", out.getWidth());
            r.put("height", out.getHeight());
            r.put("format", "png");
            r.put("data_b64", b64);
            r.put("ts", frame.tsSeconds);
            r.put("synthesized", false);
            if (out != cropped) {
                out.recycle();
            }
            if (cropped != full) {
                cropped.recycle();
            }
            return r;
        } finally {
            full.recycle();
        }
    }

    // ------------------------------------------------------------------
    // Indicator overlay + kill
    // ------------------------------------------------------------------

    void showIndicator(String sessionId) throws Exception {
        // Rearm the kill latch only for a genuinely new session: a
        // killed session never goes live again, so an indicator_show for
        // the latched session id would be a protocol violation and must
        // NOT rearm.
        String latched = killLatchedSession;
        if (latched != null && !latched.equals(sessionId)) {
            killLatchedSession = null;
        }
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicReference<String> err = new AtomicReference<>(null);
        mainHandler.post(() -> {
            try {
                hideOverlay();
                LinearLayout layout =
                        new LinearLayout(DispatchAccessibilityService.this);
                layout.setOrientation(LinearLayout.HORIZONTAL);
                layout.setBackgroundColor(0xFFCC0000);
                int pad = (int) (12 * getResources().getDisplayMetrics().density);
                layout.setPadding(pad, pad, pad, pad);

                TextView tv = new TextView(DispatchAccessibilityService.this);
                String sid = sessionId.length() > 13
                        ? sessionId.substring(0, 13) : sessionId;
                tv.setText("REMOTE SESSION LIVE " + sid);
                tv.setTextColor(0xFFFFFFFF);
                tv.setTextSize(14);
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                        0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f);
                layout.addView(tv, lp);

                Button kill = new Button(DispatchAccessibilityService.this);
                kill.setText("KILL");
                kill.setOnClickListener(v -> onLocalKill(sessionId));
                layout.addView(kill);

                WindowManager.LayoutParams params =
                        new WindowManager.LayoutParams(
                                WindowManager.LayoutParams.MATCH_PARENT,
                                WindowManager.LayoutParams.WRAP_CONTENT,
                                WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
                                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
                                PixelFormat.TRANSLUCENT);
                params.gravity = Gravity.TOP | Gravity.CENTER_HORIZONTAL;
                windowManager.addView(layout, params);
                overlayView = layout;
                overlaySessionId = sessionId;
                postKillNotification(sessionId);
            } catch (Exception e) {
                err.set(String.valueOf(e.getMessage()));
            } finally {
                latch.countDown();
            }
        });
        await(latch, "indicator_show");
        if (err.get() != null) {
            throw new Exception(err.get());
        }
    }

    void hideIndicator(String sessionId) {
        // "" or matching id hides; a non-matching id is ignored so a
        // stale hide cannot drop another session's banner.
        if (sessionId != null && !sessionId.isEmpty()
                && overlaySessionId != null
                && !overlaySessionId.equals(sessionId)) {
            return;
        }
        hideOverlay();
        cancelKillNotification();
    }

    boolean isIndicatorLive() {
        return overlayView != null;
    }

    private void hideOverlay() {
        if (overlayView != null) {
            try {
                windowManager.removeView(overlayView);
            } catch (Exception ignored) {
            }
            overlayView = null;
            overlaySessionId = null;
        }
    }

    /**
     * The user's kill switch. Latches FIRST (fail-closed, before any
     * network I/O): from this instant no bridge actuator command
     * executes, even if the user_kill event below never reaches Python.
     * Then hides the overlay and tells the Python target to run its
     * target-local user_kill entry point.
     */
    void onLocalKill(String sessionId) {
        killLatchedSession =
                (sessionId == null || sessionId.isEmpty())
                        ? "<unknown>" : sessionId;
        hideOverlay();
        cancelKillNotification();
        // Causal kill (U-9): tear the session down in the target's own
        // registry and clear the indicator -- not just the latch above.
        // Works even when no control connection is currently open.
        RemoteListener.killSession(sessionId);
        new Thread(() -> {
            try {
                SharedPreferences prefs =
                        getSharedPreferences(PREFS, MODE_PRIVATE);
                String userToken = prefs.getString(PREF_USER_TOKEN, "");
                JSONObject event = new JSONObject();
                event.put("event", "user_kill");
                event.put("session_id", sessionId);
                event.put("user_token", userToken);
                bridge.sendEventToPython(event);
            } catch (Exception ignored) {
                // The overlay is already hidden: the kill is effective
                // locally even if the event never reaches Python.
            }
        }, "local-kill").start();
    }

    private void postKillNotification(String sessionId) {
        NotificationManager nm =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    getString(R.string.kill_channel),
                    "Dispatch kill switch",
                    NotificationManager.IMPORTANCE_HIGH);
            nm.createNotificationChannel(ch);
        }
        Intent killIntent = new Intent(ACTION_LOCAL_KILL);
        killIntent.setPackage(getPackageName());
        killIntent.putExtra("session_id", sessionId);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= 23
                        ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent pi = PendingIntent.getBroadcast(
                this, 0, killIntent, flags);
        Notification.Builder b = new Notification.Builder(this)
                .setContentTitle("REMOR remote session LIVE")
                .setContentText("Tap KILL to stop the remote session now.")
                .setSmallIcon(android.R.drawable.ic_dialog_alert)
                .addAction(new Notification.Action.Builder(
                        android.graphics.drawable.Icon.createWithResource(
                                this, android.R.drawable.ic_dialog_alert),
                        "KILL", pi).build());
        if (Build.VERSION.SDK_INT >= 26) {
            b.setChannelId(getString(R.string.kill_channel));
        }
        nm.notify(NOTIF_ID, b.build());
    }

    private void cancelKillNotification() {
        NotificationManager nm =
                (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.cancel(NOTIF_ID);
    }

    // ------------------------------------------------------------------
    private AccessibilityNodeInfo findFocusedEditable(
            AccessibilityNodeInfo node) {
        if (node == null) {
            return null;
        }
        if (node.isFocused() && node.isEditable()) {
            return node;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            AccessibilityNodeInfo child = node.getChild(i);
            AccessibilityNodeInfo found = findFocusedEditable(child);
            if (found != null) {
                return found;
            }
        }
        return null;
    }

    private void dispatchSync(GestureDescription gesture) throws Exception {
        final CountDownLatch latch = new CountDownLatch(1);
        final AtomicBoolean ok = new AtomicBoolean(false);
        mainHandler.post(() -> dispatchGesture(gesture,
                new GestureResultCallback() {
                    @Override
                    public void onCompleted(GestureDescription g) {
                        ok.set(true);
                        latch.countDown();
                    }

                    @Override
                    public void onCancelled(GestureDescription g) {
                        latch.countDown();
                    }
                }, null));
        await(latch, "gesture");
        if (!ok.get()) {
            throw new Exception("gesture cancelled by system");
        }
    }

    private void await(CountDownLatch latch, String what) throws Exception {
        if (!latch.await(GESTURE_TIMEOUT_S, TimeUnit.SECONDS)) {
            throw new Exception(what + " timed out");
        }
    }

    private int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }
}
