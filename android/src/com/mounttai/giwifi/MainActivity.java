package com.mounttai.giwifi;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.view.View;
import android.widget.EditText;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

public class MainActivity extends Activity {

    private EditText edUser, edPwd, edPortal;
    private TextView statusText, statusDetail, tvLog;
    private View dot;
    private Switch swAuto;
    private Prefs prefs;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private String lastLog = "";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        AppLog.init(this);
        setContentView(R.layout.activity_main);
        prefs = new Prefs(this);

        edUser = findViewById(R.id.edUser);
        edPwd = findViewById(R.id.edPwd);
        edPortal = findViewById(R.id.edPortal);
        statusText = findViewById(R.id.statusText);
        statusDetail = findViewById(R.id.statusDetail);
        tvLog = findViewById(R.id.tvLog);
        dot = findViewById(R.id.dot);
        swAuto = findViewById(R.id.swAuto);

        // 载入配置
        edUser.setText(prefs.username());
        String pw = prefs.password();
        if (!pw.isEmpty()) {
            edPwd.setText(pw);
        }
        edPortal.setText(prefs.portal());

        // 自动登录开关（先设初值再挂监听，避免初始化误触发）
        swAuto.setChecked(prefs.autoRun());
        swAuto.setOnCheckedChangeListener((btn, checked) -> {
            prefs.setAutoRun(checked);
            if (checked) {
                MonitorService.start(this);
                AppLog.add("已开启自动登录（后台服务）");
            } else {
                MonitorService.stop(this);
                AppLog.add("已关闭自动登录");
            }
        });

        findViewById(R.id.btnSave).setOnClickListener(v -> saveConfig(true));
        findViewById(R.id.btnLoginNow).setOnClickListener(v -> loginNow());
        findViewById(R.id.btnOpenPortal).setOnClickListener(v -> openPortal());
        findViewById(R.id.btnBattery).setOnClickListener(v -> requestBattery());

