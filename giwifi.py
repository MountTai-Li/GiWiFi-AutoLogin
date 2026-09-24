# -*- coding: utf-8 -*-
"""
giwifi.py —— GiWiFi 校园网认证核心模块（零第三方依赖）

适配网关:  http://192.168.100.3/gportal/
登录流程:
    1) GET  /gportal/web/login            拿到 sign / iv / nasName / userIp 等隐藏字段
    2) 把 #frmLogin 的表单按 DOM 顺序序列化成 query string
       （追加 name=账号 & password=密码）
    3) 用 AES-128-CBC(ZeroPadding, key=1234567887654321, iv=页面返回) 加密 → Base64
    4) POST /gportal/web/authLogin?round=xxx   body: data=<密文>&iv=<iv>
"""
import gzip
import io
import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.parse          # 轻量：只依赖 re，解析 portal 地址用
# ⚠ urllib.request / urllib.error / http.cookiejar **刻意不在这里导入**：
#   它们会连带拉入 ssl + 整个 email 包（实测 +4.8 MB 工作集），
#   而外网探测（每 10 秒一次）和取 SSID 都用不到。
#   改在 GiwifiPortal.__init__ 里按需导入 —— 只有真正要登录时才加载。
from html.parser import HTMLParser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aes128  # noqa: E402

VERSION = "1.11.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# encodeURIComponent 的安全字符集（jQuery.serialize 用的就是它）
_URI_SAFE = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")


def jq_enc(text) -> str:
    """模拟 JS 的 encodeURIComponent"""
    out = []
    for ch in str(text):
        if ch in _URI_SAFE:
            out.append(ch)
        else:
            for b in ch.encode("utf-8"):
                out.append("%%%02X" % b)
    return "".join(out)


# ------------------------------------------------------------------ 表单解析
class _FormParser(HTMLParser):
    """提取 <form id="..."> 内的 input/select/textarea，保持 DOM 顺序"""

    def __init__(self, form_id):
        super().__init__(convert_charrefs=True)
        self.form_id = form_id.lower()
        self.in_form = 0
        self.fields = []          # [(name, value)]
        self.buttons = set()

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            fid = a.get("id", "").lower()
            if self.in_form:                       # 嵌套 form，计数
                self.in_form += 1
            elif fid == self.form_id:
                self.in_form = 1
            return
        if not self.in_form:
            return
        if tag in ("input", "select", "textarea"):
            name = a.get("name")
            if not name:
                return
            itype = a.get("type", "text").lower()
            if itype in ("button", "submit", "reset", "image", "file"):
                return
            if itype in ("checkbox", "radio") and "checked" not in a:
                return
            self.fields.append((name, a.get("value", "")))

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form -= 1


def parse_login_fields(html: str, form_id="frmLogin"):
    p = _FormParser(form_id)
    p.feed(html)
    return p.fields


def get_attr(html: str, el_id: str, attr="value"):
    """粗粒度取某个 id 元素的属性值——仅在 HTMLParser 拿不到时兜底"""
    m = re.search(r'id="%s"[^>]*\b%s="([^"]*)"' % (re.escape(el_id), attr), html)
    return m.group(1) if m else None


