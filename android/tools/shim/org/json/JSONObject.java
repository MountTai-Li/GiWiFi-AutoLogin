package org.json;

/**
 * 桌面测试替身（仅用于让 GiwifiClient 在普通 JDK 下编译做自测）。
 * 正式 App 里用的是 Android framework 自带的 org.json。
 */
public class JSONObject {
    private final String raw;

    public JSONObject(String s) {
        this.raw = s;
    }

    public Object opt(String key) {
        return null;
    }

    public String optString(String key) {
        return "";
    }

    public String optString(String key, String fallback) {
        return fallback;
    }

    public boolean optBoolean(String key, boolean fallback) {
        return fallback;
    }
}