        // 通知权限（Android 13+）
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }

        ui.post(ticker);
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 自愈：期望开启自动登录但服务没跑（被系统清掉）→ 重新拉起
        if (prefs.autoRun() && !MonitorService.running) {
            MonitorService.start(this);
        }
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacks(ticker);
        super.onDestroy();
    }

    // ================================================== 操作

    private void saveConfig(boolean toast) {
        prefs.setUsername(edUser.getText().toString());
        prefs.setPortal(edPortal.getText().toString());
        String pw = edPwd.getText().toString();
        if (!pw.isEmpty()) {
            prefs.setPassword(pw);      // 留空 = 不修改
        }
        AppLog.add("配置已保存");
        if (toast) {
            Toast.makeText(this, "已保存", Toast.LENGTH_SHORT).show();
        }
    }

    private void loginNow() {
        saveConfig(false);
        final String user = prefs.username();
        final String pwd = prefs.password();
        final String portal = prefs.portal();
        if (user.isEmpty() || pwd.isEmpty()) {
            Toast.makeText(this, "请先填写账号和密码", Toast.LENGTH_SHORT).show();
            return;
        }
        AppLog.add("手动触发登录检查…");
        new Thread(() -> {
            try {
                GiwifiClient client = new GiwifiClient(portal,
                        Prefs.LOGIN_PATH, Prefs.AUTH_PATH, 8);
                Status.set(Status.CHECKING, "手动检查：探测网络…");
                GiwifiClient.Probe probe = client.probeOnline();
                if (probe.online) {
                    Status.set(Status.ONLINE, "已在线（无需登录）");
                    AppLog.add("已在线，无需登录");
                    return;
                }
                if (!client.portalReachable()) {
                    Status.set(Status.NO_PORTAL, "认证网关不可达（不在校园网内？）");
                    AppLog.add("认证网关不可达");
                    return;
                }
                Status.set(Status.LOGGING, "手动登录中…");
                GiwifiClient.LoginResult lr = client.login(user, pwd);
                if (lr.ok) {
                    Status.set(Status.ONLINE, "认证成功：" + lr.message);
                    AppLog.add("✅ 手动登录成功：" + lr.message);
                } else {
                    Status.set(Status.FAIL, "登录失败：" + lr.message);
                    AppLog.add("❌ 手动登录失败：" + lr.message);
                }
            } catch (Exception e) {
                Status.set(Status.FAIL, "登录异常：" + e.getMessage());
                AppLog.add("手动登录异常：" + e.getMessage());
            }
        }, "manual-login").start();
    }

    private void openPortal() {
        saveConfig(false);
        String url = GiwifiClient.rstripSlash(prefs.portal()) + Prefs.LOGIN_PATH + "?has_reload=1";
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (Exception e) {
            Toast.makeText(this, "无法打开浏览器：" + e.getMessage(), Toast.LENGTH_SHORT).show();
        }
    }

    /** 「后台保活设置」：优先跳厂商自启动管理页（国产 ROM 必需），找不到再请求电池优化豁免 */
    private void requestBattery() {
        if (tryManufacturerAutostart()) return;
        requestBatteryOptimization();
    }

    /** 尝试打开各厂商的自启动管理页；无匹配则返回 false */
    private boolean tryManufacturerAutostart() {
        String[][] targets = {
                // 小米 / 红米（MIUI / HyperOS）
                {"com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                // 华为 / 荣耀（EMUI / MagicOS）
                {"com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.appcontrol.activity.StartupAppControlActivity"},
                // OPPO / 一加 / realme（ColorOS）
                {"com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"},
                {"com.coloros.safecenter", "com.coloros.safecenter.startupapp.StartupAppListActivity"},
                {"com.oplus.safecenter", "com.oplus.safecenter.startupapp.StartupAppListActivity"},
                // vivo / iQOO（OriginOS）
                {"com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"},
                {"com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.AddWhiteListActivity"},
                // 魅族（Flyme）
                {"com.meizu.safe", "com.meizu.safe.security.SHOW_APPSEC"},
        };
        for (String[] t : targets) {
            try {
                Intent i = new Intent();
                i.setComponent(new ComponentName(t[0], t[1]));
                i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                if (getPackageManager().resolveActivity(i, 0) == null) continue;
                startActivity(i);
                Toast.makeText(this, "请在系统设置中允许本应用「自启动 / 后台运行」",
                        Toast.LENGTH_LONG).show();
                return true;
            } catch (Exception ignored) {
            }
        }
        return false;
    }

    private void requestBatteryOptimization() {
        try {
            PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
            if (pm != null && pm.isIgnoringBatteryOptimizations(getPackageName())) {
                Toast.makeText(this, "已加入后台保活白名单 ✓", Toast.LENGTH_SHORT).show();
                return;
            }
            Intent i = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS);
            i.setData(Uri.parse("package:" + getPackageName()));
            startActivity(i);
        } catch (Exception e) {
            Toast.makeText(this, "请在 系统设置 → 电池 中把本应用设为「无限制」",
                    Toast.LENGTH_LONG).show();
        }
    }

    // ================================================== 界面刷新

    private final Runnable ticker = this::tick;

    private void tick() {
        refresh();
        ui.postDelayed(ticker, 800);
    }

    private void refresh() {
        int st = Status.state;
        String name;
        int color;
        switch (st) {
            case Status.ONLINE:
                name = "网络正常"; color = getColor(R.color.green); break;
            case Status.CHECKING:
                name = "检测中…"; color = getColor(R.color.accent); break;
            case Status.LOGGING:
                name = "正在登录…"; color = getColor(R.color.accent); break;
            case Status.NO_WIFI:
                name = "等待 WiFi"; color = getColor(R.color.yellow); break;
            case Status.NO_PORTAL:
                name = "不在校园网"; color = getColor(R.color.yellow); break;
            case Status.FAIL:
                name = "登录失败"; color = getColor(R.color.red); break;
            case Status.NOPWD:
                name = "未配置账号"; color = getColor(R.color.red); break;
            case Status.PAUSED:
                name = "已暂停"; color = getColor(R.color.text_dim); break;
            default:
                name = "未启动"; color = getColor(R.color.text_dim); break;
        }
        statusText.setText(name);
        String d = Status.detail;
        statusDetail.setText(d.isEmpty() ? " " : d);
        try {
            dot.getBackground().setTint(color);
        } catch (Exception ignored) { }

        String log = AppLog.dump(60);
        if (!log.equals(lastLog)) {
            lastLog = log;
            tvLog.setText(log.isEmpty() ? " " : log);
        }
    }
}
