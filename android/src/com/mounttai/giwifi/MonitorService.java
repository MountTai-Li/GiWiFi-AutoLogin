package com.mounttai.giwifi;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
import android.provider.Settings;

/**
 * 前台服务：连上 WiFi 后自动检测并完成校园网认证（与电脑版监控循环同逻辑）。
 *
 * 循环（常态 10 秒，网络事件即刻唤醒 → 秒级响应）：
 *   未连 WiFi → 等待
 *   选了宿舍 WiFi 但当前不是它 → 暂停（MISMATCH）
 *   已连 WiFi → 外网探测：在线 → 什么都不做
 *              离线 → 门户可达？→ 未配置账号则提示 / 已配置则提交认证（6 秒限频）
 *
 * 额外能力：
 *   · 请求绑定 WiFi 网络，手机开流量时也不会被系统切走
 *   · 「隐藏通知栏」开启时使用静默通道（无图标、无声音）
 *   · 获得 WRITE_SECURE_SETTINGS 授权时，自动临时关闭系统门户检测（拦截登录弹窗）
 */
public class MonitorService extends Service {

    public static final String CH_ID = "giwifi_status";
    public static final String CH_ID_SILENT = "giwifi_status_silent";
    public static final int NOTIF_ID = 1;

    public static volatile boolean running = false;
    /** 由界面设置的「立即登录」请求，服务消费一次 */
    public static volatile boolean manualLogin = false;

    private volatile boolean stop = false;
    private Thread worker;
    private long lastTryAt = 0;
    private long lastCycleAt = 0;
    private Network lastWifiNet = null;
    private int lastNotifState = Integer.MIN_VALUE;
    private String lastNotifDetail = "\u0000";
    private boolean lastNotifHidden = false;

    private final Object wakeLock = new Object();
    private ConnectivityManager.NetworkCallback netCb;

    public static void start(Context ctx) {
        Intent i = new Intent(ctx, MonitorService.class);
        if (Build.VERSION.SDK_INT >= 26) {
            ctx.startForegroundService(i);
        } else {
            ctx.startService(i);
        }
    }

    public static void stop(Context ctx) {
        ctx.stopService(new Intent(ctx, MonitorService.class));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        AppLog.init(this);
        createChannels();
        registerNetListener();
        applyCaptivePortalGuard();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!running) {
            running = true;
            try {
                Notification n = buildNotification("启动中…");
                if (Build.VERSION.SDK_INT >= 34) {
                    startForeground(NOTIF_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
                } else {
                    startForeground(NOTIF_ID, n);
                }
            } catch (Exception e) {
                AppLog.add("前台服务启动失败：" + e);
            }
            worker = new Thread(this::loop, "giwifi-monitor");
            worker.start();
            AppLog.add("后台服务已启动");
        } else {
            // 已运行：设置可能被改过（隐藏通知栏等）→ 强制刷新通知并重新检查
            lastNotifState = Integer.MIN_VALUE;
            lastNotifDetail = "\u0000";
            updateNotification();
            applyCaptivePortalGuard();
            wake();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stop = true;
        running = false;
        wake();
        unregisterNetListener();
        restoreCaptivePortalGuard();
        AppLog.add("后台服务已停止");
        Status.set(Status.PAUSED, "自动登录已关闭");
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            nm.cancel(NOTIF_ID);
        } catch (Exception ignored) { }
        super.onDestroy();
    }

    // ================================================== 网络事件（秒级响应）

