package com.mounttai.giwifi;

/** 进程内共享的状态（服务写、界面读） */
public class Status {
    public static final int IDLE = 0;
    public static final int ONLINE = 1;
    public static final int CHECKING = 2;
    public static final int LOGGING = 3;
    public static final int NO_WIFI = 4;
    public static final int NO_PORTAL = 5;
    public static final int FAIL = 6;
    public static final int NOPWD = 7;
    public static final int PAUSED = 8;
    public static final int MISMATCH = 9;   // 当前 WiFi 与所选宿舍 WiFi 不符

    public static volatile int state = IDLE;
    public static volatile String detail = "";
    public static volatile long lastChange = 0;

    public static void set(int s, String d) {
        if (s == state && (d == null ? detail == null : d.equals(detail))) {
            return;
        }
        state = s;
        detail = d == null ? "" : d;
        lastChange = System.currentTimeMillis();
    }
}
