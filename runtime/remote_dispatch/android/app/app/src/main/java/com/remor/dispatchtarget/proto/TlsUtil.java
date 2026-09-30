package com.remor.dispatchtarget.proto;

import java.io.FileInputStream;
import java.io.InputStream;
import java.nio.file.Path;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;

import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLServerSocketFactory;

/**
 * TLS for the target listener (javax.net.ssl), mirroring
 * {@code runtime/remote_dispatch/tls.py}.
 *
 * <p>The target presents a self-signed certificate from a keystore; the
 * controller pins the SHA-256 fingerprint of the DER certificate
 * post-handshake ({@code tls.wrap_client}). {@link #fingerprint} uses
 * the exact same semantics: SHA-256 over {@code cert.getEncoded()}
 * (the DER encoding), lowercase hex — identical to
 * {@code openssl x509 -outform DER | sha256}.
 */
public final class TlsUtil {
    private TlsUtil() {}

    /** Load a PKCS12 keystore holding the target's cert + private key. */
    public static KeyStore loadKeyStore(Path keystorePath, char[] password)
            throws Exception {
        KeyStore ks = KeyStore.getInstance("PKCS12");
        try (InputStream in = new FileInputStream(keystorePath.toFile())) {
            ks.load(in, password);
        }
        return ks;
    }

    /** TLS server context from the keystore (TLSv1.2+). */
    public static SSLContext serverContext(KeyStore ks, char[] password)
            throws Exception {
        KeyManagerFactory kmf = KeyManagerFactory.getInstance(
                KeyManagerFactory.getDefaultAlgorithm());
        kmf.init(ks, password);
        SSLContext ctx = SSLContext.getInstance("TLS");
        ctx.init(kmf.getKeyManagers(), null, null);
        return ctx;
    }

    /** Bound, listening TLS server socket. */
    public static SSLServerSocket serverSocket(SSLContext ctx, String host,
                                              int port) throws Exception {
        SSLServerSocketFactory fac = ctx.getServerSocketFactory();
        SSLServerSocket ss = (SSLServerSocket) fac.createServerSocket();
        ss.setReuseAddress(true);
        ss.bind(new java.net.InetSocketAddress(host, port));
        return ss;
    }

    /** SHA-256 fingerprint (lowercase hex) of the DER certificate. */
    public static String fingerprint(X509Certificate cert) throws Exception {
        MessageDigest sha = MessageDigest.getInstance("SHA-256");
        byte[] digest = sha.digest(cert.getEncoded());
        StringBuilder sb = new StringBuilder(64);
        for (byte b : digest) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    /** Fingerprint of the first certificate in the keystore. */
    public static String keystoreFingerprint(KeyStore ks) throws Exception {
        String alias = ks.aliases().nextElement();
        Certificate cert = ks.getCertificate(alias);
        if (!(cert instanceof X509Certificate)) {
            throw new IllegalStateException(
                    "keystore holds no X509Certificate");
        }
        return fingerprint((X509Certificate) cert);
    }
}
