package android.util;

/**
 * 桌面测试替身：让 GiwifiClient/Aes128 源码能在普通 JDK 上编译运行做自测。
 * 只用 java.util.Base64 实现 Android Base64 用到的两个方法。
 */
public class Base64 {
    public static final int NO_WRAP = 2;
    public static final int DEFAULT = 0;

    public static String encodeToString(byte[] input, int flags) {
        String s = java.util.Base64.getEncoder().encodeToString(input);
        if (flags == NO_WRAP) {
            return s;
        }
        return s;
    }

    public static byte[] decode(String str, int flags) {
        return java.util.Base64.getMimeDecoder().decode(str);
    }
}
