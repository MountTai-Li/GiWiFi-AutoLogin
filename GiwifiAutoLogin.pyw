# -*- coding: utf-8 -*-
"""
GiwifiAutoLogin.pyw —— GiWiFi 校园网自动登录助手（主程序）

用法：
    pythonw GiwifiAutoLogin.pyw              打开控制面板（图形界面）
    pythonw GiwifiAutoLogin.pyw --silent     纯后台监控，无界面（开机自启用这个）
    python  GiwifiAutoLogin.pyw --once       只跑一次检测+登录，打印结果后退出
    python  GiwifiAutoLogin.pyw --diag       打印环境诊断信息
    python  GiwifiAutoLogin.pyw --test-login 用配置里的账号做一次真实登录测试

特性：
    · 后台常驻监测，外网不通时自动完成 GiWiFi 门户认证
    · 密码用 Windows DPAPI 加密存储，别人拷走 config.json 也解不开
    · 登录限频 + 失败退避，避免触发门户「操作过于频繁」限制
    · 单实例互斥，重复启动不会出现两个进程抢着登录
"""
import os
import queue
import sys
import threading
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import giwifi      # noqa: E402
import monitor     # noqa: E402
import secure_store  # noqa: E402
import winutil     # noqa: E402

APP_TITLE = "GiWiFi 自动登录助手 v" + giwifi.VERSION

# 配色（深色）
BG      = "#1b1d23"
CARD    = "#252831"
CARD2   = "#2d313c"
FG      = "#e8eaf0"
FG_DIM  = "#9aa3b2"
ACCENT  = "#4ea1ff"
GREEN   = "#3ddc84"
YELLOW  = "#ffb02e"
RED     = "#ff5c5c"
BORDER  = "#343846"

STATE_COLOR = {
    monitor.S_ONLINE:   GREEN,
    monitor.S_LOGIN:    ACCENT,
    monitor.S_CHECK:    ACCENT,
    monitor.S_WAIT:     YELLOW,
    monitor.S_FAIL:     RED,
    monitor.S_NO_PORTAL: YELLOW,
    monitor.S_NO_WIFI:  YELLOW,
    monitor.S_PAUSED:   FG_DIM,
    monitor.S_NOPWD:    RED,
}

# 宿舍 WiFi 下拉框的「不限制」选项
WIFI_ANY = "不限制（推荐·在任何 WiFi 下都可尝试登录）"
WIFI_EMPTY = "（未扫描到 WiFi，点右侧「刷新」重试）"
# 扫描（WlanScan）会被 Windows 记为一次位置访问，所以除了必须由用户主动触发，
# 这里再兜一层节流：15 秒内重复展开下拉框不会真的再扫一次。
WIFI_SCAN_GAP = 15.0        # 自动重扫的最小间隔（秒）


# --------------------------------------------------------------- 单实例互斥
_mutex_handle = None


def acquire_single_instance(tag="GiwifiAutoLogin"):
    global _mutex_handle
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        _mutex_handle = k32.CreateMutexW(None, False, "Global\\" + tag)
        return ctypes.get_last_error() != 183          # 183 = ERROR_ALREADY_EXISTS
    except Exception:
        return True


def ensure_silent():
    """
    保证程序「不带命令窗口」运行。

    两种兜底手段：
      1) 如果是被 python.exe（控制台版解释器）拉起来的，且同目录存在 pythonw.exe，
         就用 pythonw.exe 静默重启自己，当前进程立刻退出 —— 黑框一闪而过甚至看不到。
      2) 否则如果自己确实挂着一个控制台窗口，直接把它隐藏掉。

    返回 True 表示「已经重启，调用方应马上退出」。
    用 --keep-console 可以关掉这套逻辑（方便在终端里看输出）。
    """
    args_lower = [a.lower() for a in sys.argv[1:]]
    if not winutil.IS_WIN or getattr(sys, "frozen", False) or "--keep-console" in args_lower:
        return False

    if os.path.basename(sys.executable).lower() == "python.exe":
        pw = winutil.pythonw_path()
        if pw:
            try:
                args = [a for a in sys.argv[1:] if a.lower() != "--keep-console"]
                # 也带上 -S，省 18 MB 工作集
                winutil.popen_detached([pw, "-S", os.path.abspath(__file__)] + args,
                                       cwd=BASE)
                return True
            except Exception:
                pass
    # 兜底：把自己的控制台窗口收起来
    winutil.hide_own_console()
    return False


# --------------------------------------------------------------- 低内存启动
def ensure_low_memory():
    """
    用 `-S` 重新拉起自己，跳过 `site` 模块 —— **省 18 MB 工作集**。

    为什么：`site` 会在启动时扫描 sys.path 下所有 `.pth` 文件、处理
    site-packages 目录，本机实测光这一步就要多占 ~18 MB。
    而本项目是**纯标准库**程序（json/re/socket/urllib/ctypes/tkinter 都在标准库），
    `-S` 之后功能完全不受影响（已验证：模块全可导入、pylnk3 可用、AES 自检通过）。

    返回 True 表示「已经用 -S 重启，调用方应立即退出」。
    调试时可用 `--keep-console` 或环境变量 `GIWIFI_NO_RELAUNCH=1` 跳过。
    """
    if getattr(sys, "frozen", False) or not winutil.IS_WIN:
        return False
    if sys.flags.no_site:
        return False                              # 已经带 -S 了，什么都不用做
    args_lower = [a.lower() for a in sys.argv[1:]]
    if "--keep-console" in args_lower or "--no-lowmem" in args_lower:
        return False
    if os.environ.get("GIWIFI_NO_RELAUNCH"):
        return False

    exe = sys.executable
    if os.path.basename(exe).lower() == "python.exe":
        exe = winutil.pythonw_path() or exe     # 顺便换成无控制台解释器
    try:
        DETACHED = 0x00000008
        NO_WINDOW = 0x08000000
        winutil.popen_detached([exe, "-S", os.path.abspath(__file__)] + sys.argv[1:],
                               cwd=BASE)
        return True
    except Exception:
        return False                              # 重启失败就照常继续，不能因此起不来


