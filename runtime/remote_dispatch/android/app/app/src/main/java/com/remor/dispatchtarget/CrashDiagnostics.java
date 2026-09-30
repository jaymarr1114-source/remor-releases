package com.remor.dispatchtarget;

import android.content.Context;
import android.os.Build;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Crash diagnostics: the next device report arrives with its own
 * trace, never another blind one.
 *
 * <p>Installs a default uncaught-exception handler (process-wide,
 * idempotent) that writes the full stack trace to app-private
 * storage before delegating to the previous handler (which still
 * kills the process — the crash behavior is unchanged, only the
 * evidence is kept). The setup screen's "Copy diagnostics" button
 * copies the latest trace plus device/app context to the clipboard.
 */
public final class CrashDiagnostics {

    private static final String DIR = "crash_diagnostics";
    private static final AtomicBoolean INSTALLED = new AtomicBoolean(false);

    private CrashDiagnostics() {
    }

    public static void install(Context ctx) {
        if (!INSTALLED.compareAndSet(false, true)) {
            return;
        }
        final Context appCtx = ctx.getApplicationContext();
        final Thread.UncaughtExceptionHandler prev =
                Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            try {
                writeTrace(appCtx, t, e);
            } catch (Exception ignored) {
                // Diagnostics must never mask the real crash.
            }
            if (prev != null) {
                prev.uncaughtException(t, e);
            }
        });
    }

    private static void writeTrace(Context ctx, Thread t, Throwable e)
            throws Exception {
        File dir = new File(ctx.getFilesDir(), DIR);
        if (!dir.mkdirs() && !dir.isDirectory()) {
            return;
        }
        String ts = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
                .format(new Date());
        File f = new File(dir, "crash-" + ts + ".txt");
        try (PrintWriter pw = new PrintWriter(new FileWriter(f))) {
            pw.println("package: " + ctx.getPackageName());
            pw.println("time: " + ts);
            pw.println("thread: " + t.getName());
            pw.println("android: " + Build.VERSION.RELEASE
                    + " (sdk " + Build.VERSION.SDK_INT + ")");
            pw.println("device: " + Build.MANUFACTURER + " "
                    + Build.MODEL);
            pw.println("--- stack ---");
            e.printStackTrace(pw);
            Throwable cause = e.getCause();
            while (cause != null) {
                pw.println("--- caused by ---");
                cause.printStackTrace(pw);
                cause = cause.getCause();
            }
        }
    }

    /**
     * The latest trace plus device context, for the "Copy diagnostics"
     * affordance. Returns a human-readable message when no trace
     * exists yet.
     */
    public static String readLatest(Context ctx) {
        File latest = latestFile(ctx);
        StringBuilder sb = new StringBuilder();
        sb.append("package: ").append(ctx.getPackageName()).append('\n');
        sb.append("android: ").append(Build.VERSION.RELEASE)
                .append(" (sdk ").append(Build.VERSION.SDK_INT)
                .append(")\n");
        sb.append("device: ").append(Build.MANUFACTURER).append(' ')
                .append(Build.MODEL).append('\n');
        if (latest == null) {
            sb.append("no crash trace recorded yet\n");
            return sb.toString();
        }
        sb.append("trace file: ").append(latest.getName()).append('\n');
        try (BufferedReader br = new BufferedReader(
                new FileReader(latest))) {
            String line;
            while ((line = br.readLine()) != null) {
                sb.append(line).append('\n');
            }
        } catch (Exception e) {
            sb.append("unreadable: ").append(e).append('\n');
        }
        return sb.toString();
    }

    private static File latestFile(Context ctx) {
        File dir = new File(ctx.getFilesDir(), DIR);
        File[] files = dir.listFiles((d, name) ->
                name.startsWith("crash-") && name.endsWith(".txt"));
        if (files == null || files.length == 0) {
            return null;
        }
        File latest = files[0];
        for (File f : files) {
            if (f.lastModified() > latest.lastModified()) {
                latest = f;
            }
        }
        return latest;
    }
}
