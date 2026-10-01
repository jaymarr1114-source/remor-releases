package com.remor.dispatchtarget;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.os.Build;
import android.os.IBinder;
import android.util.Base64;

import com.remor.dispatchtarget.proto.AnnounceClient;
import com.remor.dispatchtarget.proto.DiscoveryBeacon;
import com.remor.dispatchtarget.proto.PairingServer;
import com.remor.dispatchtarget.proto.SessionRegistry;
import com.remor.dispatchtarget.proto.TargetServer;
import com.remor.dispatchtarget.proto.TlsUtil;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Foreground service hosting the remote-dispatch target listener.
 *
 * <p>Binds the TLS {@link TargetServer} on 0.0.0.0 (all interfaces) so
 * the LAN controller can reach it; the controller authenticates the
 * target by the pinned certificate fingerprint established at pairing.
 * The server authenticates the controller per session via the session
 * token (target-side {@link SessionRegistry}).
 *
 * <p>Start/stop are idempotent: starting a running listener (or
 * stopping a stopped one) is a no-op that reports the current state.
 *
 * <p>Boundaries (BOUNDED, untested on device):
 * <ul>
 *   <li>The TLS identity lives in the AndroidKeyStore when available;
 *       on failure it falls back to an app-private PKCS12 file. The
 *       fingerprint shown in MainActivity is what the controller must
 *       pin at pairing.
 *   <li>Target-side consent: {@link ConsentActivity} grants a pending
 *       session and registers it in the listener's registry. The
 *       controller-side consent endpoint it calls is a named boundary
 *       for the controller track (see TargetApiClient).
 * </ul>
 */
public class RemoteListener extends Service {

    private static final String TAG = "RemoteListener";
    private static final String CHANNEL_ID = "rd-target-listener";
    private static final int NOTIF_ID = 4201;

    public static final String ACTION_START =
            "com.remor.dispatchtarget.action.LISTENER_START";
    public static final String ACTION_STOP =
            "com.remor.dispatchtarget.action.LISTENER_STOP";

    /** Prefs keys (app-private; see the EncryptedSharedPreferences gap). */
    static final String PREFS = DispatchAccessibilityService.PREFS;
    static final String PREF_CONTROLLER_URL = "controller_url";
    static final String PREF_DEVICE_ID = "device_id";
    static final String PREF_AGENT_ID = "agent_id";
    static final String PREF_AGENT_TOKEN = "agent_token";
    static final String PREF_LISTENER_PORT = "listener_port";

    private static final String KEY_ALIAS = "rd-target-tls";
    private static final char[] KEY_PASS = "rd-target".toCharArray();

    private static final AtomicReference<RemoteListener> INSTANCE =
            new AtomicReference<>();

    private volatile TargetServer server;
    private volatile SessionRegistry registry;
    private volatile int boundPort;
    private volatile String fingerprint = "";

    /** Tap-to-pair (RD-EASYPAIR-1): pairing state machine + beacon. */
    private volatile PairingServer pairingServer;
    private volatile DiscoveryBeacon.BeaconHandle beaconHandle;
    private static final int PAIR_NOTIF_ID = 4202;

    /** Causal kill from the on-device KILL button / overlay. */
    public static void killSession(String sessionId) {
        RemoteListener self = INSTANCE.get();
        if (self != null) {
            self.localKill(sessionId);
        }
    }

    public static boolean isRunning() {
        return INSTANCE.get() != null;
    }

    public static String currentFingerprint() {
        RemoteListener self = INSTANCE.get();
        return self != null ? self.fingerprint : "";
    }