# --------------------------------------------------------------- 退出信号
# 界面点「退出程序」时留一个标记文件，让开机自启的后台实例也一起退出，
# 免得出现「以为退干净了，其实还有后台在跑」。
QUIT_FLAG = "giwifi.quit"


def _quit_flag_path():
    return os.path.join(BASE, QUIT_FLAG)


def quit_flag_set():
    try:
        return os.path.exists(_quit_flag_path())
    except Exception:
        return False


def clear_quit_flag():
    try:
        p = _quit_flag_path()
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def request_quit_all():
    try:
        with open(_quit_flag_path(), "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass


def _silent_log(msg):
    """
    后台模式（pythonw）**没有控制台，sys.stdout 是 None，print 会被静默丢弃**，
    所以关键信息要显式追加到日志文件，否则出了问题完全查不到。
    """
    try:
        cfg = giwifi.load_config()
        p = os.path.join(BASE, cfg.get("log_file") or "giwifi.log")
        with open(p, "a", encoding="utf-8") as f:
            f.write("%s [INFO] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# --------------------------------------------------------------- 无界面模式
def run_silent():
    if ensure_silent():
        return 0                       # 已用 pythonw 静默重启
    if not acquire_single_instance():
        print("已有一个实例在运行，退出。")
        return 0
    clear_quit_flag()                  # 清掉可能残留的陈旧标记
    cfg = giwifi.load_config()
    mon = monitor.Monitor(
        cfg,
        on_log=lambda lv, m: print("[%s] %s" % (lv, m), flush=True),
        on_state=lambda s, d: None,
        # 后台模式没有 tkinter，用原生消息框；放在独立线程里弹，
        # 绝不能让模态框把监控线程（重试 / 退出联动）一起卡住
        on_alert=lambda msg: (winutil.show_alert_async("GiWiFi 认证失败", msg),
                              _silent_log("认证失败已弹窗提示用户"))[0])
    mon.start()
    print("GiWiFi 自动登录已在后台运行（Ctrl+C 退出）")
    try:
        while True:
            time.sleep(1)
            if quit_flag_set():
                clear_quit_flag()
                _silent_log("收到控制面板发来的退出请求，正在退出")
                break
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        try:
            mon.stop()
        except Exception:
            pass
    return 0


def run_once():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = giwifi.load_config()
    print("=" * 56)
    print("GiWiFi 自动登录 —— 单次运行")
    print("=" * 56)
    ssid = giwifi.get_current_ssid()
    print("当前 WiFi      :", ssid or "(未连接)")
    print("门户地址       :", cfg["portal"])
    print("门户可达       :", "是" if giwifi.portal_reachable(cfg) else "否")
    online, why = giwifi.probe_online(cfg)
    print("外网连通       :", "在线" if online else "离线", "|", why)
    if online:
        print("\n网络正常，无需登录。")
        return 0
    acct = cfg.get("username", "").strip()
    pwd = secure_store.get_password(cfg)
    if not acct or not pwd:
        print("\n未配置账号或密码，请先打开控制面板填写。")
        return 2
    print("\n正在尝试登录（账号: %s）..." % acct)
    portal = giwifi.GiwifiPortal(cfg, log=lambda lv, m: print("   ", m))
    ok, info, raw = portal.login(acct, pwd)
    print("登录结果       :", "✅ 成功" if ok else "❌ 失败")
    print("服务器信息     :", info)
    print("原始响应       :", raw[:400])
    if ok:
        ok2, why2 = giwifi.probe_online(cfg)
        print("复测连通性     :", "已联网" if ok2 else "仍未联网", "|", why2)
    return 0 if ok else 1


def run_diag():
    import autostart          # 诊断才需要
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = giwifi.load_config()
    print("=" * 56)
    print("环境诊断")
    print("=" * 56)
    print("程序目录       :", BASE)
    print("配置文件       :", giwifi.config_path())
    print("解释器         :", sys.executable)
    print("DPAPI 加密可用 :", secure_store.available())
    print("pylnk3 可用    :", autostart._HAS_PYLNK3)
    print("当前 WiFi      :", giwifi.get_current_ssid() or "(未连接)")
    try:
        import wlanapi
        print("Wi-Fi 可用     :", wlanapi.is_wifi_available())
        _r, _m = wlanapi.ensure_radio_on()
        print("Wi-Fi 开关     :", ("开着" if (_r and not _m) else (_m or "已打开")))
    except Exception as e:
        print("Wi-Fi 开关     : 查询失败", type(e).__name__, e)
    print("自动开 Wi-Fi   :", cfg.get("ensure_wifi_on", True))
    print("自动连 WiFi    :", cfg.get("auto_connect_wifi", True),
          "| 配置文件:", cfg.get("wifi_profile") or "(运行时自动解析)")
    print("门户地址       :", cfg["portal"], "->",
          "可达" if giwifi.portal_reachable(cfg) else "不可达")
    print("账号已配置     :", bool(cfg.get("username")))
    print("密码已配置     :", bool(secure_store.get_password(cfg)))
    print("开机自启       :", "已开启" if autostart.is_enabled() else "未开启")
    print("桌面目录       :", autostart.desktop_dir())
    print()
    print("-- 位置信息相关 --")
    print("    说明：Windows 把「读 Wi-Fi 信息」视为一次位置访问。")
    print("    会触发的调用：查当前 SSID / 扫描附近 WiFi / netsh wlan connect")
    print("    不会触发    ：读 Wi-Fi 开关状态、枚举网卡、netsh wlan show profiles")
    _want = (cfg.get("wifi_ssid") or "").strip()
    print("    WiFi 限定      :", _want or "不限制（监控循环完全不查 SSID，零位置访问）")
    print("    SSID 查询间隔  :", cfg.get("ssid_query_interval", 60), "秒（仅限定 WiFi 时生效）")
    print("    界面显示间隔   :", cfg.get("ssid_display_interval", 120), "秒（窗口隐藏时不刷新）")
    print("    启动自动扫描   : 已关闭（下拉列表只在展开/点刷新时扫）")
    print()
    print("-- 启动与重试 --")
    print("    Wi-Fi 就绪上限 :", cfg.get("startup_delay", 5), "秒（一就绪立刻继续，不盲等）")
    print("    失败重试间隔   :", cfg.get("retry_interval", 5), "秒（固定间隔，直到成功）")
    print("    失败提示       :", "连续失败 %s 次后弹窗" % cfg.get("alert_after_fails", 1)
          if cfg.get("alert_after_fails") else "不弹窗（仅写日志）")
    print("    正常检测间隔   :", cfg.get("check_interval", 10), "秒")
    print("\n-- 外网探测点 --")
    for u in cfg.get("probe_urls", []):
        print("   ", u)
    print("\n-- 附近可见 WiFi（按 已连接/信号 排序）--")
    try:
        nets = giwifi.list_wifi_networks(force=True)
        if not nets:
            print("    未扫描到（WLAN 是否已开启？）")
        for n in nets:
            print("    %-28s %-6s %s" % (
                n["ssid"],
                ("%d%%" % n["signal"]) if n["signal"] is not None else "未知",
                "← 已连接" if n["connected"] else ""))
    except Exception as e:
        print("    扫描失败:", type(e).__name__, e)
    print("\n-- 门户登录页 --")
    try:
        p = giwifi.GiwifiPortal(cfg)
        _, fields = p.fetch_login_page()
        for k, v in fields:
            print("    %-10s = %s" % (k, (v[:60] + "…") if len(v) > 60 else v))
    except Exception as e:
        print("    解析失败:", type(e).__name__, e)
    return 0


# --------------------------------------------------------------- 图形界面
def run_gui():
    # 先确保自己是无控制台进程，再创建窗口
    if ensure_silent():
        return 0

    # 界面本身也做单实例（后台 --silent 实例不受影响，两者靠文件锁限频共存）
    if not acquire_single_instance("GiwifiAutoLogin.Gui"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, "GiWiFi 自动登录助手已经在运行了，请到任务栏查看。",
                APP_TITLE, 0x40)
        except Exception:
            pass
        return 0

    import tkinter as tk

    root = tk.Tk()
    root.configure(bg=BG)

    # 自适应屏幕：高分屏（如 175% 缩放）下逻辑分辨率会小很多，必须夹住尺寸
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    w = min(680, max(520, sw - 60))
    h = min(790, max(430, sh - 60))
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 4)))
    root.minsize(520, 430)

    App(root, tk)
    root.mainloop()
    return 0


