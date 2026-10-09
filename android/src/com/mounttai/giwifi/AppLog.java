package com.mounttai.giwifi;

import android.content.Context;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.FileReader;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * 共享运行日志：内存环形缓冲（界面展示）+ 文件持久化（跨进程/重启保留历史）。
 * 未调用 init() 时退化为纯内存日志，不会抛错。
 */
public class AppLog {
    private static final int MAX = 300;
    private static final int MAX_FILE_BYTES = 256 * 1024;
    private static final ArrayDeque<String> LINES = new ArrayDeque<>();
    private static File file;

    /** 在进程入口（Activity / Service / 接收器）调用：加载历史日志并开启文件持久化 */
    public static synchronized void init(Context ctx) {
        if (file != null) return;
        try {
            file = new File(ctx.getFilesDir(), "giwifi-android.log");
            if (file.exists() && file.length() > MAX_FILE_BYTES) {
                trimFile();
            }
            if (file.exists()) {
                List<String> all = new ArrayList<>();
                BufferedReader r = new BufferedReader(new FileReader(file));
                String line;
                while ((line = r.readLine()) != null) all.add(line);
                r.close();
                int skip = Math.max(0, all.size() - MAX);
                for (int i = skip; i < all.size(); i++) {
                    LINES.addLast(stripDate(all.get(i)));
                }
            }
        } catch (Throwable t) {
            file = null;
        }
    }

    public static synchronized void add(String s) {
        String now = new SimpleDateFormat("HH:mm:ss", Locale.US).format(new Date());
        String line = now + "  " + s;
        LINES.addLast(line);
        while (LINES.size() > MAX) {
            LINES.removeFirst();
        }
        if (file != null) {
            try {
                String full = new SimpleDateFormat("MM-dd ", Locale.US).format(new Date()) + line;
                FileOutputStream out = new FileOutputStream(file, true);
                out.write((full + "\n").getBytes("UTF-8"));
                out.close();
            } catch (Throwable ignored) {
            }
        }
    }

    public static synchronized String dump(int maxLines) {
        int skip = Math.max(0, LINES.size() - maxLines);
        StringBuilder sb = new StringBuilder();
        int i = 0;
        for (String l : LINES) {
            if (i++ < skip) continue;
            sb.append(l).append('\n');
        }
        return sb.toString();
    }

    /** "10-09 21:45:33  文本" → "21:45:33  文本"（内存展示不带日期） */
    private static String stripDate(String line) {
        if (line.length() > 7 && line.charAt(2) == '-' && line.charAt(5) == ' ') {
            return line.substring(6);
        }
        return line;
    }

    /** 文件过大时只保留后半段 */
    private static void trimFile() {
        try {
            List<String> all = new ArrayList<>();
            BufferedReader r = new BufferedReader(new FileReader(file));
            String line;
            while ((line = r.readLine()) != null) all.add(line);
            r.close();
            StringBuilder sb = new StringBuilder();
            for (int i = all.size() / 2; i < all.size(); i++) {
                sb.append(all.get(i)).append('\n');
            }
            FileOutputStream out = new FileOutputStream(file);
            out.write(sb.toString().getBytes("UTF-8"));
            out.close();
        } catch (Throwable ignored) {
        }
    }
}
