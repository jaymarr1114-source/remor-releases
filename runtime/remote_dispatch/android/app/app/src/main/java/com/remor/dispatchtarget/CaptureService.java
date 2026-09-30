package com.remor.dispatchtarget;

import android.app.Activity;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.view.WindowManager;

import java.nio.ByteBuffer;

/**
 * Foreground service owning the MediaProjection screen-capture
 * pipeline for the session stream (BridgeProtocol.md v1, "Screen
 * capture (MediaProjection contract)").
 *
 * <p>Why a foreground service: on API 29+ the system throws
 * SecurityException from {@code getMediaProjection()} unless the
 * caller is a foreground service with the {@code mediaProjection}
 * type. The grant itself comes from the user's one-time system
 * dialog, launched from {@link MainActivity}; this service only
 * <em>holds</em> the granted projection.
 *
 * <p>Fail-closed contract:
 * <ul>
 *   <li>No grant / projection stopped → {@link #acquireLatestFrame()}
 *       returns null → the bridge answers CAPTURE_UNAVAILABLE. Never
 *       a stale or placeholder frame.
 *   <li>The service keeps no frame history and writes no frame to
 *       storage: each acquire builds one Bitmap from the latest
 *       Image and closes the Image immediately.
 *   <li>The user kill latch is enforced by {@code BridgeServer}
 *       before {@code capture_frame} is dispatched, so a latched
 *       kill stops the stream even in the lost-event case. The
 *       projection itself is a user-level grant, not per-session,
 *       and survives across sessions until the user revokes it.
 * </ul>
 */
public class CaptureService extends Service {

    public static final String ACTION_START =
            "com.remor.dispatchtarget.CAPTURE_START";
    public static final String EXTRA_RESULT_CODE = "result_code";
    public static final String EXTRA_RESULT_DATA = "result_data";

    private static final int NOTIF_ID = 3102;
    private static final String CHANNEL_ID = "dispatch_capture";
    private static final String VD_NAME = "remor-dispatch-capture";
    private static final int MAX_IMAGES = 3;
    // How long to wait for the first frame after the projection
    // starts producing (transient, not a permission failure).
    private static final long FIRST_FRAME_WAIT_MS = 3000;

    private static final Object LOCK = new Object();
    private static CaptureService instance = null;

    private MediaProjection projection;
    private VirtualDisplay virtualDisplay;
    private ImageReader imageReader;
    private HandlerThread workerThread;
    private Handler workerHandler;
    private int displayWidth;
    private int displayHeight;
    private int displayDpi;

    /** One acquired frame. The Bitmap is freshly built per acquire;
     * the caller owns it and must recycle it. */
    public static final class Frame {
        public final Bitmap bitmap; // ARGB_8888, full display size
        public final double tsSeconds; // image acquisition time
        public final int width;
        public final int height;

        Frame(Bitmap bitmap, double tsSeconds) {
            this.bitmap = bitmap;
            this.tsSeconds = tsSeconds;
            this.width = bitmap.getWidth();
            this.height = bitmap.getHeight();
        }
    }

    /** True when a live projection is held. Shown in the setup UI;
     * never cached across process death (static, process-local). */
    public static boolean hasProjection() {
        synchronized (LOCK) {
            return instance != null && instance.projection != null
                    && instance.imageReader != null;
        }
    }

    /**
     * Acquire the latest frame, or null when no projection is active.
     * Null is fail-closed (bridge → CAPTURE_UNAVAILABLE), never a
     * placeholder.
     */
    public static Frame acquireLatestFrame() {
        CaptureService svc;
        synchronized (LOCK) {
            svc = instance;
        }
        if (svc == null) {
            return null;
        }
        return svc.acquireFrame();
    }