class App:
    def __init__(self, root, tk):
        from tkinter import messagebox
        self.tk = tk
        self.mb = messagebox
        self.root = root
        self.cfg = giwifi.load_config()
        self.q = queue.Queue()
        self.log_lines = []

        # 宿舍 WiFi 下拉框的状态
        self._wifi_map = {WIFI_ANY: ""}     # 下拉标签 -> 实际 SSID
        self._wifi_scanning = False
        self._wifi_scan_ts = 0.0

        self._hidden = False
        self._tray_status = "启动中"

        self._build_ui()
        self._load_into_form()

        self.mon = monitor.Monitor(
            self.cfg,
            on_log=lambda lv, m: self.q.put(("log", lv, m)),
            on_state=lambda s, d: self.q.put(("state", s, d)),
            # 认证失败要弹提示 —— 这里只投递到队列，由主线程弹窗
            # （托盘线程 / 监控线程直接弹模态框会把它们卡住）
            on_alert=lambda msg: self.q.put(("alert", msg, None)))
        self.mon.start()

        # 系统托盘：关掉窗口之后靠它继续常驻
        self.tray = self._setup_tray()

        self.root.after(120, self._drain)
        self.root.after(1000, self._tick)
        # ⚠ 启动时**不再自动扫描** WiFi 列表。
        #   扫描（WlanScan + WlanGetAvailableNetworkList）会被 Windows 记为一次
        #   位置访问。下拉列表改为**只在用户主动展开下拉框或点「刷新」时**才扫。
        #   下面这一步只刷新「当前 WiFi」显示（窗口可见 = 必要时机），不是扫描。
        self.mon.display_ssid = True
        self.mon.request_ssid_refresh()

    # ---------------------------------------------------------- 构建界面
    def _build_ui(self):
        from tkinter import ttk
        self.ttk = ttk
        tk = self.tk
        self.root.title(APP_TITLE)
        import autostart          # 只有画界面时才需要（拿图标路径）
        ic = autostart.icon_path() or (sys.executable if getattr(sys, "frozen", False)
                                       else None)
        if ic:
            try:
                self.root.iconbitmap(ic)
            except Exception:
                pass

        # 用 grid 布局：日志行 weight=1 + minsize，保证它永远不会被挤没
        root = self.root
        root.columnconfigure(0, weight=1)
        for r, w in ((0, 0), (1, 0), (2, 0), (3, 0), (4, 0), (5, 1)):
            root.rowconfigure(r, weight=w)
        root.rowconfigure(5, minsize=124)
        PX = 14

        # --- 标题
        head = tk.Frame(root, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=PX, pady=(8, 2))
        tk.Label(head, text="GiWiFi 自动登录助手", bg=BG, fg=FG,
                 font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        tk.Label(head, text="v" + giwifi.VERSION, bg=BG, fg=FG_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=(8, 0), pady=(5, 0))

        # --- 状态卡
        card = tk.Frame(root, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.grid(row=1, column=0, sticky="ew", padx=PX, pady=(0, 6))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=14, pady=8)
        self.cv = tk.Canvas(inner, width=36, height=36, bg=CARD,
                            highlightthickness=0)
        self.cv.pack(side="left")
        self._dot = self.cv.create_oval(6, 6, 30, 30, fill=FG_DIM, outline="")
        txt = tk.Frame(inner, bg=CARD)
        txt.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self.lb_state = tk.Label(txt, text="正在启动…", bg=CARD, fg=FG,
                                 font=("Microsoft YaHei UI", 13, "bold"),
                                 anchor="w")
        self.lb_state.pack(fill="x")
        self.lb_detail = tk.Label(txt, text="", bg=CARD, fg=FG_DIM, anchor="w",
                                  font=("Microsoft YaHei UI", 9), wraplength=440,
                                  justify="left")
        self.lb_detail.pack(fill="x", pady=(2, 0))

        # --- 运行信息
        info = tk.Frame(root, bg=CARD2)
        info.grid(row=2, column=0, sticky="ew", padx=PX, pady=(0, 6))
        self.info_labels = {}
        for i, (key, name) in enumerate([("ssid", "当前 WiFi"), ("portal", "认证门户"),
                                         ("stat", "运行统计"), ("last", "最近登录")]):
            tk.Label(info, text=name, bg=CARD2, fg=FG_DIM, width=9, anchor="w",
                     font=("Microsoft YaHei UI", 9)
                     ).grid(row=i, column=0, sticky="w", padx=(14, 4), pady=(4 if i == 0 else 1,
                                                                           4 if i == 3 else 1))
            lb = tk.Label(info, text="--", bg=CARD2, fg=FG, anchor="w",
                          font=("Microsoft YaHei UI", 9))
            lb.grid(row=i, column=1, sticky="w", padx=(0, 14),
                    pady=(4 if i == 0 else 1, 4 if i == 3 else 1))
            info.columnconfigure(1, weight=1)
            self.info_labels[key] = lb

        # --- 账号设置
        cfgf = tk.LabelFrame(root, text=" 账号设置 ", bg=BG, fg=FG_DIM,
                             bd=1, relief="solid", font=("Microsoft YaHei UI", 9))
        cfgf.grid(row=3, column=0, sticky="ew", padx=PX, pady=(0, 4))
        g = tk.Frame(cfgf, bg=BG)
        g.pack(fill="x", padx=10, pady=6)
        g.columnconfigure(1, weight=1)

        def field(row, label, key, show=None):
            tk.Label(g, text=label, bg=BG, fg=FG_DIM, anchor="w", width=11,
                     font=("Microsoft YaHei UI", 9)
                     ).grid(row=row, column=0, sticky="w", pady=2)
            e = tk.Entry(g, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                         font=("Microsoft YaHei UI", 10), show=show,
                         highlightthickness=1, highlightbackground=BORDER,
                         highlightcolor=ACCENT)
            e.grid(row=row, column=1, sticky="ew", pady=2, ipady=2)
            self.__dict__["e_" + key] = e
            return e

        field(0, "校园网账号", "username")
        field(1, "密码", "password", show="●")
        field(3, "检测间隔(秒)", "interval")
        self.e_interval.delete(0, "end")
        self.e_interval.insert(0, str(self.cfg.get("check_interval", 10)))

        # --- 宿舍 WiFi：自动扫描 + 下拉选择（不用手输）
        tk.Label(g, text="宿舍 WiFi", bg=BG, fg=FG_DIM, anchor="w", width=11,
                 font=("Microsoft YaHei UI", 9)
                 ).grid(row=2, column=0, sticky="w", pady=2)

        st = ttk.Style()
        try:
            st.theme_use("clam")            # clam 才允许自定义配色
        except Exception:
            pass
        st.configure("Dark.TCombobox",
                     fieldbackground=CARD, background=CARD, foreground=FG,
                     arrowcolor=FG, bordercolor=BORDER, lightcolor=BORDER,
                     darkcolor=BORDER, selectbackground=CARD, selectforeground=FG,
                     insertcolor=FG, padding=2)
        # ttk 下拉列表是内部 Tk Listbox，只能用 option 数据库改色
        root.option_add("*TCombobox*Listbox.background", CARD)
        root.option_add("*TCombobox*Listbox.foreground", FG)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        root.option_add("*TCombobox*Listbox.font", "{Microsoft YaHei UI} 10")

        self.cb_wifi = ttk.Combobox(g, style="Dark.TCombobox", height=12,
                                    font=("Microsoft YaHei UI", 10),
                                    values=[WIFI_ANY], state="normal")
        self.cb_wifi.grid(row=2, column=1, sticky="ew", pady=2, ipady=2)
        self.cb_wifi.set(WIFI_ANY)
        # 点开下拉时顺便静默刷新一遍（有节流，不会刷屏）
        self.cb_wifi.configure(postcommand=lambda: self._request_wifi_scan())
        # 选择变化时同步「记住并自动连接」的可用状态
        self.cb_wifi.bind("<<ComboboxSelected>>", lambda e: self._sync_remember_state())
        self.cb_wifi.bind("<KeyRelease>", lambda e: self._sync_remember_state())

        self.btn_wifi = tk.Button(
            g, text="刷新", command=lambda: self._request_wifi_scan(force=True),
            bg=CARD, fg=FG, activebackground=ACCENT, activeforeground="#fff",
            relief="flat", font=("Microsoft YaHei UI", 8), bd=0, cursor="hand2",
            padx=8, pady=2)
        self.btn_wifi.grid(row=2, column=2, sticky="w", padx=(6, 0))

        # 「记住并自动连接」：勾上后，一旦没连到这个 WiFi 就自动去连
        self.var_remember = tk.IntVar(value=0)
        self.chk_remember = tk.Checkbutton(
            g, text="记住并自动连接", variable=self.var_remember,
            command=self._sync_remember_state, bg=BG, fg=FG_DIM,
            activebackground=BG, activeforeground=FG, selectcolor=CARD,
            font=("Microsoft YaHei UI", 8), bd=0, highlightthickness=0,
            cursor="hand2")
        self.chk_remember.grid(row=2, column=3, sticky="w", padx=(8, 0))

        # 密码明文永不回显；用一个复选框临时切换可见性，方便核对有没有打错
        self.var_showpwd = tk.IntVar(value=0)
        self.chk_show = tk.Checkbutton(
            g, text="显示", variable=self.var_showpwd, command=self._toggle_pwd_show,
            bg=BG, fg=FG_DIM, activebackground=BG, activeforeground=FG,
            selectcolor=CARD, font=("Microsoft YaHei UI", 8), bd=0,
            highlightthickness=0, cursor="hand2")
        self.chk_show.grid(row=1, column=2, sticky="w", padx=(6, 0))

        self.lb_pwd_state = tk.Label(
            g, text="", bg=BG, fg=FG_DIM, font=("Microsoft YaHei UI", 8), anchor="w")
        self.lb_pwd_state.grid(row=4, column=1, columnspan=3, sticky="w", pady=(1, 0))

        # --- 按钮
        btns = tk.Frame(root, bg=BG)
        btns.grid(row=4, column=0, sticky="ew", padx=PX, pady=(0, 2))

        def mkbtn(text, cmd, bg=CARD, fg=FG, col=0, row=0):
            b = tk.Button(btns, text=text, command=cmd, bg=bg, fg=fg,
                          activebackground=ACCENT, activeforeground="#fff",
                          relief="flat", font=("Microsoft YaHei UI", 9),
                          padx=10, pady=4, cursor="hand2", bd=0)
            b.grid(row=row, column=col, padx=(0, 8), pady=2, sticky="ew")
            btns.columnconfigure(col, weight=1)
            return b

        self.btn_save = mkbtn("保存并应用", self.on_save, bg=ACCENT, fg="#fff",
                              col=0, row=0)
        self.btn_login = mkbtn("立即登录", self.on_login_now, col=1, row=0)
        self.btn_pause = mkbtn("暂停监控", self.on_toggle_pause, col=2, row=0)
        mkbtn("打开日志", self.on_open_log, col=0, row=1)
        self.btn_auto = mkbtn("", self.on_toggle_autostart, col=1, row=1)
        mkbtn("退出程序", self.on_quit, col=2, row=1)

        # 保存结果就地提示（不弹窗打扰）：让「已落盘、重启也在」这件事看得见
        self.lb_save_hint = tk.Label(
            btns, text="配置改动会立即写入本机，关机重启后依然生效",
            bg=BG, fg=FG_DIM, anchor="w", font=("Microsoft YaHei UI", 8))
        self.lb_save_hint.grid(row=2, column=0, columnspan=3, sticky="w",
                               pady=(2, 0))

        # --- 日志
        logf = tk.LabelFrame(root, text=" 运行日志 ", bg=BG, fg=FG_DIM,
                             bd=1, relief="solid", font=("Microsoft YaHei UI", 9))
        logf.grid(row=5, column=0, sticky="nsew", padx=PX, pady=(2, 10))
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.txt = tk.Text(logf, bg="#15171c", fg=FG_DIM, relief="flat",
                           font=("Consolas", 9), wrap="none", height=5,
                           insertbackground=FG)
        self.txt.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=5)
        sb = tk.Scrollbar(logf, command=self.txt.yview, bg=CARD2,
                          troughcolor=BG, relief="flat", bd=0, width=12)
        sb.grid(row=0, column=1, sticky="ns", pady=5, padx=(0, 6))
        self.txt.configure(yscrollcommand=sb.set, state="disabled")
        self.txt.tag_configure("ERROR", foreground=RED)
        self.txt.tag_configure("WARN", foreground=YELLOW)
        self.txt.tag_configure("OK", foreground=GREEN)
        self.txt.tag_configure("dim", foreground=FG_DIM)

        # × 号 = 隐藏窗口（程序继续在托盘里跑）；真正退出走按钮或托盘菜单
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._refresh_autostart_btn()

    def _load_into_form(self):
        self.e_username.delete(0, "end")
        self.e_username.insert(0, self.cfg.get("username", ""))

        # 账号密码都永久保存（密码经 DPAPI 加密），每次打开自动填充。
        # 密码以掩码显示，点右侧「显示」才看得到明文。
        pwd = secure_store.get_password(self.cfg)
        self.e_password.delete(0, "end")
        if pwd:
            self.e_password.insert(0, pwd)
        self.var_showpwd.set(0)
        self._toggle_pwd_show()
        self._refresh_pwd_state()

        # 宿舍 WiFi：先用配置里的值占位，扫描回来后自动换成带信号强度的标签
        want = (self.cfg.get("wifi_ssid") or "").strip()
        self.cb_wifi.set(want if want else WIFI_ANY)

        # 「记住并自动连接」
        self.var_remember.set(1 if self.cfg.get("auto_connect_wifi") else 0)
        self._sync_remember_state()

    def _sync_remember_state(self):
        """
        「记住并自动连接」只有在选中了**具体某个 WiFi** 时才有意义。
        选「不限制」时置灰并取消勾选（否则无从"连哪一个"）。
        """
        try:
            if self._wifi_selected_value():
                self.chk_remember.configure(state="normal", fg=FG_DIM)
            else:
                self.var_remember.set(0)
                self.chk_remember.configure(state="disabled", fg="#59606e")
        except Exception:
            pass

    def _toggle_pwd_show(self):
        """切换密码框的掩码显示：默认掩码，勾「显示」才看得到明文"""
        self.e_password.configure(show="" if self.var_showpwd.get() else "●")

    def _refresh_pwd_state(self):
        """刷新密码存储状态提示"""
        kind = secure_store.storage_kind(self.cfg)
        if kind == "dpapi":
            txt = "🔒 账号密码已加密保存在本机，每次打开自动填充"
            col = GREEN
        elif kind == "plain":
            txt = "⚠ 当前是明文存储，请输入密码后重新保存以加密"
            col = YELLOW
        else:
            txt = "尚未设置密码，请输入后点「保存并应用」"
            col = FG_DIM
        self.lb_pwd_state.configure(text=txt, fg=col)
    def _request_wifi_scan(self, force=False):
        """在后台线程扫描 WiFi；界面绝不阻塞"""
        now = time.time()
        if self._wifi_scanning:
            return
        if not force and (now - self._wifi_scan_ts) < WIFI_SCAN_GAP:
            return
        self._wifi_scanning = True
        self._wifi_scan_ts = now
        try:
            self.btn_wifi.configure(text="扫描中", state="disabled")
        except Exception:
            pass
        threading.Thread(target=self._wifi_scan_worker, daemon=True).start()

    def _wifi_scan_worker(self):
        try:
            nets = giwifi.list_wifi_networks(force=True)
        except Exception as e:
            nets = []
            self.q.put(("log", "WARN", "扫描 WiFi 失败: %s" % e))
        self.q.put(("wifi", nets, None))

    @staticmethod
    def _wifi_label(info):
        parts = [info["ssid"]]
        if info.get("signal") is not None:
            parts.append("%d%%" % info["signal"])
        else:
            parts.append("信号未知")
        if info.get("connected"):
            parts.append("已连接")
        return "   ·   ".join(parts)

    def _apply_wifi_list(self, nets):
        self._wifi_scanning = False
        try:
            self.btn_wifi.configure(text="刷新", state="normal")
        except Exception:
            pass

        self._wifi_map = {WIFI_ANY: ""}
        labels = [WIFI_ANY]
        for n in nets:
            lb = self._wifi_label(n)
            self._wifi_map[lb] = n["ssid"]
            labels.append(lb)
        if not nets:
            self._wifi_map[WIFI_EMPTY] = ""
            labels.append(WIFI_EMPTY)
        self.cb_wifi.configure(values=labels)

        # 把当前框里的值规范化：不管是裸 SSID（来自配置），还是**信号强度变了
        # 导致失配的旧标签**，都先还原成纯 SSID，再映射到本轮扫描的完整标签。
        cur_ssid = self._ssid_from_wifi_text(self.cb_wifi.get())
        if not cur_ssid:
            self.cb_wifi.set(WIFI_ANY)
        else:
            hit = None
            for lb, val in self._wifi_map.items():
                if val and val == cur_ssid:
                    hit = lb
                    break
            self.cb_wifi.set(hit or cur_ssid)

    def _ssid_from_wifi_text(self, text):
        """
        把下拉框里的文本还原成**纯 SSID**。

        下拉框标签形如 'GiWiFi-xxx-5G   ·   90%   ·   已连接'。
        ⚠ 信号强度随环境变化 —— 同一个网络的标签随时在变，所以**绝不能**
        只靠标签字符串精确匹配映射表：旧标签一旦失配，就会把
        '… · 90% · 已连接' 这一整串当成 SSID 存进 config.json，
        进而让 monitor 的 _wifi_ok() 永远判定「WiFi 不对」而**跳过登录**。
        这里统一按分隔符把 SSID 切出来。
        """
        t = (text or "").strip()
        if not t:
            return ""
        if t in self._wifi_map:                      # 映射表命中，最准
            return self._wifi_map[t]
        # 占位文案绝不能被当成 SSID
        if t.startswith("不限制") or t.startswith("（未扫描到"):
            return ""
        if "·" in t:                                 # 切出第一段
            head = t.split("·")[0].strip()
            if head:
                return head
        return t                                     # 允许手输前缀，例如 "GiWiFi"

    def _wifi_selected_value(self):
        """把下拉框里的文本换算成要写进配置的纯 SSID（空字符串 = 不限制）"""
        return self._ssid_from_wifi_text(self.cb_wifi.get())

    # ---------------------------------------------------------- 队列 → 界面
    def _append_log(self, level, msg):
        line = "%s  %s\n" % (time.strftime("%H:%M:%S"), msg)
        self.txt.configure(state="normal")
        self.txt.insert("end", line, level if level in ("ERROR", "WARN", "OK") else "dim")
        self.txt.see("end")
        self.txt.configure(state="disabled")
        self.log_lines.append(line)

    def _show_alert(self, msg):
        """
        弹窗告知用户「认证失败」—— 由监控线程投递到队列、主线程执行。

        ⚠️ 弹模态框会阻塞 Tk 主循环，所以**绝不能**从监控线程或托盘线程直接调；
        必须走队列回到主线程。
        """
        title = "GiWiFi 认证失败"
        self._append_log("WARN", msg.replace("\n", " "))
        if not self.cfg.get("alert_after_fails"):
            return
        try:
            self.mb.showwarning(title, msg)
        except Exception:
            # 万一弹不出来（例如窗口正在销毁），至少别让主线程挂掉
            pass

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1], item[2])
                elif item[0] == "state":
                    self._set_state(item[1], item[2])
                elif item[0] == "wifi":
                    self._apply_wifi_list(item[1])
                elif item[0] == "tray":
                    # 托盘菜单是在别的线程触发的，统一回到主线程执行
                    self._handle_tray_command(item[1])
                elif item[0] == "alert":
                    self._show_alert(item[1])
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _set_state(self, state, detail):
        self.lb_state.configure(text=state)
        self.cv.itemconfigure(self._dot, fill=STATE_COLOR.get(state, FG_DIM))
        self.lb_detail.configure(text=detail or "")
        # 同步托盘提示文字（只在状态真的变了才去调系统 API）
        if state != self._tray_status:
            self._tray_status = state
            t = getattr(self, "tray", None)
            if t and t.available:
                t.update_tooltip(self._tray_tip())

    def _tick(self):
        """
        定时刷新信息区。

        ⚠ 这里**绝对不能**做网络请求或起子进程（原来的版本每秒调用
        netsh 和 portal_reachable，一个要起进程、一个最多阻塞 2 秒，
        直接把 Tk 主循环卡死 —— 表现就是窗口像卡住、字打不进去）。
        所有探测都在 monitor 后台线程里做，这里只读现成的快照。
        """
        m = self.mon
        self.info_labels["ssid"].configure(text=m.current_ssid or "(未连接)")

        if m.online is None:
            ptxt = "(检测中…)"
        elif m.online:
            ptxt = "(已联网)"
        elif m.portal_ok is None:
            ptxt = "(检测中…)"
        elif m.portal_ok:
            ptxt = "(可达·待认证)"
        else:
            ptxt = "(不可达)"
        self.info_labels["portal"].configure(text="%s  %s" % (self.cfg["portal"], ptxt))

        self.info_labels["stat"].configure(
            text="成功 %d 次 · 失败 %d 次 · 检测于 %s" %
                 (m.total_logins, m.total_fails, m.last_check_time or "--"))
        self.info_labels["last"].configure(text=m.last_login_time or "尚未登录")
        self.root.after(1000, self._tick)

    # ---------------------------------------------------------- 按钮回调
    def on_save(self):
        acct = self.e_username.get().strip()
        pwd = self.e_password.get()
        kind_now = secure_store.storage_kind(self.cfg)

        if not acct:
            self.mb.showwarning("提示", "请填写校园网账号（通常是手机号）。")
            return
        # 输入框为空表示「不修改密码」，但前提是本来就已经存过密码
        if not pwd and kind_now == "none":
            self.mb.showwarning("提示", "请填写密码。")
            return

        try:
            interval = max(3.0, min(600.0, float(self.e_interval.get())))
            if interval == int(interval):
                interval = int(interval)        # 10.0 就存成 10，配置里好看一点
        except ValueError:
            interval = 10.0
            self.e_interval.delete(0, "end")
            self.e_interval.insert(0, "10")

        new_cfg = dict(self.cfg)
        new_cfg["username"] = acct
        new_cfg["check_interval"] = interval

        # 宿舍 WiFi：下拉选的 SSID（空 = 不限制）
        wifi = self._wifi_selected_value()
        new_cfg["wifi_ssid"] = wifi

        # 「记住并自动连接」：勾上且选定了具体 WiFi 才生效
        remember = bool(self.var_remember.get()) and bool(wifi)
        new_cfg["auto_connect_wifi"] = remember
        if remember:
            # 自动连接需要本机已保存的无线配置文件；没有就提前告诉用户
            prof = (new_cfg.get("wifi_profile") or "").strip() or giwifi.find_profile_for(wifi)
            new_cfg["wifi_profile"] = prof
            if not prof:
                self.mb.showwarning(
                    "无法自动连接",
                    "本机没有「%s」的无线配置文件，程序没法自动连上它。\n\n"
                    "请先在 Windows 的 WiFi 列表里手动连一次这个网络，"
                    "之后就能自动连接了。\n\n"
                    "（选择仍会保存，只是暂时无法自动连。）" % wifi)
        else:
            new_cfg["wifi_profile"] = ""

        if pwd:
            try:
                kind = secure_store.set_password(new_cfg, pwd)      # 默认只走 DPAPI
            except Exception as e:
                self.mb.showerror("加密失败", str(e))
                return
            if kind != "dpapi":
                self.mb.showwarning("安全提示", "密码未能加密，已以明文保存。")
        else:
            kind = kind_now                                         # 保留原密文

        # 原地更新，保证监控线程看到的是同一份字典
        self.cfg.clear()
        self.cfg.update(new_cfg)
        giwifi.save_config(self.cfg)

        # 保存后把密码重新填回（掩码显示）——不要清空，否则看起来像"密码丢了"
        if not pwd:
            pwd = secure_store.get_password(self.cfg)
        self.e_password.delete(0, "end")
        if pwd:
            self.e_password.insert(0, pwd)
        self.var_showpwd.set(0)
        self._toggle_pwd_show()
        self._refresh_pwd_state()
        self._sync_remember_state()

        wifi = self.cfg.get("wifi_ssid") or ""
        if not wifi:
            wtxt = "不限制"
        elif self.cfg.get("auto_connect_wifi"):
            wtxt = "%s（已记住，没连上会自动连）" % wifi
        else:
            wtxt = "%s（仅限定，不自动连）" % wifi
        self._append_log("OK", "配置已保存（宿舍 WiFi: %s）%s" % (
            wtxt, "，密码以 DPAPI 密文存于本机" if kind == "dpapi" else ""))
        self.mon.wake()
        # 立即持久化：save_config 走「临时文件 + 原子替换 + fsync」，
        # 到这里数据已经在磁盘上了，关机 / 重启都不会丢 —— 所以不再弹确认框。
        self._flash_saved()

    def on_login_now(self):
        self._append_log("dim", "手动触发一次检测与登录…")
        self.mon.resume()
        self.btn_pause.configure(text="暂停监控")
        self.mon.wake()

    def on_toggle_pause(self):
        if self.mon.paused:
            self.mon.resume()
            self.btn_pause.configure(text="暂停监控")
            self._append_log("dim", "已恢复监控")
        else:
            self.mon.pause(True)
            self.btn_pause.configure(text="恢复监控")
            self._append_log("WARN", "已暂停监控")

    def on_toggle_autostart(self):
        import autostart
        if autostart.is_enabled():
            ok, msg = autostart.disable()
            self._append_log("OK" if ok else "ERROR",
                             "已关闭开机自启" if ok else "关闭失败: %s" % msg)
        else:
            ok, msg = autostart.enable(silent=True)
            self._append_log("OK" if ok else "ERROR",
                             "已设置开机自启（后台静默启动）" if ok else "设置失败: %s" % msg)
        self._refresh_autostart_btn()

    def _refresh_autostart_btn(self):
        import autostart
        on = autostart.is_enabled()
        self.btn_auto.configure(text="开机自启：已开启" if on else "开机自启：已关闭",
                                fg=GREEN if on else FG)

    def on_open_log(self):
        path = self.cfg.get("log_file", "giwifi.log")
        if not os.path.isabs(path):
            path = os.path.join(BASE, path)
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        try:
            os.startfile(path)
        except Exception as e:
            self.mb.showerror("打开失败", str(e))

    # ------------------------------------------------------------ 系统托盘
    def _setup_tray(self):
        """
        创建系统托盘图标。失败也不影响主功能 —— 只是关窗口就直接结束，
        相当于退回旧行为。
        """
        try:
            import tray as tray_mod
        except Exception as e:
            self._append_log("dim", "托盘不可用（%s），关闭窗口将直接退出" % e)
            return None
        if not tray_mod.is_supported():
            return None
        try:
            t = tray_mod.TrayIcon(
                os.path.join(BASE, "giwifi.ico"),
                self._tray_tip(),
                on_command=self._on_tray_command,
                on_activate=self.show_window,
                menu_provider=self._tray_menu)
            if not t.start():
                self._append_log("dim", "托盘图标创建失败，关闭窗口将直接退出")
                return None
        except Exception as e:
            self._append_log("WARN", "托盘初始化异常：%s" % e)
            return None
        self._append_log("dim", "已加入系统托盘：点 × 只隐藏窗口，"
                                "双击托盘图标可重新打开")
        return t

    def _tray_menu(self):
        """托盘右键菜单（每次弹出时动态生成，所以文字能反映当前状态）"""
        if self.mon.paused:
            pause_key, pause_label = "resume", "恢复监控"
        else:
            pause_key, pause_label = "pause", "暂停监控"
        return [
            ("open", "打开主窗口"),
            ("login", "立即登录"),
            ("---", None),
            (pause_key, pause_label),
            ("---", None),
            ("quit", "退出程序"),
        ]

    def _on_tray_command(self, key):
        """托盘菜单回调：在**托盘线程**里触发，投递到主线程处理"""
        self.q.put(("tray", key, None))

    def _handle_tray_command(self, key):
        if key == "open":
            self.show_window()
        elif key == "login":
            self.show_window()
            self.on_login_now()
        elif key == "pause":
            self.mon.pause(True)
            self.btn_pause.configure(text="恢复监控")
            self._append_log("WARN", "已暂停监控（从托盘）")
        elif key == "resume":
            self.mon.resume()
            self.btn_pause.configure(text="暂停监控")
            self._append_log("dim", "已恢复监控（从托盘）")
        elif key == "quit":
            self._append_log("dim", "从托盘退出程序…")
            self.on_quit()

    # --------------------------------------------- 保存反馈 / 显示隐藏 / 退出
    def _flash_saved(self):
        """保存后用「按钮文字 + 提示行」就地反馈，不弹模态框打扰"""
        ts = time.strftime("%H:%M:%S")
        try:
            self.btn_save.configure(text="✓ 已保存")
            self.btn_save.after(1600, lambda: self.btn_save.configure(
                text="保存并应用"))
        except Exception:
            pass
        try:
            self.lb_save_hint.configure(
                text="✓ 已写入本机 config.json（%s）· 关机重启后依然生效" % ts,
                fg=GREEN)
        except Exception:
            pass

    def on_close(self):
        """
        点 × 号：**只隐藏窗口**，程序继续在系统托盘里静默监控。
        真正退出请用「退出程序」按钮，或托盘菜单里的「退出程序」。
        """
        self.hide_window(first=True)

    def hide_window(self, first=False):
        try:
            self.root.withdraw()
        except Exception:
            return
        self._hidden = True
        try:
            # 窗口不可见 → 连「当前 WiFi」显示都不需要刷新了，于是后台运行期间
            # 完全不碰位置相关 API（除非限定了 WiFi、必须判断是否连对）
            self.mon.display_ssid = False
        except Exception:
            pass
        self._append_log("dim", "窗口已隐藏，程序继续在系统托盘运行"
                                "（双击托盘图标可重新打开）。")
        if self.tray and self.tray.available:
            self.tray.update_tooltip(self._tray_tip())
            if first:
                # 第一次隐藏弹个气泡，免得用户以为程序被关掉了
                self.tray.notify("GiWiFi 自动登录助手",
                                 "已最小化到托盘，仍在后台保持网络在线。\n"
                                 "双击托盘图标可重新打开窗口。")

    def show_window(self):
        self._hidden = False
        try:
            # 窗口重新可见 → 恢复低频刷新，并立刻刷新一次让用户看到实时值
            self.mon.display_ssid = True
            self.mon.request_ssid_refresh()
        except Exception:
            pass
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass
        if self.tray and self.tray.available:
            self.tray.update_tooltip(self._tray_tip())

    def _tray_tip(self):
        return "%s · %s" % (APP_TITLE, self._tray_status)

    def on_quit(self):
        """真正退出：停监控 → 通知后台实例 → 撤托盘 → 销毁窗口"""
        try:
            self.mon.stop()
        except Exception:
            pass
        # 让开机自启的那个后台实例也一起退出（它每秒检查这个标记）
        request_quit_all()
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------- 入口
def _crash_log(exc_text):
    try:
        with open(os.path.join(BASE, "crash.log"), "a", encoding="utf-8") as f:
            f.write("\n===== %s =====\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                               exc_text))
    except Exception:
        pass


def main():
    args = [a.lower() for a in sys.argv[1:]]
    # 常量模式（--once / --diag / --test-login）是一次性命令，输出要打到当前控制台，
    # 走 detached 重启会把输出丢掉，所以这些模式跳过低内存重启。
    one_shot = any(a in args for a in ("--once", "--diag", "--test-login"))
    if not one_shot and ensure_low_memory():
        return 0
    try:
        if "--silent" in args or "-s" in args:
            return run_silent()
        if "--once" in args:
            return run_once()
        if "--diag" in args:
            return run_diag()
        if "--test-login" in args:
            cfg = giwifi.load_config()
            acct = cfg.get("username", "")
            pwd = secure_store.get_password(cfg)
            if not acct or not pwd:
                print("请先配置账号密码。")
                return 2
            p = giwifi.GiwifiPortal(cfg, log=lambda lv, m: print("  ", m))
            ok, info, raw = p.login(acct, pwd)
            print("成功" if ok else "失败", "|", info)
            print(raw[:500])
            return 0 if ok else 1
        return run_gui()
    except Exception:
        _crash_log(traceback.format_exc())
        if os.environ.get("GIWIFI_DEBUG"):
            raise
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