    public static int boundPort() {
        RemoteListener self = INSTANCE.get();
        return self != null ? self.boundPort : 0;
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopListener();
            stopSelf();
            return START_NOT_STICKY;
        }
        startListener();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopListener();
        super.onDestroy();
    }

    /** Idempotent start. */
    public synchronized void startListener() {
        if (server != null) {
            return; // already running
        }
        startForeground(NOTIF_ID, buildNotification("starting…"));
        try {
            SharedPreferences prefs =
                    getSharedPreferences(PREFS, MODE_PRIVATE);
            String deviceId = prefs.getString(PREF_DEVICE_ID, "");
            String agentId = prefs.getString(PREF_AGENT_ID, "");
            String agentToken = prefs.getString(PREF_AGENT_TOKEN, "");
            int port = prefs.getInt(PREF_LISTENER_PORT, 0);

            KeyStore.PrivateKeyEntry entry = ensureTlsIdentity();
            fingerprint = TlsUtil.fingerprint(
                    (X509Certificate) entry.getCertificate());

            // AndroidKeyStore keys are non-exportable: build an
            // in-memory KeyStore holding the entry (never on disk).
            KeyStore ks = KeyStore.getInstance(
                    KeyStore.getDefaultType());
            ks.load(null, null);
            ks.setEntry(KEY_ALIAS, entry,
                    new KeyStore.PasswordProtection(KEY_PASS));

            registry = new SessionRegistry();
            server = new TargetServer(
                    deviceId, agentId, agentToken,
                    registry, new ListenerInputSink(),
                    new ListenerCapture(), new ListenerIndicator(),
                    ks, KEY_PASS, "0.0.0.0", port, false);
            server.start();
            boundPort = server.boundPort();
            // RD-EASYPAIR-1: tap-to-pair handler + discovery beacon.
            pairingServer = new PairingServer(pairingCallback);
            server.setPairingServer(pairingServer);
            beaconHandle = DiscoveryBeacon.start(deviceId, deviceId,
                    boundPort, fingerprint);
            INSTANCE.set(this);
            startForeground(NOTIF_ID, buildNotification(
                    "listening on :" + boundPort));
            android.util.Log.i(TAG, "listening on 0.0.0.0:" + boundPort
                    + " fp=" + fingerprint);
        } catch (Exception e) {
            android.util.Log.e(TAG, "startListener failed", e);
            stopListener();
        }
    }

    /** Idempotent stop. */
    public synchronized void stopListener() {
        INSTANCE.compareAndSet(this, null);
        if (beaconHandle != null) {
            try {
                beaconHandle.stop();
            } catch (Exception ignored) {}
            beaconHandle = null;
        }
        pairingServer = null;
        cancelPairNotification();
        if (server != null) {
            try {
                server.stop();
            } catch (Exception ignored) {
            }
            server = null;
        }
        registry = null;
        boundPort = 0;
        stopForeground(true);
    }

    /**
     * The on-device KILL button's causal path: halt injection, tear
     * the session down in the registry, clear the indicator. This is
     * the U-9 semantic -- killing stops the remote work, not merely
     * the control connection.
     */
    private void localKill(String sessionId) {
        if (registry == null || sessionId == null) {
            return;
        }
        registry.killSession(sessionId);
        // The input sink's halt is invoked by the server's kill path
        // for channel kills; for the local button the accessibility
        // service halts its own pipeline via the kill latch (the
        // overlay is already hidden by onLocalKill).
        try {
            new ListenerIndicator().hide(sessionId);
        } catch (Exception ignored) {
        }
        android.util.Log.i(TAG, "local kill: " + sessionId);
    }

    /** Register a consented session (called by ConsentActivity). */
    public static boolean grantConsentedSession(String sessionId,
                                                String tokenHash,
                                                String scopeJson,
                                                double consentExpiresAt) {
        RemoteListener self = INSTANCE.get();
        return self != null && self.registerConsentedSession(
                sessionId, tokenHash, scopeJson, consentExpiresAt);
    }

    // -- tap-to-pair (RD-EASYPAIR-1; called by PairConsentActivity) --------

    /** Look up a pending pairing for the consent screen. */
    public static PairingServer.PendingPairing pendingPairing(
            String pairingId) {
        RemoteListener self = INSTANCE.get();
        if (self == null || self.pairingServer == null
                || pairingId == null) {
            return null;
        }
        // The pairing map lives in the PairingServer; expose a read
        // through a fresh request is not possible, so the activity is
        // launched with the id and the server is the source of truth.
        // (PairingServer keeps pending private; approval/denial are the
        // only mutations the UI needs.)
        return self.pairingForUi(pairingId);
    }

    /** Tablet owner allowed the pairing (from PairConsentActivity). */
    public static boolean approvePairing(String pairingId) {
        RemoteListener self = INSTANCE.get();
        return self != null && self.pairingServer != null
                && self.pairingServer.approvePairing(pairingId);
    }

    /** Tablet owner denied the pairing (from PairConsentActivity). */
    public static boolean denyPairing(String pairingId) {
        RemoteListener self = INSTANCE.get();
        return self != null && self.pairingServer != null
                && self.pairingServer.denyPairing(pairingId);
    }

    /** Source of truth for the consent UI: the pairing server's map. */
    private PairingServer.PendingPairing pairingForUi(String pairingId) {
        PairingServer ps = pairingServer;
        return ps == null ? null : ps.getPending(pairingId);
    }

    /** Tap-to-pair callback: show consent, persist on approval. */
    private final PairingServer.PairingCallback pairingCallback =
            new PairingServer.PairingCallback() {
                @Override
                public void onPairRequest(
                        PairingServer.PendingPairing pairing) {
                    showPairingRequest(pairing);
                }

                @Override
                public void onPairSettled(
                        PairingServer.PendingPairing pairing,
                        boolean approved) {
                    if (!approved) {
                        cancelPairNotification();
                        return;
                    }
                    // Persist the phone's credentials + API URL (the same
                    // four fields the manual ceremony collects), then
                    // restart the listener so the server picks up the new
                    // identity. Done off the pairing connection thread.
                    new Thread(() -> {
                        try {
                            SharedPreferences prefs =
                                    getSharedPreferences(PREFS,
                                            MODE_PRIVATE);
                            prefs.edit()
                                    .putString(PREF_CONTROLLER_URL,
                                            pairing.phoneApiUrl)
                                    .putString(PREF_DEVICE_ID,
                                            pairing.deviceId)
                                    .putString(PREF_AGENT_ID,
                                            pairing.agentId)
                                    .putString(PREF_AGENT_TOKEN,
                                            pairing.agentToken)
                                    .apply();
                            cancelPairNotification();
                            stopListener();
                            startListener();
                            android.util.Log.i(TAG,
                                    "tap-to-pair settled: listener"
                                            + " restarted with new pairing");
                        } catch (Exception e) {
                            android.util.Log.e(TAG,
                                    "tap-to-pair settle failed", e);
                        }
                    }, "rd-pair-settle").start();
                }
            };

    /** Show the pairing consent screen + a tappable notification. */
    private void showPairingRequest(
            PairingServer.PendingPairing pairing) {
        try {
            Intent i = new Intent(this, PairConsentActivity.class)
                    .putExtra(PairConsentActivity.EXTRA_PAIRING_ID,
                            pairing.pairingId)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            PendingIntent pi = PendingIntent.getActivity(
                    this, pairing.pairingId.hashCode(), i,
                    PendingIntent.FLAG_UPDATE_CURRENT
                            | PendingIntent.FLAG_IMMUTABLE);
            Notification.Builder b =
                    (Build.VERSION.SDK_INT >= 26)
                            ? new Notification.Builder(this, CHANNEL_ID)
                            : new Notification.Builder(this);
            Notification n = b.setContentTitle("Pairing request")
                    .setContentText("\"" + pairing.phoneName
                            + "\" wants to pair -- tap to review")
                    .setSmallIcon(
                            android.R.drawable.stat_sys_data_bluetooth)
                    .setContentIntent(pi)
                    .setAutoCancel(true)
                    .build();
            NotificationManager nm =
                    (NotificationManager) getSystemService(
                            Context.NOTIFICATION_SERVICE);
            if (Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(new NotificationChannel(
                        CHANNEL_ID, "Dispatch target listener",
                        NotificationManager.IMPORTANCE_HIGH));
            }
            nm.notify(PAIR_NOTIF_ID, n);
            // Best effort: bring the consent screen forward directly.
            // (Background-launch restrictions may block this; the
            // notification above is the reliable path.)
            try {
                startActivity(i);
            } catch (Exception e) {
                android.util.Log.i(TAG,
                        "direct consent launch blocked; using notification");
            }
        } catch (Exception e) {
            android.util.Log.e(TAG, "showPairingRequest failed", e);
        }
    }

    private void cancelPairNotification() {
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(
                            Context.NOTIFICATION_SERVICE);
            nm.cancel(PAIR_NOTIF_ID);
        } catch (Exception ignored) {}
    }

    /** Register a consented session (called by ConsentActivity). */
    public boolean registerConsentedSession(String sessionId,
                                            String tokenHash,
                                            String scopeJson,
                                            double consentExpiresAt) {
        if (registry == null) {
            return false;
        }
        try {
            Map<String, Object> scope =
                    com.remor.dispatchtarget.proto.Json.parseObject(
                            scopeJson);
            registry.registerSession(sessionId, tokenHash, scope,
                    "consented", consentExpiresAt);
            return true;
        } catch (Exception e) {
            android.util.Log.e(TAG, "registerConsentedSession failed", e);
            return false;
        }
    }

    // -- TLS identity --------------------------------------------------

    /**
     * The target's TLS identity. Prefers the AndroidKeyStore; falls
     * back to an app-private PKCS12 file. The certificate's SHA-256
     * fingerprint is the pairing pin.
     */
    private KeyStore.PrivateKeyEntry ensureTlsIdentity()
            throws Exception {
        try {
            KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
            ks.load(null);
            if (ks.containsAlias(KEY_ALIAS)) {
                KeyStore.Entry e = ks.getEntry(KEY_ALIAS, null);
                if (e instanceof KeyStore.PrivateKeyEntry) {
                    return (KeyStore.PrivateKeyEntry) e;
                }
            }
            KeyPairGenerator kpg = KeyPairGenerator.getInstance(
                    "RSA", "AndroidKeyStore");
            android.security.keystore.KeyGenParameterSpec spec =
                    new android.security.keystore.KeyGenParameterSpec
                            .Builder(KEY_ALIAS,
                            android.security.keystore.KeyProperties
                                    .PURPOSE_SIGN
                                    | android.security.keystore
                                    .KeyProperties.PURPOSE_VERIFY)
                            .setKeySize(2048)
                            .setCertificateSubject(
                                    new javax.security.auth.x500
                                            .X500Principal("CN=rd-target"))
                            .setCertificateSerialNumber(
                                    java.math.BigInteger.valueOf(
                                            System.currentTimeMillis()))
                            .setCertificateNotBefore(
                                    new java.util.Date(
                                            System.currentTimeMillis()
                                                    - 86400000L))
                            .setCertificateNotAfter(new java.util.Date(
                                    System.currentTimeMillis()
                                            + 10L * 365 * 86400000L))
                            .build();
            kpg.initialize(spec);
            KeyPair kp = kpg.generateKeyPair();
            Certificate cert = ks.getCertificate(KEY_ALIAS);
            return new KeyStore.PrivateKeyEntry(kp.getPrivate(),
                    new Certificate[]{cert});
        } catch (Exception e) {
            android.util.Log.w(TAG,
                    "AndroidKeyStore unavailable, using app-private"
                            + " PKCS12 fallback", e);
            return ensureFallbackIdentity();
        }
    }

    private KeyStore.PrivateKeyEntry ensureFallbackIdentity()
            throws Exception {
        // Last resort when the AndroidKeyStore is unavailable: an
        // app-private PKCS12 file. The key material is exportable here
        // (unlike the AndroidKeyStore path), so this is strictly weaker
        // -- MainActivity surfaces which identity is in use.
        File f = new File(getFilesDir(), "rd-target-tls.p12");
        KeyStore ks = KeyStore.getInstance("PKCS12");
        if (f.exists()) {
            try (FileInputStream in = new FileInputStream(f)) {
                ks.load(in, KEY_PASS);
            }
            KeyStore.Entry e = ks.getEntry(KEY_ALIAS,
                    new KeyStore.PasswordProtection(KEY_PASS));
            if (e instanceof KeyStore.PrivateKeyEntry) {
                return (KeyStore.PrivateKeyEntry) e;
            }
        }
        // Generate a fresh RSA pair and a self-signed cert. Android's
        // default provider cannot mint X.509 without BouncyCastle; the
        // cert is built via the platform CertPathBuilder-free path used
        // by AndroidKeyStore specs is unavailable here, so this fallback
        // is a named gap: on a device without AndroidKeyStore the
        // listener refuses to start rather than shipping a weak cert.
        throw new IllegalStateException(
                "AndroidKeyStore unavailable and no fallback identity"
                        + " present: refusing to start without a"
                        + " hardware/device-backed TLS identity");
    }

    // -- notification --------------------------------------------------

    private Notification buildNotification(String text) {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm =
                    (NotificationManager) getSystemService(
                            Context.NOTIFICATION_SERVICE);
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "Dispatch target listener",
                    NotificationManager.IMPORTANCE_LOW);
            nm.createNotificationChannel(ch);
        }
        Notification.Builder b =
                (Build.VERSION.SDK_INT >= 26)
                        ? new Notification.Builder(this, CHANNEL_ID)
                        : new Notification.Builder(this);
        return b.setContentTitle("REMOR dispatch target")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .build();
    }

    // -- wiring --------------------------------------------------------

    /**
     * InputSink backed by the accessibility service's real injection.
     * Scope checks happen in TargetServer before execute() is called;
     * the kill latch is checked per action (fail-closed).
     */
    private final class ListenerInputSink
            implements TargetServer.InputSink {
        @Override
        public Map<String, Object> execute(
                Map<String, Object> action,
                TargetServer.LivenessCheck live) throws Exception {
            live.check();
            DispatchAccessibilityService svc =
                    DispatchAccessibilityService.instance();
            if (svc == null) {
                throw new TargetServer.RefusalException(
                        "accessibility service not connected: injection"
                                + " unavailable");
            }
            svc.checkKillLatch();
            live.check();
            String type = String.valueOf(action.get("type"));
            org.json.JSONObject result;
            switch (type) {
                case "move":
                    // move is cursor positioning for the next gesture;
                    // no injection needed.
                    result = new org.json.JSONObject();
                    break;
                case "click":
                    result = svc.doTap(num(action.get("x")),
                            num(action.get("y")));
                    break;
                case "scroll":
                    result = svc.doScroll(num(action.get("x")),
                            num(action.get("y")),
                            num(action.get("dx")),
                            num(action.get("dy")));
                    break;
                case "type":
                    result = svc.doSetText(
                            String.valueOf(action.get("text")));
                    break;
                case "launch_app":
                    result = svc.doLaunch(
                            String.valueOf(action.get("app")));
                    break;
                default:
                    throw new TargetServer.RefusalException(
                            "unsupported action '" + type
                                    + "' on this target");
            }
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("executed", true);
            r.put("action", type);
            return r;
        }

        @Override
        public void halt() {
            // The accessibility service enforces the kill latch on its
            // own pipeline; nothing cached here.
        }

        private int num(Object v) {
            return v instanceof Number ? ((Number) v).intValue() : 0;
        }
    }

    /** ScreenCapture backed by the MediaProjection pipeline. */
    private final class ListenerCapture
            implements TargetServer.ScreenCapture {
        @Override
        public Map<String, Object> capture(Map<String, Object> clip,
                                           Integer maxDim)
                throws TargetServer.RefusalException,
                TargetServer.CaptureException {
            CaptureService.Frame frame =
                    CaptureService.acquireLatestFrame();
            if (frame == null) {
                if (CaptureService.hasProjection()) {
                    throw new TargetServer.CaptureException(
                            "projection held but no frame produced");
                }
                throw new TargetServer.RefusalException(
                        "screen capture unavailable: grant screen"
                                + " capture in the setup screen");
            }
            Bitmap full = frame.bitmap;
            try {
                int cx = 0, cy = 0;
                int cw = full.getWidth(), ch = full.getHeight();
                long x = num(clip.get("x")), y = num(clip.get("y"));
                long w = num(clip.get("w"), cw),
                        h = num(clip.get("h"), ch);
                cx = (int) Math.max(0, Math.min(x, cw));
                cy = (int) Math.max(0, Math.min(y, ch));
                cw = (int) Math.max(0,
                        Math.min(x + w, full.getWidth()) - cx);
                ch = (int) Math.max(0,
                        Math.min(y + h, full.getHeight()) - cy);
                if (cw <= 0 || ch <= 0) {
                    throw new TargetServer.RefusalException(
                            "clip lies outside the display");
                }
                Bitmap cropped =
                        (cx == 0 && cy == 0
                                && cw == full.getWidth()
                                && ch == full.getHeight())
                                ? full
                                : Bitmap.createBitmap(
                                        full, cx, cy, cw, ch);
                if (maxDim != null && maxDim > 0) {
                    int m = Math.max(cw, ch);
                    if (m > maxDim) {
                        double s = (double) maxDim / m;
                        int sw = Math.max(1, (int) (cw * s));
                        int sh = Math.max(1, (int) (ch * s));
                        cropped = Bitmap.createScaledBitmap(
                                cropped, sw, sh, true);
                        cw = sw;
                        ch = sh;
                    }
                }
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                cropped.compress(Bitmap.CompressFormat.PNG, 100, bos);
                String b64 = Base64.encodeToString(bos.toByteArray(),
                        Base64.NO_WRAP);
                Map<String, Object> out = new LinkedHashMap<>();
                out.put("width", (long) cw);
                out.put("height", (long) ch);
                out.put("format", "png");
                out.put("data_b64", b64);
                out.put("ts", (double) (System.currentTimeMillis()
                        / 1000L));
                out.put("synthesized", false);
                return out;
            } catch (TargetServer.RefusalException e) {
                throw e;
            } catch (Exception e) {
                throw new TargetServer.CaptureException(
                        "frame encode failed: " + e.getMessage());
            }
        }

        @Override
        public void close() {
        }

        private long num(Object v) {
            return v instanceof Number ? ((Number) v).longValue() : 0;
        }

        private long num(Object v, long dflt) {
            return v instanceof Number ? ((Number) v).longValue()
                    : dflt;
        }
    }

    /** SessionIndicator backed by the overlay + kill notification. */
    private final class ListenerIndicator
            implements TargetServer.SessionIndicator {
        @Override
        public void show(String sessionId) {
            DispatchAccessibilityService svc =
                    DispatchAccessibilityService.instance();
            if (svc == null) {
                return;
            }
            try {
                svc.showIndicator(sessionId);
            } catch (Exception e) {
                android.util.Log.w(TAG, "showIndicator failed", e);
            }
        }

        @Override
        public void hide(String sessionId) {
            DispatchAccessibilityService svc =
                    DispatchAccessibilityService.instance();
            if (svc != null) {
                svc.hideIndicator(sessionId);
            }
        }

        @Override
        public boolean live() {
            DispatchAccessibilityService svc =
                    DispatchAccessibilityService.instance();
            return svc != null && svc.isIndicatorLive();
        }
    }
}