    @Override
    public void onCreate() {
        super.onCreate();
        synchronized (LOCK) {
            instance = this;
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_START.equals(intent.getAction())) {
            int resultCode = intent.getIntExtra(
                    EXTRA_RESULT_CODE, Activity.RESULT_CANCELED);
            Intent data = null;
            if (Build.VERSION.SDK_INT >= 33) {
                data = intent.getParcelableExtra(
                        EXTRA_RESULT_DATA, Intent.class);
            } else {
                @SuppressWarnings("deprecation")
                Intent legacy = intent.getParcelableExtra(EXTRA_RESULT_DATA);
                data = legacy;
            }
            // startForeground stays on the main thread: it must run
            // promptly after startForegroundService (FGS timeout).
            // Everything else -- getMediaProjection, ImageReader,
            // createVirtualDisplay -- runs on the worker thread. Doing
            // it on the main thread froze the app on the grant path
            // (multi-second main-thread stall on real devices).
            startForegroundWithType();
            ensureWorkerThread();
            final int rc = resultCode;
            final Intent d = data;
            final Handler h;
            synchronized (LOCK) {
                h = workerHandler;
            }
            if (h != null) {
                h.post(() -> startCapture(rc, d));
            }
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        teardown();
        synchronized (LOCK) {
            if (instance == this) {
                instance = null;
            }
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ------------------------------------------------------------------

    private void startCapture(int resultCode, Intent data) {
        try {
            startCaptureInner(resultCode, data);
        } catch (Throwable t) {
            // Fail-closed: never let a capture-setup failure crash
            // the app -- this includes Errors such as an
            // OutOfMemoryError from the full-screen ImageReader
            // allocation. The bridge answers CAPTURE_UNAVAILABLE.
            teardown();
            stopForeground(true);
        }
    }

    private void startCaptureInner(int resultCode, Intent data) {
        if (resultCode != Activity.RESULT_OK || data == null) {
            // No grant: stay fail-closed. The service simply has no
            // projection; captures answer CAPTURE_UNAVAILABLE.
            // Drop foreground status: there is nothing to hold.
            stopForeground(true);
            return;
        }
        MediaProjectionManager mpm = (MediaProjectionManager)
                getSystemService(Context.MEDIA_PROJECTION_SERVICE);
        MediaProjection proj;
        try {
            proj = mpm.getMediaProjection(resultCode, data);
        } catch (Exception e) {
            stopForeground(true);
            return; // fail-closed: no projection held
        }
        resolveDisplaySize();
        // A second grant replaces the first: tear down the old
        // pipeline before building the new one (no leak).
        teardownPipeline();
        ImageReader reader = ImageReader.newInstance(
                displayWidth, displayHeight, PixelFormat.RGBA_8888,
                MAX_IMAGES);
        VirtualDisplay vd = proj.createVirtualDisplay(
                VD_NAME, displayWidth, displayHeight, displayDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                reader.getSurface(), null, workerHandler);
        proj.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() {
                // The user revoked the projection (or the system
                // stopped it): drop everything so the next capture
                // answers CAPTURE_UNAVAILABLE instead of serving
                // frames from a dead pipeline.
                teardown();
            }
        }, workerHandler);
        synchronized (LOCK) {
            projection = proj;
            virtualDisplay = vd;
            imageReader = reader;
        }
    }

    /** Start the worker thread if it is not running yet. */
    private void ensureWorkerThread() {
        synchronized (LOCK) {
            if (workerThread != null) {
                return;
            }
            workerThread = new HandlerThread("capture-worker");
            workerThread.start();
            workerHandler = new Handler(workerThread.getLooper());
        }
    }

    private Frame acquireFrame() {
        ImageReader reader;
        synchronized (LOCK) {
            if (projection == null || imageReader == null) {
                return null;
            }
            reader = imageReader;
        }
        // The pipeline may need a beat to produce its first frame;
        // poll briefly. A persistent null here is a real error the
        // caller reports honestly (not CAPTURE_UNAVAILABLE, which is
        // specifically the permission case).
        Image image = null;
        long deadline = System.currentTimeMillis() + FIRST_FRAME_WAIT_MS;
        while (image == null
                && System.currentTimeMillis() < deadline) {
            try {
                image = reader.acquireLatestImage();
            } catch (Exception e) {
                return null;
            }
            if (image == null) {
                try {
                    Thread.sleep(100);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return null;
                }
            }
        }
        if (image == null) {
            return null;
        }
        try {
            return frameFromImage(image);
        } finally {
            image.close();
        }
    }

    private Frame frameFromImage(Image image) {
        int w = image.getWidth();
        int h = image.getHeight();
        Image.Plane[] planes = image.getPlanes();
        ByteBuffer buffer = planes[0].getBuffer();
        int pixelStride = planes[0].getPixelStride();
        int rowStride = planes[0].getRowStride();
        int rowPadding = rowStride - pixelStride * w;
        Bitmap bitmap = Bitmap.createBitmap(
                w + rowPadding / pixelStride, h,
                Bitmap.Config.ARGB_8888);
        bitmap.copyPixelsFromBuffer(buffer);
        if (rowPadding > 0) {
            Bitmap cropped = Bitmap.createBitmap(bitmap, 0, 0, w, h);
            bitmap.recycle();
            bitmap = cropped;
        }
        double tsSeconds = image.getTimestamp() / 1e9;
        return new Frame(bitmap, tsSeconds);
    }

    private void resolveDisplaySize() {
        // NOTE: WindowManager.getCurrentWindowMetrics() requires a UI
        // (Activity) context and throws from a Service context, which
        // crashed the app on the grant path. getRealMetrics works from
        // any context on every API level we support (26+).
        WindowManager wm =
                (WindowManager) getSystemService(WINDOW_SERVICE);
        DisplayMetrics dm = new DisplayMetrics();
        @SuppressWarnings("deprecation")
        android.view.Display display = wm.getDefaultDisplay();
        display.getRealMetrics(dm);
        displayWidth = dm.widthPixels;
        displayHeight = dm.heightPixels;
        displayDpi = dm.densityDpi;
    }

    private void startForegroundWithType() {
        NotificationManager nm = (NotificationManager)
                getSystemService(NOTIFICATION_SERVICE);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(
                    CHANNEL_ID, "Dispatch screen capture",
                    NotificationManager.IMPORTANCE_LOW);
            nm.createNotificationChannel(ch);
        }
        Notification.Builder b = new Notification.Builder(this)
                .setContentTitle("REMOR dispatch screen capture")
                .setContentText("Capturing for the consented remote session.")
                .setSmallIcon(android.R.drawable.ic_dialog_alert);
        if (Build.VERSION.SDK_INT >= 26) {
            b.setChannelId(CHANNEL_ID);
        }
        Notification notif = b.build();
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ID, notif,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        } else {
            startForeground(NOTIF_ID, notif);
        }
    }

    private void teardown() {
        teardownPipeline();
        synchronized (LOCK) {
            if (workerThread != null) {
                workerThread.quitSafely();
                workerThread = null;
                workerHandler = null;
            }
        }
    }

    /** Release the projection pipeline, keeping the worker thread. */
    private void teardownPipeline() {
        synchronized (LOCK) {
            if (virtualDisplay != null) {
                try {
                    virtualDisplay.release();
                } catch (Exception ignored) {
                }
                virtualDisplay = null;
            }
            if (imageReader != null) {
                try {
                    imageReader.close();
                } catch (Exception ignored) {
                }
                imageReader = null;
            }
            if (projection != null) {
                try {
                    projection.stop();
                } catch (Exception ignored) {
                }
                projection = null;
            }
        }
    }
}
