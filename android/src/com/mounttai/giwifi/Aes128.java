package com.mounttai.giwifi;

import android.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.spec.IvParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/**
 * AES-128-CBC + ZeroPadding —— 与门户前端 CryptoJS 行为完全一致。
 * key 硬编码为 1234567887654321（前端 JS 里就是明文），iv 用登录页返回的值。
 */
public class Aes128 {
    public static final String KEY = "1234567887654321";

    public static String encrypt(String plain, String iv) throws Exception {
        byte[] data = plain.getBytes("UTF-8");
        // ZeroPadding：不足 16 字节补 0x00；已对齐则不补整块（与 CryptoJS 一致）
        int rem = data.length % 16;
        if (rem != 0) {
            byte[] padded = new byte[data.length + (16 - rem)];
            System.arraycopy(data, 0, padded, 0, data.length);
            data = padded;
        }
        Cipher c = Cipher.getInstance("AES/CBC/NoPadding");
        c.init(Cipher.ENCRYPT_MODE,
                new SecretKeySpec(KEY.getBytes("UTF-8"), "AES"),
                new IvParameterSpec(iv.getBytes("UTF-8")));
        return Base64.encodeToString(c.doFinal(data), Base64.NO_WRAP);
    }
}
