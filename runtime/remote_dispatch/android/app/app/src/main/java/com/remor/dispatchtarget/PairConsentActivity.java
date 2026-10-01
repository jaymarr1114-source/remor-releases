package com.remor.dispatchtarget;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.remor.dispatchtarget.proto.PairingServer;

/**
 * Tablet-side tap-to-pair consent screen (RD-EASYPAIR-1).
 *
 * <p>Shows who is asking to pair and the 6-digit comparison code. The
 * tablet owner taps ALLOW or DENY. ALLOW lets the phone complete the
 * pairing (the phone must additionally confirm the code matches what it
 * received over the pinned TLS channel). Nothing here ever approves
 * silently: no approval leaves this screen except the owner's tap.
 *
 * <p>Launched by {@link RemoteListener} when a pair_request arrives.
 */
public class PairConsentActivity extends Activity {

    public static final String EXTRA_PAIRING_ID = "pairing_id";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        Intent in = getIntent();
        final String pairingId = in.getStringExtra(EXTRA_PAIRING_ID);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);
        scroll.addView(root);

        final PairingServer.PendingPairing pairing =
                RemoteListener.pendingPairing(pairingId);

        TextView title = new TextView(this);
        title.setText("Pairing request");
        title.setTextSize(20);
        root.addView(title);

        if (pairing == null) {
            // Expired, denied, or already settled: say so honestly.
            TextView gone = new TextView(this);
            gone.setText("This pairing request is no longer pending.\n"
                    + "It may have expired, been denied, or already been"
                    + " completed. Ask the phone to try again.");
            gone.setPadding(0, pad / 2, 0, pad / 2);
            root.addView(gone);
            Button close = new Button(this);
            close.setText("Close");
            close.setOnClickListener(v -> finish());
            root.addView(close);
            setContentView(scroll);
            return;
        }

        TextView who = new TextView(this);
        who.setText("\"" + pairing.phoneName + "\" wants to pair with"
                + " this tablet.\n\nOnly allow this if you started the"
                + " pairing from that phone yourself.");
        who.setPadding(0, pad / 2, 0, pad / 2);
        root.addView(who);

        TextView codeLabel = new TextView(this);
        codeLabel.setText("Check that this code matches the one on"
                + " the phone:");
        root.addView(codeLabel);

        TextView code = new TextView(this);
        code.setText(pairing.displayCode());
        code.setTextSize(36);
        code.setPadding(0, pad / 2, 0, pad);
        root.addView(code);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);

        Button deny = new Button(this);
        deny.setText("DENY");
        deny.setOnClickListener(v -> {
            RemoteListener.denyPairing(pairingId);
            Toast.makeText(this, "Pairing denied", Toast.LENGTH_SHORT)
                    .show();
            finish();
        });
        row.addView(deny);

        Button allow = new Button(this);
        allow.setText("ALLOW");
        allow.setOnClickListener(v -> {
            boolean ok = RemoteListener.approvePairing(pairingId);
            Toast.makeText(this,
                    ok ? "Pairing allowed -- confirm the code on the phone"
                            : "Pairing expired -- ask the phone to try again",
                    Toast.LENGTH_LONG).show();
            finish();
        });
        row.addView(allow);

        root.addView(row);
        setContentView(scroll);
    }
}
