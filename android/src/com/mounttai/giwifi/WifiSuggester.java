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
 * 仅在 SSID / 密码 / 安全性 发生变化时才重复执行（避免无谓的增删）。
 *
 * 安全性参数 sec：-1=未知，0=无密码（开放网络），1=有密码。
 * 无密码网络不需要用户填写任何密码，直接按开放网络推荐；有密码网络才需要密码。
 */
final class WifiSuggester {

    private static String lastSsid = "\u0000";
    private static String lastPass = "\u0000";
    private static int lastSec = Integer.MIN_VALUE;

    private WifiSuggester() { }

    /** 与上次同步结果比较，有变化才真正执行 */
    static synchronized void syncIfChanged(Context ctx, String ssid, String pass, int sec) {
        String s = ssid == null ? "" : ssid;
        String p = pass == null ? "" : pass;
        if (s.equals(lastSsid) && p.equals(lastPass) && sec == lastSec) return;
        lastSsid = s;
        lastPass = p;
        lastSec = sec;
        sync(ctx, s, p, sec);
    }

    static synchronized void sync(Context ctx, String ssid, String pass, int sec) {
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

                WifiNetworkSuggestion.Builder b = new WifiNetworkSuggestion.Builder().setSsid(ssid);
                boolean openNet;
                if (!pass.isEmpty()) {
                    b.setWpa2Passphrase(pass);
                    openNet = false;
                } else if (sec == 0) {
                    // 无密码开放网络：不设置任何凭据
                    openNet = true;
                } else {
                    // 未填密码且无法确认是开放网络 → 不猜测，交给系统已保存的连接
                    AppLog.add("未填 WiFi 密码且未识别为开放网络：「" + ssid + "」将使用系统已保存的连接");
                    return;
                }

                int r = wm.addNetworkSuggestions(Collections.singletonList(b.build()));
                if (r == WifiManager.STATUS_NETWORK_SUGGESTIONS_SUCCESS) {
                    AppLog.add("已把「" + ssid + "」" + (openNet ? "（无密码网络）" : "")
                            + "推荐给系统（可自动连接）");
                } else if (r == WifiManager.STATUS_NETWORK_SUGGESTIONS_ERROR_ADD_DUPLICATE) {
                    AppLog.add("「" + ssid + "」已在系统推荐列表中");
                } else {
                    AppLog.add("WiFi 推荐失败（码 " + r + "）：请确认已授予定位/附近设备权限");
                }
            } else {
                // Android 9 及以下：旧接口直接添加并保存（定位权限由界面引导申请）
                if (ssid.isEmpty()) return;
                WifiConfiguration cfg = new WifiConfiguration();
                cfg.SSID = "\"" + ssid + "\"";
                boolean openNet;
                if (!pass.isEmpty()) {
                    cfg.preSharedKey = "\"" + pass + "\"";
                    openNet = false;
                } else if (sec == 0) {
                    cfg.allowedKeyManagement.set(WifiConfiguration.KeyMgmt.NONE);
                    openNet = true;
                } else {
                    AppLog.add("未填 WiFi 密码且未识别为开放网络：「" + ssid + "」将使用系统已保存的连接");
                    return;
                }
                int id = wm.addNetwork(cfg);
                if (id >= 0) {
                    wm.enableNetwork(id, false);
                    wm.saveConfiguration();
                    AppLog.add("已添加「" + ssid + "」" + (openNet ? "（无密码网络）" : "")
                            + "到系统 WiFi 列表（可自动连接）");
                } else {
                    AppLog.add("添加 WiFi 配置失败（码 " + id + "）");
                }
            }
        } catch (Exception e) {
            AppLog.add("WiFi 推荐异常：" + e.getMessage());
        }
    }
}
