package com.mounttai.giwifi;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetSocketAddress;
import java.net.Proxy;
import java.net.Socket;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * GiWiFi 门户协议客户端 —— 与电脑版 giwifi.py 行为保持一致：
 *
 *   1) GET  {portal}/gportal/web/login        拿 sign / iv 等隐藏字段（保持 DOM 顺序）
 *   2) 按 jQuery.serialize 语义序列化表单，替换其中的 name / password
 *   3) AES-128-CBC + ZeroPadding 加密 → Base64
 *   4) POST {portal}/gportal/web/authLogin?round=xxx   body: data=<密文>&iv=<iv>
 */
public class GiwifiClient {

    public static final String UA =
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                    + "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36";

    private final String portal;      // 已去掉末尾 /
    private final String loginPath;
    private final String authPath;
    private final int timeoutMs;
    private final Map<String, String> cookieMap = new LinkedHashMap<>();

    /** 全局登录互斥：避免「后台服务」和界面「立即登录」同时提交撞门户限频 */
    public static final Object GLOBAL_LOCK = new Object();

    public GiwifiClient(String portal, String loginPath, String authPath, int timeoutSec) {
        this.portal = rstripSlash(portal);
        this.loginPath = loginPath;
        this.authPath = authPath;
        this.timeoutMs = Math.max(2, timeoutSec) * 1000;
    }

    public static String rstripSlash(String s) {
        if (s == null) return "";
        s = s.trim();
        while (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        return s;
    }

    // ================================================== 结果类型

    public static class LoginResult {
        public final boolean ok;
        public final String message;
        public LoginResult(boolean ok, String message) {
            this.ok = ok;
            this.message = message == null ? "" : message;
        }
    }

    public static class Probe {
        public final boolean online;
        public final String reason;
        public Probe(boolean online, String reason) {
            this.online = online;
            this.reason = reason == null ? "" : reason;
        }
    }

    // ================================================== 底层 HTTP

    private static class Resp {
        final int code;
        final String body;
        Resp(int code, String body) { this.code = code; this.body = body == null ? "" : body; }
    }

    private static Map<String, String> headers(String... kv) {
        Map<String, String> m = new LinkedHashMap<>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put(kv[i], kv[i + 1]);
        return m;
    }

    private Resp request(String urlStr, String method, String postBody,
                         Map<String, String> extra, int timeoutOverrideMs) throws IOException {
        URL url = new URL(urlStr);
        HttpURLConnection c = (HttpURLConnection) url.openConnection(Proxy.NO_PROXY);
        int to = timeoutOverrideMs > 0 ? timeoutOverrideMs : timeoutMs;
        c.setConnectTimeout(to);
        c.setReadTimeout(to);
        c.setRequestMethod(method);
        c.setInstanceFollowRedirects(true);
        c.setUseCaches(false);
        c.setRequestProperty("User-Agent", UA);
        c.setRequestProperty("Accept-Language", "zh-CN,zh;q=0.9");
        String ck = cookieHeader();
        if (!ck.isEmpty()) c.setRequestProperty("Cookie", ck);
        if (extra != null) {
            for (Map.Entry<String, String> e : extra.entrySet()) {
                c.setRequestProperty(e.getKey(), e.getValue());
            }
        }

        if (postBody != null) {
            byte[] data = postBody.getBytes(StandardCharsets.UTF_8);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type",
                    "application/x-www-form-urlencoded; charset=UTF-8");
            c.setFixedLengthStreamingMode(data.length);
            OutputStream os = c.getOutputStream();
            os.write(data);
            os.close();
        }

        int code;
        String body = "";
        try {
            code = c.getResponseCode();
            absorbCookies(c);
            InputStream is = (code >= 400) ? c.getErrorStream() : c.getInputStream();
            body = readAll(is);
        } finally {
            c.disconnect();
        }
        return new Resp(code, body);
    }

    private static String readAll(InputStream is) throws IOException {
        if (is == null) return "";
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        is.close();
        return new String(bos.toByteArray(), StandardCharsets.UTF_8);
    }

