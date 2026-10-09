package com.mounttai.giwifi;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;

/**
 * 前台服务：连上 WiFi 后自动检测并完成校园网认证（与电脑版监控循环同逻辑）。
 *
 * 循环（每 10 秒）：
 *   未连 WiFi → 等待
 *   已连 WiFi → 外网探测：在线 → 什么都不做
 *              离线 → 门户可达？→ 未配置账号则提示 / 已配置则提交认证（6 秒限频）
 */
public class MonitorService extends Service {

    public static final String CH_ID = "giwifi_status";
    public static final int NOTIF_ID = 1;

    public static volatile boolean running = false;
    /** 由界面设置的「立即登录」请求，服务消费一次 */
    public static volatile boolean manualLogin = false;

    private volatile boolean stop = false;
    private Thread worker;
    private long lastTryAt = 0;
    private int lastNotifState = -1;
    private String lastNotifDetail = "";

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
        createChannel();
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
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stop = true;
        running = false;
        AppLog.add("后台服务已停止");
        Status.set(Status.PAUSED, "自动登录已关闭");
        updateNotification();
        super.onDestroy();
    }

    // ================================================== 监控循环

    private void loop() {
        while (!stop) {
            try {
                Prefs p = new Prefs(this);
                boolean manual = manualLogin;
                manualLogin = false;

                // 1) 是否连上 WiFi（不需要定位权限的判据）
                if (!isWifiConnected()) {
                    Status.set(Status.NO_WIFI, "未连接 WiFi，等待中…");
                    updateNotification();
                    sleep(5000);
                    continue;
                }

                String user = p.username();
                String pwd = p.password();
                GiwifiClient client = new GiwifiClient(p.portal(), Prefs.LOGIN_PATH,
                        Prefs.AUTH_PATH, 8);

                // 2) 外网探测：在线就什么都不做
                GiwifiClient.Probe probe = client.probeOnline();
                if (probe.online) {
                    Status.set(Status.ONLINE, probe.reason);
                    updateNotification();
                    sleep(manual ? 2000 : 10000);
                    continue;
                }

                // 3) 离线：需要认证
                if (user.isEmpty() || pwd.isEmpty()) {
                    Status.set(Status.NOPWD, "未配置账号或密码，请在界面中填写并保存");
                    updateNotification();
                    sleep(10000);
                    continue;
                }

                long now = SystemClock.elapsedRealtime();
                if (!manual && now - lastTryAt < 6000) {   // 门户有 5 秒限频，留余量
                    sleep(1000);
                    continue;
                }
                lastTryAt = now;

                Status.set(Status.CHECKING, "检测到未联网，正在检查校园网…");
                updateNotification();

                if (!client.portalReachable()) {
                    Status.set(Status.NO_PORTAL, "不在校园网内（认证网关不可达），等待中…");
                    updateNotification();
                    sleep(10000);
                    continue;
                }

                Status.set(Status.LOGGING, "正在提交认证…");
                updateNotification();
                try {
                    GiwifiClient.LoginResult lr = client.login(user, pwd);
                    if (lr.ok) {
                        AppLog.add("✅ 登录成功：" + lr.message);
                        Status.set(Status.ONLINE, "认证成功：" + lr.message);
                        sleep(8000);
                    } else {
                        AppLog.add("❌ 登录失败：" + lr.message);
                        Status.set(Status.FAIL, "登录失败：" + lr.message + "（将自动重试）");
                        sleep(5000);
                    }
                } catch (Exception e) {
                    AppLog.add("登录异常：" + e.getMessage());
                    Status.set(Status.FAIL, "登录异常：" + e.getMessage());
                    sleep(5000);
                }
                updateNotification();
            } catch (Throwable t) {
                AppLog.add("监控循环异常：" + t);
                sleep(5000);
            }
        }
    }

    private void sleep(long ms) {
        long end = SystemClock.elapsedRealtime() + ms;
        while (!stop && SystemClock.elapsedRealtime() < end) {
            try {
                Thread.sleep(Math.min(200, end - SystemClock.elapsedRealtime()));
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    private boolean isWifiConnected() {
        try {
            ConnectivityManager cm =
                    (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
            if (cm == null) return false;
            Network n = cm.getActiveNetwork();
            if (n == null) return false;
            NetworkCapabilities caps = cm.getNetworkCapabilities(n);
            return caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI);
        } catch (Exception e) {
            return false;
        }
    }

    // ================================================== 通知

    private void createChannel() {
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            NotificationChannel ch = new NotificationChannel(CH_ID, "运行状态",
                    NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            nm.createNotificationChannel(ch);
        }
    }

    private String stateText(int s) {
        switch (s) {
            case Status.ONLINE: return "网络正常";
            case Status.CHECKING: return "检测中…";
            case Status.LOGGING: return "正在登录…";
            case Status.NO_WIFI: return "等待 WiFi";
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
        Notification.Builder b;
        if (Build.VERSION.SDK_INT >= 26) {
            b = new Notification.Builder(this, CH_ID);
        } else {
            b = new Notification.Builder(this);
        }
        return b.setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("GiWiFi 自动登录")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(pi)
                .build();
    }

    private void updateNotification() {
        int st = Status.state;
        String d = Status.detail;
        if (st == lastNotifState && d.equals(lastNotifDetail)) return;
        lastNotifState = st;
        lastNotifDetail = d;
        try {
            NotificationManager nm =
                    (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
            String text = stateText(st);
            if (!d.isEmpty()) text = text + " · " + d;
            nm.notify(NOTIF_ID, buildNotification(text));
        } catch (Exception ignored) { }
    }
}