    private void registerNetListener() {
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) return;
            netCb = new ConnectivityManager.NetworkCallback() {
                @Override public void onAvailable(Network network) { wake(); }
                @Override public void onLost(Network network) { wake(); }
                @Override public void onCapabilitiesChanged(Network network, NetworkCapabilities caps) {
                    wake();
                }
            };
            NetworkRequest req = new NetworkRequest.Builder()
                    .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                    .build();
            cm.registerNetworkCallback(req, netCb);
        } catch (Exception e) {
            AppLog.add("网络事件监听未启用：" + e.getMessage());
        }
    }

    private void unregisterNetListener() {
        try {
            if (netCb != null) {
                ConnectivityManager cm =
                        (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
                if (cm != null) cm.unregisterNetworkCallback(netCb);
            }
        } catch (Exception ignored) { }
        netCb = null;
    }

    // ================================================== 监控循环

    private void loop() {
        while (!stop) {
            try {
                Prefs p = new Prefs(this);
                boolean manual = manualLogin;
                manualLogin = false;

                // 合并短时间内的网络事件，避免高频空转
                long now = SystemClock.elapsedRealtime();
                if (!manual && now - lastCycleAt < 2500) {
                    park(2500 - (now - lastCycleAt));
                    continue;
                }
                lastCycleAt = now;

                // 1) 找 WiFi 网络（连上即算，不要求它是系统默认网络）
                Network wifiNet = GiwifiClient.findWifiNetwork(this);
                if (wifiNet == null) {
                    Status.set(Status.NO_WIFI, "未连接 WiFi，等待中…");
                    updateNotification();
                    park(5000);
                    continue;
                }
                if (wifiNet != lastWifiNet) {   // 换了网络 → 允许立刻尝试
                    lastWifiNet = wifiNet;
                    lastTryAt = 0;
                }

                // 2) 宿舍 WiFi 过滤：选了具体网络时，只在它上面自动登录
                String want = p.wifiSsid();
                if (!want.isEmpty()) {
                    String cur = currentSsid();
                    if (cur.isEmpty()) {
                        Status.set(Status.MISMATCH,
                                "无法读取 WiFi 名称（请在弹窗中允许「附近设备/定位」权限）");
                        updateNotification();
                        park(8000);
                        continue;
                    }
                    if (!cur.equals(want)) {
                        Status.set(Status.MISMATCH,
                                "当前 WiFi「" + cur + "」与所选「" + want + "」不符，暂不登录");
                        updateNotification();
                        park(5000);
                        continue;
                    }
                }

                String user = p.username();
                String pwd = p.password();
                GiwifiClient client = new GiwifiClient(p.portal(), Prefs.LOGIN_PATH,
                        Prefs.AUTH_PATH, 8);
                client.setBindNetwork(wifiNet);   // 强制走 WiFi，开着流量也不会被切走

                // 3) 外网探测：在线就什么都不做
                GiwifiClient.Probe probe = client.probeOnline();
                if (probe.online) {
                    Status.set(Status.ONLINE, probe.reason);
                    updateNotification();
                    park(manual ? 2000 : 10000);
                    continue;
                }

                // 4) 离线：需要认证
                if (user.isEmpty() || pwd.isEmpty()) {
                    Status.set(Status.NOPWD, "未配置账号或密码，请在界面中填写并保存");
                    updateNotification();
                    park(10000);
                    continue;
                }

                long t = SystemClock.elapsedRealtime();
                if (!manual && t - lastTryAt < 6000) {   // 门户有 5 秒限频，留余量
                    park(1000);
                    continue;
                }
                lastTryAt = t;

                Status.set(Status.CHECKING, "检测到未联网，正在检查校园网…");
                updateNotification();

                if (!client.portalReachable()) {
                    Status.set(Status.NO_PORTAL, "不在校园网内（认证网关不可达），等待中…");
                    updateNotification();
                    park(8000);
                    continue;
                }

                Status.set(Status.LOGGING, "正在提交认证…");
                updateNotification();
                try {
                    GiwifiClient.LoginResult lr = client.login(user, pwd);
                    if (lr.ok) {
                        AppLog.add("✅ 登录成功：" + lr.message);
                        Status.set(Status.ONLINE, "认证成功：" + lr.message);
                        park(8000);
                    } else {
                        AppLog.add("❌ 登录失败：" + lr.message);
                        Status.set(Status.FAIL, "登录失败：" + lr.message + "（将自动重试）");
                        park(5000);
                    }
                } catch (Exception e) {
                    AppLog.add("登录异常：" + e.getMessage());
                    Status.set(Status.FAIL, "登录异常：" + e.getMessage());
                    park(5000);
                }
                updateNotification();
            } catch (Throwable t) {
                AppLog.add("监控循环异常：" + t);
                park(5000);
            }
        }
    }

    /** 可被网络事件提前唤醒的等待 */
    private void park(long ms) {
        synchronized (wakeLock) {
            try {
                wakeLock.wait(Math.max(1, ms));
            } catch (InterruptedException ignored) { }
        }
    }

    private void wake() {
        synchronized (wakeLock) {
            wakeLock.notifyAll();
        }
    }

    /** 当前 WiFi 名称（读不到返回空串：未连接 / 缺权限） */
    private String currentSsid() {
        try {
            WifiManager wm = (WifiManager) getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
            if (wm == null) return "";
            WifiInfo wi = wm.getConnectionInfo();
            if (wi == null) return "";
            String s = wi.getSSID();
            if (s == null) return "";
            if (s.length() > 2 && s.startsWith("\"") && s.endsWith("\"")) {
                s = s.substring(1, s.length() - 1);
            }
            if (s.isEmpty() || s.equals("<unknown ssid>") || s.equals("0x")) return "";
            return s;
        } catch (Exception e) {
            return "";
        }
    }

    // ================================================== 系统登录弹窗拦截（需一次性授权）

    private boolean hasSecureSettings() {
        try {
            return checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS")
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Exception e) {
            return false;
        }
    }

    /** 已授权时：临时关闭系统「门户检测」——系统不再弹出/提示登录页（关服务时自动恢复） */
    private void applyCaptivePortalGuard() {
        if (!hasSecureSettings()) return;
        try {
            ContentResolver cr = getContentResolver();
            int cur = Settings.Global.getInt(cr, "captive_portal_mode", 1);
            if (cur != 0) {
                Prefs p = new Prefs(this);
                if (p.cpmBackup() < 0) p.setCpmBackup(cur);
                Settings.Global.putInt(cr, "captive_portal_mode", 0);
                AppLog.add("已关闭系统登录弹窗（拦截生效）");
            }
        } catch (Exception e) {
            AppLog.add("关闭系统登录弹窗失败：" + e.getMessage());
        }
    }

    private void restoreCaptivePortalGuard() {
        try {
            Prefs p = new Prefs(this);
            int bak = p.cpmBackup();
            if (bak < 0) return;
            if (hasSecureSettings()) {
                Settings.Global.putInt(getContentResolver(), "captive_portal_mode", bak);
                AppLog.add("已恢复系统登录弹窗设置");
            }
            p.setCpmBackup(-1);
        } catch (Exception ignored) { }
    }

    // ================================================== 通知

    private void createChannels() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            NotificationChannel ch = new NotificationChannel(CH_ID, "运行状态",
                    NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
            NotificationChannel silent = new NotificationChannel(CH_ID_SILENT, "静默运行状态",
                    NotificationManager.IMPORTANCE_MIN);
            silent.setDescription("开启「隐藏通知栏」后使用：无状态栏图标、无声音");
            silent.setShowBadge(false);
            nm.createNotificationChannel(silent);
        }
    }

    private String stateText(int s) {
        switch (s) {
            case Status.ONLINE: return "网络正常";
            case Status.CHECKING: return "检测中…";
            case Status.LOGGING: return "正在登录…";
            case Status.NO_WIFI: return "等待 WiFi";
            case Status.MISMATCH: return "非目标 WiFi";
            case Status.NO_PORTAL: return "不在校园网";
            case Status.FAIL: return "登录失败（重试中）";
            case Status.NOPWD: return "未配置账号";
            case Status.PAUSED: return "已暂停";
            default: return "运行中";
        }
    }

    private Notification buildNotification(String text) {
        Intent i = new Intent(this, MainActivity.class);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        PendingIntent pi = PendingIntent.getActivity(this, 0, i, flags);
        boolean hidden = new Prefs(this).hideNotif();
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(this, hidden ? CH_ID_SILENT : CH_ID);
        } else {
            b = new Notification.Builder(this);
        }
        return b.setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("GiWiFi 自动登录")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .setPriority(hidden ? Notification.PRIORITY_MIN : Notification.PRIORITY_LOW)
                .setOnlyAlertOnce(true)
                .build();
    }

    private void updateNotification() {
        int st = Status.state;
        String d = Status.detail;
        boolean hid = new Prefs(this).hideNotif();
        if (st == lastNotifState && d.equals(lastNotifDetail) && hid == lastNotifHidden) return;
        lastNotifState = st;
        lastNotifDetail = d;
        lastNotifHidden = hid;
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            String text = stateText(st);
            if (!d.isEmpty()) text = text + " · " + d;
            nm.notify(NOTIF_ID, buildNotification(text));
        } catch (Exception ignored) { }
    }
}
