package com.mounttai.giwifi;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** 开机自启：配置里开启了「自动登录」则拉起后台服务 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction())) {
            return;
        }
        try {
            AppLog.init(context);
            Prefs p = new Prefs(context);
            if (!p.autoRun()) return;
            Intent svc = new Intent(context, MonitorService.class);
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(svc);
            } else {
                context.startService(svc);
            }
            AppLog.add("开机自启：已拉起后台服务");
        } catch (Exception e) {
            AppLog.add("开机自启失败：" + e);
        }
    }
}
