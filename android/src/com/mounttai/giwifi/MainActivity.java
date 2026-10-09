package com.mounttai.giwifi;

import android.Manifest;
import android.app.Activity;
import android.app.ActivityManager;
import android.app.AlertDialog;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.net.wifi.ScanResult;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.view.View;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.CompoundButton;
import android.widget.EditText;
import android.widget.Spinner;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;

public class MainActivity extends Activity {

    private EditText edUser, edPwd, edPortal;
    private TextView statusText, statusDetail, tvLog;
    private View dot;
    private Switch swAuto;
    private CompoundButton cbShowPwd, cbHideRecents, cbHideNotif;
    private Spinner spWifi;
    private TextView tvWifiState, tvGuard;
    private ArrayAdapter<String> wifiAdapter;
    private final List<String> wifiItems = new ArrayList<>();
    private boolean spLoading = false;
    private String wifiAnyLabel = "";
    private int tickCount = 0;
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

        // ---- 密码显示切换 ----
        cbShowPwd = findViewById(R.id.cbShowPwd);
        cbShowPwd.setOnCheckedChangeListener((b, c) -> applyPwdVisibility(c));

        // ---- 隐藏后台 / 隐藏通知栏 ----
        cbHideRecents = findViewById(R.id.cbHideRecents);
        cbHideNotif = findViewById(R.id.cbHideNotif);
        cbHideRecents.setChecked(prefs.hideRecents());
        cbHideRecents.setOnCheckedChangeListener((b, c) -> {
            prefs.setHideRecents(c);
            applyRecents();
            Toast.makeText(this, c ? "已隐藏后台（不在最近任务显示）" : "已取消隐藏后台",
                    Toast.LENGTH_SHORT).show();
        });
        cbHideNotif.setChecked(prefs.hideNotif());
        cbHideNotif.setOnCheckedChangeListener((b, c) -> {
            prefs.setHideNotif(c);
            if (MonitorService.running) {
                MonitorService.start(this);   // 通知服务切换到对应通道
            }
            AppLog.add(c ? "已开启隐藏通知栏（静默通知）" : "已关闭隐藏通知栏");
        });

