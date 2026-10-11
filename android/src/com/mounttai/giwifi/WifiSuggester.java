package com.mounttai.giwifi;

import android.content.Context;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiManager;
import android.net.wifi.WifiNetworkSuggestion;
import android.os.Build;

import java.util.Collections;

/**
 * 把「宿舍 WiFi」同步给系统，实现自动连接：
 *   - Android 10+ ：WifiNetworkSuggestion（系统在能连时自动连；首次系统会弹一次确认通知）
 *   - Android 9-  ：旧的 addNetwork + enableNetwork
 * 仅在 SSID / 密码发生变化时才重复执行（避免无谓的增删）。
 */
final class WifiSuggester {

    private static String lastSsid = "\u0000";
    private static String lastPass = "\u0000";

    private WifiSuggester() { }

    /** 与上次同步结果比较，有变化才真正执行 */
    static synchronized void syncIfChanged(Context ctx, String ssid, String pass) {
        String s = ssid == null ? "" : ssid;
        String p = pass == null ? "" : pass;
        if (s.equals(lastSsid) && p.equals(lastPass)) return;
        lastSsid = s;
        lastPass = p;
        sync(ctx, s, p);
    }

    static synchronized void sync(Context ctx, String ssid, String pass) {
        try {
            WifiManager wm = (WifiManager) ctx.getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) return;
            if (Build.VERSION.SDK_INT >= 29) {
                // 先移除本应用旧的推荐，再按当前配置添加
                try {
                    java.util.List<WifiNetworkSuggestion> old = wm.getNetworkSuggestions();
                    if (old != null && !old.isEmpty()) {
                        wm.removeNetworkSuggestions(old);
                    }
                } catch (Exception ignored) { }

                if (ssid.isEmpty()) return;
                if (pass.isEmpty()) {
                    // 没填密码不猜（避免把加密网络误推荐为开放网络）；只用系统已保存的连接
                    AppLog.add("未填 WiFi 密码：「" + ssid + "」将使用系统已保存的连接");
                    return;
                }

                WifiNetworkSuggestion.Builder b = new WifiNetworkSuggestion.Builder().setSsid(ssid);
                b.setWpa2Passphrase(pass);
                int r = wm.addNetworkSuggestions(Collections.singletonList(b.build()));
                if (r == WifiManager.STATUS_NETWORK_SUGGESTIONS_SUCCESS) {
                    AppLog.add("已把「" + ssid + "」推荐给系统（可自动连接）");
                } else if (r == WifiManager.STATUS_NETWORK_SUGGESTIONS_ERROR_ADD_DUPLICATE) {
                    AppLog.add("「" + ssid + "」已在系统推荐列表中");
                } else {
                    AppLog.add("WiFi 推荐失败（码 " + r + "）：请确认已授予定位/附近设备权限");
                }
            } else {
                // Android 9 及以下：旧接口直接添加并保存（定位权限由界面引导申请）
                if (ssid.isEmpty()) return;
                if (pass.isEmpty()) {
                    AppLog.add("未填 WiFi 密码：「" + ssid + "」将使用系统已保存的连接");
                    return;
                }
                WifiConfiguration cfg = new WifiConfiguration();
                cfg.SSID = "\"" + ssid + "\"";
                if (!pass.isEmpty()) {
                    cfg.preSharedKey = "\"" + pass + "\"";
                }
                int id = wm.addNetwork(cfg);
                if (id >= 0) {
                    wm.enableNetwork(id, false);
                    wm.saveConfiguration();
                    AppLog.add("已添加「" + ssid + "」到系统 WiFi 列表（可自动连接）");
                } else {
                    AppLog.add("添加 WiFi 配置失败（码 " + id + "）");
                }
            }
        } catch (Exception e) {
            AppLog.add("WiFi 推荐异常：" + e.getMessage());
        }
    }
}