# ------------------------------------------------------------------ 门户客户端
class GiwifiPortal:
    def __init__(self, cfg, log=None):
        self.cfg = cfg
        self.portal = cfg["portal"].rstrip("/")
        self.login_path = cfg.get("login_path", "/gportal/web/login")
        self.auth_path = cfg.get("auth_path", "/gportal/web/authLogin")
        self.timeout = float(cfg.get("timeout", 8))
        self.log = log or (lambda *a, **k: None)
        # 按需导入（见文件头说明）：只有走到「真的要用 HTTP 客户端」时才付出这 4.8 MB
        import http.cookiejar
        import urllib.error
        import urllib.request
        self.jar = http.cookiejar.CookieJar()
        # 显式禁用系统代理：校园门户只能直连
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "zh-CN,zh;q=0.9"),
        ]

    # -------------------------------------------------- 底层请求
    def _request(self, url, data=None, headers=None, method=None):
        """
        返回 (status_code, body_bytes)。
        注意：HTTP 4xx/5xx 也当作正常结果返回（门户常用非 200 状态码携带业务错误 JSON，
        所以必须把错误响应的 body 也读出来，不能直接抛异常）。
        """
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=headers or {})
        if body is not None:
            req.add_header("Content-Type",
                           "application/x-www-form-urlencoded; charset=UTF-8")
        try:
            resp = self.opener.open(req, timeout=self.timeout)
            status, raw, enc = resp.status, resp.read(), resp.headers.get("Content-Encoding")
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read()
            enc = e.headers.get("Content-Encoding") if e.headers else None
        if enc == "gzip":
            raw = gzip.decompress(raw)
        return status, raw

    # -------------------------------------------------- 1) 拿登录页
    def fetch_login_page(self):
        url = self.portal + self.login_path
        status, raw = self._request(url, headers={
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer": self.portal + "/",
        })
        if status != 200:
            raise RuntimeError("登录页返回 HTTP %s（门户地址是否正确？）" % status)
        html = raw.decode("utf-8", "replace")
        fields = parse_login_fields(html, "frmLogin")
        if not fields:
            raise RuntimeError("没能从登录页解析出 frmLogin 表单，门户改版？")
        d = dict(fields)
        if not d.get("sign") or not d.get("iv"):
            raise RuntimeError("登录页缺少 sign / iv，可能已经在线或门户改版")
        return html, fields

    # -------------------------------------------------- 2) 登录
    def login(self, username, password):
        """返回 (成功?, 服务器消息, 原始响应文本)"""
        html, fields = self.fetch_login_page()

        # 用真实账号密码替换表单里的 name / password
        new = []
        for k, v in fields:
            if k == "name":
                v = username
            elif k == "password":
                v = password
            new.append((k, v))
        iv = dict(new).get("iv", "")
        if len(iv) != 16:
            raise RuntimeError("iv 长度异常: %r" % iv)

        # jQuery.serialize 语义
        plain = "&".join("%s=%s" % (jq_enc(k), jq_enc(v)) for k, v in new)
        enc = aes128.encrypt_cbc_zeropad(plain, "1234567887654321", iv)

        url = "%s%s?round=%d" % (self.portal, self.auth_path, random.randint(0, 1000))
        status, raw = self._request(url, data={"data": enc, "iv": iv}, headers={
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.portal,
            "Referer": self.portal + self.login_path + "?has_reload=1",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        text = raw.decode("utf-8", "replace")
        try:
            js = json.loads(text)
        except Exception:
            return False, "服务端返回 HTTP %s 且非 JSON 响应" % status, text
        ok = str(js.get("status")) in ("1", "200") or js.get("success") is True
        return ok, str(js.get("info") or js.get("msg") or text[:200]), text


# ------------------------------------------------------------------ 在线探测
def _http_get_light(url, timeout=4.0):
    """
    极简 HTTP/1.0 GET —— 只给「探测外网是否被门户劫持」用。

    为什么不用 urllib：urllib.request 会连带拉入 ssl / email / http.cookiejar
    整整一套（实测 +4.8 MB 工作集）。而这里只需要发一个最朴素的 GET
    并读回状态码 + 少量响应体，用 socket 十几行就够了，
    这样**在线状态下 urllib 根本不会被加载**。

    返回 (状态码, 响应体前若干字节)。非 http:// 或出错时返回 None，由调用方跳过。
    """
    u = urllib.parse.urlsplit(url)
    if u.scheme.lower() != "http" or not u.hostname:
        return None                       # https 等交给调用方跳过（探测点都是 http）
    port = u.port or 80
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    req = ("GET %s HTTP/1.0\r\n"
           "Host: %s\r\n"
           "User-Agent: %s\r\n"
           "Cache-Control: no-cache\r\n"
           "Connection: close\r\n\r\n" % (path, u.hostname, UA)).encode(
               "ascii", "ignore")
    s = socket.create_connection((u.hostname, port), timeout)
    try:
        s.sendall(req)
        buf = b""
        while len(buf) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            buf += chunk
    finally:
        try:
            s.close()
        except Exception:
            pass
    head, _, body = buf.partition(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0].split()
    status = int(first[1]) if len(first) > 1 and first[1].isdigit() else 0
    # 只按第一段状态行判断，响应体也只留前 256 字节
    return status, body[:256]


def probe_online(cfg, opener=None):
    """
    返回 (在线?, 原因)。用 204 探测点判断是否被门户劫持。

    走 `_http_get_light`（纯 socket），**不加载 urllib** —— 这个函数每 10 秒
    就会被调一次，是 urllib 全家桶最大的常驻用户。`opener` 参数保留只为兼容旧调用。
    """
    probes = cfg.get("probe_urls") or [
        "http://connect.rom.miui.com/generate_204",
        "http://www.msftconnecttest.com/connecttest.txt",
    ]
    timeout = float(cfg.get("probe_timeout", 4))
    for url in probes:
        try:
            r = _http_get_light(url, timeout)
        except Exception:
            continue
        if not r:
            continue
        status, body = r
        if "generate_204" in url:
            # 正常：204 且响应体为空；被门户劫持时会是 200 + 登录页
            if status == 204 and not body:
                return True, "204 探测通过 (%s)" % url
        elif "connecttest" in url:
            if b"Microsoft Connect Test" in body:
                return True, "微软探测点通过 (%s)" % url
        else:
            if status == 200 and body:
                return True, "探测通过 (%s)" % url
    return False, "所有探测点均未通过（疑似被门户拦截 / 断网）"


# ------------------------------------------------------------------ 常用工具
# 默认缓存时长 —— 它的真正作用是**限制位置信息的访问频率**。
# 查「当前连接」走 WlanQueryInterface(wlan_intf_opcode_current_connection)，
# 而 Windows 把「读取 Wi-Fi 信息」视为一次**位置访问**（Wi-Fi 可反推地理位置）。
#
# 历史教训：这里原来是 3.0 秒，而监控循环每 10 秒跑一轮 → 缓存必然失效 →
# **每 10 秒就访问一次位置信息**，在「设置 → 隐私和安全性 → 位置信息 → 最近活动」
# 里就能看到本程序反复出现。
#
# 现在默认 60 秒，且只用于「限定了 WiFi、需要判断是否连对」这一条路径；
# 默认配置（wifi_ssid 留空 = 不限制）根本不会走到这里 → 稳态零位置访问。
_SSID_CACHE = {"t": 0.0, "v": ""}
SSID_TTL_DEFAULT = 60.0
_SSID_TTL = SSID_TTL_DEFAULT


def get_current_ssid(ttl=None, force=False):
    """
    取当前连接的 WiFi 名称，取不到返回 ''。

    实现顺序（重要）：
      1) **优先走原生 WLAN API**（`wlanapi.current_ssid`）—— 纯函数调用，
         **不启动任何子进程**，也就不会在任务管理器里冒出「网络命令外壳」(netsh.exe)。
      2) 原生 API 不可用时，退回 `netsh wlan show interfaces` 解析文本。
         ⚠ 退回路径必须用 `winutil.run_hidden` 静默执行：在无控制台的 pythonw 进程里，
           普通 subprocess 会让 Windows 给 netsh **新建一个控制台窗口**，屏幕上闪黑框。
    """
    if ttl is None:
        ttl = _SSID_TTL
    now = time.time()
    if not force and (now - _SSID_CACHE["t"]) < ttl:
        return _SSID_CACHE["v"]

    val = None
    # ---- 1) 原生 API ----
    try:
        import wlanapi
        val = wlanapi.current_ssid()
    except Exception:
        val = None

    # ---- 2) 兜底：netsh ----
    if val is None:
        val = ""
        try:
            import winutil
            out = winutil.run_hidden(["netsh", "wlan", "show", "interfaces"], timeout=8)
            txt = out.stdout or ""
            m = re.search(r"^\s*SSID\s*:\s*(.+?)\s*$", txt, re.M)
            if m:
                val = m.group(1).strip()
        except Exception:
            val = ""

    _SSID_CACHE["t"] = now
    _SSID_CACHE["v"] = val
    return val


# ------------------------------------------------------------- 扫描 WiFi 列表
_NET_CACHE = {"t": 0.0, "v": None}
_NET_TTL = 10.0


def list_wifi_networks(ttl=None, force=False):
    """
    扫描当前位置可见的 WiFi，供界面下拉选择。

    返回按「已连接 → 信号强弱」排序的列表：
        [{"ssid": "GiWiFi-xxx-5G", "signal": 92, "connected": True}, ...]

    ⚠ 关键：优先用原生 WLAN API（wlanapi.scan_networks）。
      笔记本连着 AP 时，Windows 只会做「定向扫描」，`netsh wlan show networks`
      往往只返回当前连接的那一个网络（实测本机 netsh 给 1 个、原生 API 给 5 个）。
      只有主动调用 WlanScan() 才能拿到完整列表。
      netsh 解析仅作为原生 API 不可用时的兜底。

    结果带 10 秒缓存，避免界面上疯狂触发扫描。
    """
    now = time.time()
    if ttl is None:
        ttl = _NET_TTL
    if not force and _NET_CACHE["v"] is not None and (now - _NET_CACHE["t"]) < ttl:
        return [dict(x) for x in _NET_CACHE["v"]]

    raw = []
    native_ok = False
    try:
        import wlanapi
        raw = wlanapi.scan_networks()
        native_ok = bool(raw)
    except Exception:
        raw = []
    if not raw:
        raw = _scan_wifi_via_netsh()

    # 原生扫描结果自带「已连接」标记（dwFlags 的 FLAG_CONNECTED），**不需要**
    # 再单独查一次当前 SSID —— 那次查询本身也会触发一次位置访问。
    # 只有走 netsh 兜底（拿不到标记）时才补查一次。
    connected_flags = {r["ssid"] for r in raw if r.get("connected")}
    if not connected_flags and not native_ok:
        cur = get_current_ssid(ttl=0)
        if cur:
            connected_flags = {cur}

    result = []
    for r in raw:
        ssid = (r.get("ssid") or "").strip()
        if not ssid:
            continue
        result.append({"ssid": ssid,
                       "signal": r.get("signal"),
                       "connected": ssid in connected_flags})

    def sort_key(d):
        sig = d["signal"] if d["signal"] is not None else -1
        return (0 if d["connected"] else 1, -sig, d["ssid"].lower())

    result.sort(key=sort_key)
    _NET_CACHE["t"] = now
    _NET_CACHE["v"] = [dict(x) for x in result]
    return result


_PROFILE_CACHE = {"t": 0.0, "v": None}
_PROFILE_TTL = 30.0


def list_wlan_profiles(ttl=None, force=False):
    """
    列出本机已保存的无线配置文件名称。

    为什么需要：`netsh wlan connect` 只能按**配置文件**连接，
    而配置文件只在你之前成功连过这个网络之后才会存在。
    所以「自动连接某个 WiFi」的前提是：本机已经有它的配置文件。
    （新网络的配置文件包含密码，程序没法凭空造出来。）
    """
    now = time.time()
    if ttl is None:
        ttl = _PROFILE_TTL
    if not force and _PROFILE_CACHE["v"] is not None and (now - _PROFILE_CACHE["t"]) < ttl:
        return list(_PROFILE_CACHE["v"])
    names = []
    try:
        import winutil
        proc = winutil.run_hidden(["netsh", "wlan", "show", "profiles"], timeout=12)
        txt = proc.stdout or ""
        # 中文：「所有用户配置文件 : xxx」  英文：「All User Profile : xxx」
        for pat in (r"^\s*所有用户配置文件\s*:\s*(.+?)\s*$",
                    r"^\s*All User Profile\s*:\s*(.+?)\s*$"):
            names += re.findall(pat, txt, re.M | re.I)
    except Exception:
        return []
    # 去重且保持顺序
    seen, out = set(), []
    for n in names:
        n = n.strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    _PROFILE_CACHE["t"] = now
    _PROFILE_CACHE["v"] = list(out)
    return out


def find_profile_for(ssid):
    """
    找一个能连上目标 SSID 的配置文件。完全匹配优先，其次容忍 2.4G/5G 后缀差异。
    找不到返回 ""。
    """
    want = (ssid or "").strip()
    if not want:
        return ""
    profs = list_wlan_profiles()
    for p in profs:
        if p == want:
            return p
    strip = lambda s: re.sub(r"[-_\s]?(?:5g|2\.4g|24g|5ghz|2\.4ghz)$", "", s, flags=re.I).lower()
    for p in profs:
        if p.startswith(want) or want.startswith(p) or strip(p) == strip(want):
            return p
    return ""


def _scan_wifi_via_netsh():
    """兜底方案：解析 netsh 输出。返回 [{"ssid","signal","connected"}]"""
    try:
        import winutil
        proc = winutil.run_hidden(["netsh", "wlan", "show", "networks", "mode=bssid"],
                                  timeout=15)
        txt = proc.stdout or ""
    except Exception:
        txt = ""

    signal, order, cur = {}, [], None
    for line in txt.splitlines():
        m = re.match(r"^\s*SSID\s+\d+\s*:\s*(.*?)\s*$", line)
        if m:
            cur = m.group(1).strip()
            if cur and cur not in signal:
                signal[cur] = None
                order.append(cur)
            continue
        # 中文是「信号」，英文是 Signal，两者都兼容
        m = re.match(r"^\s*(?:信号|Signal)\s*:\s*(\d{1,3})\s*%", line, re.I)
        if m and cur:
            v = int(m.group(1))
            if signal.get(cur) is None or v > signal[cur]:
                signal[cur] = v
    return [{"ssid": s, "signal": signal[s], "connected": False} for s in order]


def gateway_host(cfg):
    return urllib.parse.urlparse(cfg["portal"]).hostname


# ------------------------------------------------------------------ 配置读写
def app_dir():
    """程序所在目录（兼容 PyInstaller 打包后的 exe）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


DEFAULT_CONFIG = {
    "username": "",                 # 校园网账号（通常是手机号）
    "password_enc": "",             # DPAPI 加密后的密码（界面保存时自动生成）
    "portal": "http://192.168.100.3",
    "login_path": "/gportal/web/login",
    "auth_path": "/gportal/web/authLogin",
    "check_interval": 10,           # 在线时的检测间隔（秒）
    "timeout": 8,                   # 单次请求超时
    "probe_timeout": 4,             # 连通性探测超时
    # 开机后「等 Wi-Fi 就绪」的**上限**秒数：一旦无线电打开就立刻继续，
    # 不做固定等待（改造前是盲等满 5 秒才开始开 Wi-Fi，实测白费 11 秒）。
    "startup_delay": 5,
    # --- 认证失败后的重试策略 ---
    "retry_interval": 5,            # 失败后每隔几秒重试一次，直到成功
    "alert_after_fails": 1,         # 连续失败几次弹提示告知用户；0=不弹，1=首次就弹
    "wifi_ssid": "",                # 绑定宿舍 WiFi 名称；留空表示不限制
    "ensure_wifi_on": True,         # 启动后自动打开 Wi-Fi 开关（无线电）
    "auto_connect_wifi": True,      # 没连上指定 WiFi 时自动连过去
    # --- 位置信息节流（查当前 SSID / 扫描 WiFi 都会被记成一次位置访问）---
    # 「不限制 WiFi」时程序**完全不查** SSID，这两项只在下面两种情况生效：
    # 0 = **不做周期性检查**（默认）：只有检测到掉线、真的需要判断
    #     「连的是不是目标 WiFi」时才查一次 —— 稳态下零位置访问。
    #     想让程序「一直主动保持在指定 WiFi 上」，设成 60 之类的正数即可恢复。
    "ssid_query_interval": 0,
    "ssid_display_interval": 120,   # 界面显示「当前 WiFi」的刷新间隔（窗口隐藏时不刷新）
    "wifi_profile": "",             # 自动连接用的无线配置文件名称
    # --- 开机自启（后台 --silent）时是否也显示系统托盘图标 ---
    # False（默认）= 完全静默，托盘区不留任何图标（旧行为）；
    # True         = 开机后立刻出现一个托盘图标，右键可「打开主窗口 /
    #                立即登录 / 暂停监控 / 退出程序」，双击可打开窗口。
    # 注意：打开控制面板后托盘图标由窗口接管，不会出现两个图标。
    "tray_on_autostart": False,
    "log_file": "giwifi.log",
    "probe_urls": [
        "http://connect.rom.miui.com/generate_204",
        "http://www.msftconnecttest.com/connecttest.txt",
    ],
}

CONFIG_NAME = "config.json"


def config_path():
    return os.path.join(app_dir(), CONFIG_NAME)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception:
            pass
    return cfg


def save_config(cfg):
    """
    把配置写回磁盘，**立即、可靠地持久化**。

    用「临时文件 + 原子替换 + fsync」而不是直接覆盖：
    * 直接覆盖时若中途断电/崩溃，会留下半截 JSON → 下次启动配置全丢；
    * `os.replace` 在 Windows 上也是原子操作，替换前后文件始终完整；
    * `fsync` 强制刷到磁盘，避免还在系统缓存里就被断电。
    """
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    # 目录项也刷一次，确保「文件已改名」这件事本身落盘
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        pass
    return path


def portal_reachable(cfg, timeout=2.0):
    host = gateway_host(cfg)
    port = urllib.parse.urlparse(cfg["portal"]).port or 80
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except Exception:
        return False