    private void absorbCookies(HttpURLConnection c) {
        Map<String, List<String>> all = c.getHeaderFields();
        for (Map.Entry<String, List<String>> e : all.entrySet()) {
            String k = e.getKey();
            if (k == null || !k.equalsIgnoreCase("Set-Cookie")) continue;
            for (String sc : e.getValue()) {
                String kv = sc.split(";", 2)[0].trim();
                int eq = kv.indexOf('=');
                if (eq > 0) cookieMap.put(kv.substring(0, eq), kv.substring(eq + 1));
            }
        }
    }

    private String cookieHeader() {
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : cookieMap.entrySet()) {
            if (sb.length() > 0) sb.append("; ");
            sb.append(e.getKey()).append('=').append(e.getValue());
        }
        return sb.toString();
    }

    // ================================================== 表单解析

    /** 提取 #frmLogin 内的 input 字段（保持 DOM 顺序），语义与电脑版 _FormParser 一致 */
    public static List<String[]> parseLoginFields(String html) {
        List<String[]> fields = new ArrayList<>();
        int start = -1, end = -1;
        Matcher fm = Pattern.compile("<form\\b[^>]*>", Pattern.CASE_INSENSITIVE).matcher(html);
        while (fm.find()) {
            String tag = fm.group();
            String id = attr(tag, "id");
            if (id != null && id.equalsIgnoreCase("frmLogin")) {
                start = fm.end();
                int close = html.toLowerCase().indexOf("</form>", start);
                end = (close >= 0) ? close : html.length();
                break;
            }
        }
        if (start < 0) return fields;

        String body = html.substring(start, end);
        Matcher im = Pattern.compile("<input\\b[^>]*>", Pattern.CASE_INSENSITIVE).matcher(body);
        while (im.find()) {
            String tag = im.group();
            String name = attr(tag, "name");
            if (name == null || name.isEmpty()) continue;
            String type = attr(tag, "type");
            type = (type == null) ? "text" : type.toLowerCase();
            if (type.equals("button") || type.equals("submit") || type.equals("reset")
                    || type.equals("image") || type.equals("file")) continue;
            if ((type.equals("checkbox") || type.equals("radio")) && !hasAttr(tag, "checked")) continue;
            String value = attr(tag, "value");
            fields.add(new String[]{unescape(name), unescape(value == null ? "" : value)});
        }
        return fields;
    }

    private static String attr(String tag, String name) {
        Matcher m = Pattern.compile("\\b" + name + "\\s*=\\s*(\"[^\"]*\"|'[^']*'|[^\\s>]+)",
                Pattern.CASE_INSENSITIVE).matcher(tag);
        if (m.find()) {
            String v = m.group(1);
            if (v.length() >= 2 && (v.startsWith("\"") || v.startsWith("'"))) {
                v = v.substring(1, v.length() - 1);
            }
            return v;
        }
        return null;
    }

    private static boolean hasAttr(String tag, String name) {
        return Pattern.compile("\\b" + name + "\\b", Pattern.CASE_INSENSITIVE)
                .matcher(tag).find();
    }

    private static String unescape(String s) {
        return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", "\"").replace("&#39;", "'")
                .replace("&#x27;", "'").replace("&apos;", "'");
    }

    // ================================================== jQuery.serialize 语义

    private static final String SAFE =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()";

    /** 模拟 JS 的 encodeURIComponent */
    public static String jqEnc(String s) {
        StringBuilder out = new StringBuilder();
        for (byte b : s.getBytes(StandardCharsets.UTF_8)) {
            int c = b & 0xff;
            if (SAFE.indexOf(c) >= 0) {
                out.append((char) c);
            } else {
                out.append('%');
                String h = Integer.toHexString(c).toUpperCase();
                if (h.length() < 2) out.append('0');
                out.append(h);
            }
        }
        return out.toString();
    }

    // ================================================== 对外能力

    /** 在线探测：204 / 微软探测点（与电脑版 probe_online 一致） */
    public Probe probeOnline() {
        try {
            Resp r = request("http://connect.rom.miui.com/generate_204", "GET", null,
                    headers("Accept", "*/*"), 4000);
            if (r.code == 204 && r.body.isEmpty()) {
                return new Probe(true, "204 探测通过");
            }
        } catch (Exception ignored) { }
        try {
            Resp r = request("http://www.msftconnecttest.com/connecttest.txt", "GET", null,
                    headers("Accept", "*/*"), 4000);
            if (r.body.contains("Microsoft Connect Test")) {
                return new Probe(true, "微软探测点通过");
            }
        } catch (Exception ignored) { }
        return new Probe(false, "所有探测点均未通过（疑似被门户拦截 / 断网）");
    }

    /** 认证网关是否可达（纯 TCP，2 秒超时） */
    public boolean portalReachable() {
        try {
            URL u = new URL(portal);
            String host = u.getHost();
            int port = u.getPort() > 0 ? u.getPort() : 80;
            Socket s = new Socket();
            s.connect(new InetSocketAddress(host, port), 2000);
            s.close();
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    /** 完整登录流程；失败抛 IOException（网络/协议层），认证被拒返回 ok=false */
    public LoginResult login(String username, String password) throws Exception {
        synchronized (GLOBAL_LOCK) {
            return loginLocked(username, password);
        }
    }

    private LoginResult loginLocked(String username, String password) throws Exception {
        // ---- 1) 登录页，拿隐藏字段
        Resp p = request(portal + loginPath, "GET", null,
                headers("Accept", "text/html,application/xhtml+xml,*/*;q=0.8",
                        "Referer", portal + "/"), 0);
        if (p.code != 200) {
            throw new IOException("登录页返回 HTTP " + p.code + "（门户地址是否正确？）");
        }
        List<String[]> fields = parseLoginFields(p.body);
        if (fields.isEmpty()) {
            throw new IOException("没能从登录页解析出 frmLogin 表单（门户改版？）");
        }
        String sign = null, iv = null;
        for (String[] f : fields) {
            if (f[0].equals("sign")) sign = f[1];
            else if (f[0].equals("iv")) iv = f[1];
        }
        if (sign == null || sign.isEmpty() || iv == null || iv.isEmpty()) {
            throw new IOException("登录页缺少 sign / iv（可能已在线或门户改版）");
        }
        if (iv.length() != 16) {
            throw new IOException("iv 长度异常: " + iv);
        }

        // ---- 2) 替换账号密码，按 jQuery.serialize 序列化
        StringBuilder plain = new StringBuilder();
        for (String[] f : fields) {
            String k = f[0], v = f[1];
            if (k.equals("name")) v = username;
            else if (k.equals("password")) v = password;
            if (plain.length() > 0) plain.append('&');
            plain.append(jqEnc(k)).append('=').append(jqEnc(v));
        }

        // ---- 3) AES 加密
        String enc = Aes128.encrypt(plain.toString(), iv);

        // ---- 4) 提交认证
        String body = "data=" + URLEncoder.encode(enc, "UTF-8")
                + "&iv=" + URLEncoder.encode(iv, "UTF-8");
        String url = portal + authPath + "?round=" + new Random().nextInt(1001);
        Resp r = request(url, "POST", body,
                headers("X-Requested-With", "XMLHttpRequest",
                        "Origin", portal,
                        "Referer", portal + loginPath + "?has_reload=1",
                        "Accept", "application/json, text/javascript, */*; q=0.01"), 0);

        try {
            JSONObject js = new JSONObject(r.body);
            String st = String.valueOf(js.opt("status"));
            boolean ok = "1".equals(st) || "200".equals(st) || js.optBoolean("success", false);
            String info = js.optString("info", js.optString("msg", ""));
            return new LoginResult(ok, info.isEmpty() ? ("服务端 HTTP " + r.code) : info);
        } catch (Exception e) {
            return new LoginResult(false, "服务端返回 HTTP " + r.code + " 且非 JSON 响应");
        }
    }
}