        // ---- 宿舍 WiFi 选择 ----
        spWifi = findViewById(R.id.spWifi);
        tvWifiState = findViewById(R.id.tvWifiState);
        tvGuard = findViewById(R.id.tvGuard);
        wifiAnyLabel = getString(R.string.wifi_any);
        wifiAdapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, wifiItems);
        wifiAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        spWifi.setAdapter(wifiAdapter);
        spWifi.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                if (spLoading) return;
                String v = wifiItems.get(position);
                boolean any = v.equals(wifiAnyLabel);
                prefs.setWifiSsid(any ? "" : v);
                AppLog.add(any ? "宿舍 WiFi 选择：不限" : "宿舍 WiFi 选择：" + v);
            }
            @Override
            public void onNothingSelected(AdapterView<?> parent) { }
        });
        spLoading = true;
        wifiItems.add(wifiAnyLabel);
        String savedSsid = prefs.wifiSsid();
        if (!savedSsid.isEmpty()) {
            wifiItems.add(savedSsid);
        }
        wifiAdapter.notifyDataSetChanged();
        spWifi.setSelection(savedSsid.isEmpty() ? 0 : 1);
        spLoading = false;
        findViewById(R.id.btnScanWifi).setOnClickListener(v -> ensureWifiPermThenScan());
        tvGuard.setOnClickListener(v ->
                showText(getString(R.string.guard_help_title), getString(R.string.guard_help_body)));

        // ---- 应用内说明（点按查看）----
        int[] helpIds = {R.id.help1, R.id.help2, R.id.help3, R.id.help4, R.id.help5};
        int[] helpTitles = {R.string.help_1, R.string.help_2, R.string.help_3, R.string.help_4, R.string.help_5};
        int[] helpBodies = {R.string.help_1_body, R.string.help_2_body, R.string.help_3_body,
                R.string.help_4_body, R.string.help_5_body};
        for (int k = 0; k < helpIds.length; k++) {
            final int idx = k;
            findViewById(helpIds[k]).setOnClickListener(v ->
                    showText(getString(helpTitles[idx]), getString(helpBodies[idx])));
        }

        applyRecents();

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
        applyRecents();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == 2) {
            if (hasWifiPerm()) {
                startScan();
            } else {
                Toast.makeText(this, "未授予权限：「宿舍 WiFi」列表不可用", Toast.LENGTH_LONG).show();
            }
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
                client.setBindNetwork(GiwifiClient.findWifiNetwork(this));
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

    // ================================================== 显示选项

    /** 密码显示切换 */
    private void applyPwdVisibility(boolean show) {
        int sel = edPwd.getSelectionEnd();
        edPwd.setInputType(show
                ? (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD)
                : (InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD));
        if (sel >= 0) {
            edPwd.setSelection(Math.min(sel, edPwd.getText().length()));
        }
    }

    /** 隐藏后台：把自己从「最近任务」列表中排除（或恢复） */
    private void applyRecents() {
        try {
            ActivityManager am = (ActivityManager) getSystemService(Context.ACTIVITY_SERVICE);
            if (am == null) return;
            for (ActivityManager.AppTask t : am.getAppTasks()) {
                t.setExcludeFromRecents(prefs.hideRecents());
            }
        } catch (Exception ignored) { }
    }

    private void showText(String title, String body) {
        try {
            new AlertDialog.Builder(this)
                    .setTitle(title)
                    .setMessage(body)
                    .setPositiveButton("知道了", null)
                    .show();
        } catch (Exception ignored) { }
    }

    // ================================================== 宿舍 WiFi 扫描 / 选择

    private boolean hasWifiPerm() {
        try {
            if (Build.VERSION.SDK_INT >= 33) {
                return checkSelfPermission(Manifest.permission.NEARBY_WIFI_DEVICES)
                        == PackageManager.PERMISSION_GRANTED
                        || checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                        == PackageManager.PERMISSION_GRANTED;
            }
            return checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Exception e) {
            return false;
        }
    }

    private boolean granted(String p) {
        try {
            return checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED;
        } catch (Exception e) {
            return false;
        }
    }

    private void ensureWifiPermThenScan() {
        List<String> need = new ArrayList<>();
        // Android 13+ 需要「附近设备」；定位权限用于旧版本与部分机型读取 SSID/扫描结果
        if (Build.VERSION.SDK_INT >= 33
                && !granted(Manifest.permission.NEARBY_WIFI_DEVICES)) {
            need.add(Manifest.permission.NEARBY_WIFI_DEVICES);
        }
        if (!granted(Manifest.permission.ACCESS_FINE_LOCATION)) {
            need.add(Manifest.permission.ACCESS_FINE_LOCATION);
        }
        if (need.isEmpty()) {
            startScan();
            return;
        }
        requestPermissions(need.toArray(new String[0]), 2);
    }

    private String readCurrentSsid() {
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

    /** 扫描附近 WiFi 并填充下拉列表（保留「不限」与已保存项） */
    private void startScan() {
        Toast.makeText(this, "扫描中…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            List<String> found = new ArrayList<>();
            String err = null;
            try {
                WifiManager wm = (WifiManager) getApplicationContext()
                        .getSystemService(Context.WIFI_SERVICE);
                if (wm == null) {
                    err = "此设备不支持 WiFi";
                } else if (!wm.isWifiEnabled()) {
                    err = "请先打开 WiFi";
                } else {
                    String cur = readCurrentSsid();
                    if (!cur.isEmpty()) found.add(cur);
                    wm.startScan();
                    try {
                        Thread.sleep(2000);   // 等扫描结果回填（本方法运行在后台线程）
                    } catch (InterruptedException ignored) { }
                    List<ScanResult> results = wm.getScanResults();
                    if (results != null) {
                        for (ScanResult r : results) {
                            if (r != null && r.SSID != null && !r.SSID.isEmpty()
                                    && !found.contains(r.SSID)) {
                                found.add(r.SSID);
                            }
                        }
                    }
                }
            } catch (Exception e) {
                err = e.getMessage();
            }
            final String ferr = err;
            final List<String> ff = found;
            ui.post(() -> {
                if (ferr != null) {
                    Toast.makeText(this, "扫描失败：" + ferr, Toast.LENGTH_LONG).show();
                    return;
                }
                String saved = prefs.wifiSsid();
                spLoading = true;
                wifiItems.clear();
                wifiItems.add(wifiAnyLabel);
                wifiItems.addAll(ff);
                if (!saved.isEmpty() && !wifiItems.contains(saved)) {
                    wifiItems.add(saved);
                }
                wifiAdapter.notifyDataSetChanged();
                spWifi.setSelection(saved.isEmpty() ? 0 : Math.max(0, wifiItems.indexOf(saved)));
                spLoading = false;
                Toast.makeText(this, ff.isEmpty()
                        ? "没扫到网络：请确认「定位」开关已打开、且已授予权限"
                        : "扫描完成：共 " + ff.size() + " 个网络", Toast.LENGTH_LONG).show();
            });
        }, "wifi-scan").start();
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
            case Status.MISMATCH:
                name = "非目标 WiFi"; color = getColor(R.color.yellow); break;
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

        // 低频刷新 WiFi / 拦截状态（约 4 秒一次，避免频繁 IPC）
        if (++tickCount >= 5) {
            tickCount = 0;
            String cur = hasWifiPerm() ? readCurrentSsid() : "";
            String saved = prefs.wifiSsid();
            String curText = cur.isEmpty()
                    ? (hasWifiPerm() ? "未连接 WiFi" : "需授权后显示")
                    : cur;
            tvWifiState.setText(saved.isEmpty()
                    ? "当前：" + curText + " ｜ 不限网络"
                    : "当前：" + curText + " ｜ 已选择：" + saved);

            boolean guard;
            try {
                guard = checkSelfPermission("android.permission.WRITE_SECURE_SETTINGS")
                        == PackageManager.PERMISSION_GRANTED;
            } catch (Exception e) {
                guard = false;
            }
            tvGuard.setText(guard
                    ? "系统登录弹窗拦截：已启用 ✓（点按查看说明）"
                    : "系统登录弹窗拦截：未启用 · 点此查看开启方法");
        }
    }
}
