package com.mounttai.giwifi;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * 配置存取。密码用 Android Keystore 的 AES-GCM 加密后存储
 * （密钥由系统托管、不可导出；换机/清数据后密文解不开属预期行为）。
 */
public class Prefs {
    private static final String SP = "giwifi";
    private static final String ALIAS = "giwifi_pwd_key";

    public static final String DEFAULT_PORTAL = "http://192.168.100.3";
    public static final String LOGIN_PATH = "/gportal/web/login";
    public static final String AUTH_PATH = "/gportal/web/authLogin";

    private final SharedPreferences sp;

    public Prefs(Context ctx) {
        sp = ctx.getApplicationContext().getSharedPreferences(SP, Context.MODE_PRIVATE);
    }

    // ---------------- 普通字段 ----------------
    public String username() { return sp.getString("username", ""); }
    public String portal() {
        String p = sp.getString("portal", DEFAULT_PORTAL);
        return (p == null || p.trim().isEmpty()) ? DEFAULT_PORTAL : p.trim();
    }
    public boolean autoRun() { return sp.getBoolean("auto_run", false); }

    public void setUsername(String v) { sp.edit().putString("username", v == null ? "" : v.trim()).apply(); }
    public void setPortal(String v) { sp.edit().putString("portal", v == null ? "" : v.trim()).apply(); }
    public void setAutoRun(boolean v) { sp.edit().putBoolean("auto_run", v).apply(); }

    // ---------------- 密码（Keystore 加密） ----------------
    public String password() {
        String stored = sp.getString("password_enc", "");
        if (stored == null || stored.isEmpty()) return "";
        try {
            byte[] all = Base64.decode(stored, Base64.NO_WRAP);
            byte[] iv = new byte[12];
            System.arraycopy(all, 0, iv, 0, 12);
            byte[] ct = new byte[all.length - 12];
            System.arraycopy(all, 12, ct, 0, ct.length);
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.DECRYPT_MODE, getKey(), new GCMParameterSpec(128, iv));
            return new String(c.doFinal(ct), "UTF-8");
        } catch (Exception e) {
            return "";
        }
    }

    public void setPassword(String plain) {
        if (plain == null || plain.isEmpty()) {
            sp.edit().putString("password_enc", "").apply();
            return;
        }
        try {
            Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
            c.init(Cipher.ENCRYPT_MODE, getKey());
            byte[] iv = c.getIV();
            byte[] ct = c.doFinal(plain.getBytes("UTF-8"));
            byte[] all = new byte[iv.length + ct.length];
            System.arraycopy(iv, 0, all, 0, iv.length);
            System.arraycopy(ct, 0, all, iv.length, ct.length);
            sp.edit().putString("password_enc", Base64.encodeToString(all, Base64.NO_WRAP)).apply();
        } catch (Exception e) {
            AppLog.add("密码加密失败: " + e);
        }
    }

    private static SecretKey getKey() throws Exception {
        KeyStore ks = KeyStore.getInstance("AndroidKeyStore");
        ks.load(null);
        if (!ks.containsAlias(ALIAS)) {
            KeyGenerator kg = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,
                    "AndroidKeyStore");
            kg.init(new KeyGenParameterSpec.Builder(ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build());
            return kg.generateKey();
        }
        return ((KeyStore.SecretKeyEntry) ks.getEntry(ALIAS, null)).getSecretKey();
    }
}
