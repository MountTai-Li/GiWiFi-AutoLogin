# -*- coding: utf-8 -*-
"""
monitor.py —— 后台监控引擎：探测网络 → 掉线自动登录 → 退避重试

设计要点（都是从真实门户行为反推出来的）：
  * 门户有频率限制，实测会返回「操作过于频繁,请5秒后再试！」，
    因此两次真实登录请求之间强制间隔 ≥ MIN_ATTEMPT_GAP 秒。
  * 登录失败不能死循环猛冲，采用线性退避（6s / 12s / 18s ... 上限 120s）。
  * 网络恢复判定以"真实外网探测点可通"为准，而不是只看登录接口返回。
"""
import os
import re
import threading
import time

import giwifi
import winutil

# 状态常量
S_WAIT = "等待启动"
S_CHECK = "检测中"
S_ONLINE = "网络正常"
S_LOGIN = "正在登录"
S_FAIL = "登录失败"
S_NO_PORTAL = "门户不可达"
S_NO_WIFI = "未连接校园WiFi"
S_PAUSED = "监控已暂停"
S_NOPWD = "未配置账号密码"
# v1.12 新增：用户自己关掉了 Wi-Fi 开关 —— 程序尊重这个操作，不去打开它
S_WIFI_OFF = "Wi-Fi 已关闭"

MIN_ATTEMPT_GAP = 6.0        # 两次真实登录请求的最小间隔（门户限频 5s）
# 认证失败后的重试间隔。原来是「指数退避」：第 1 次失败要等 interval+6=16 秒，
# 实测开机时认证往往第 2 次就成功，却白等了那 16 秒 —— 改成固定 5 秒快速重试。
RETRY_INTERVAL = 5.0
# 门户自身限制 5 秒内不能重复提交，所以 5 秒的「重试周期」配合 6 秒的「提交闸门」，
# 实际提交间隔约 6 秒 —— 这是在不触发门户限频的前提下能做到的最快节奏。
READY_POLL = 0.5             # 等 Wi-Fi 就绪时的轮询间隔
RADIO_RETRY_GAP = 5.0        # 打开 Wi-Fi 失败后的重试间隔（不用等满 45 秒）
CONNECT_POLL = 1.5           # 自动连接后确认 SSID 的轮询间隔
CONNECT_WAIT = 12.0          # 自动连接后最多等多久
RADIO_CHECK_GAP = 45.0       # 每隔多久检查一次 Wi-Fi 开关（读开关状态不涉及位置）
WIFI_ATTEMPT_GAP = 12.0      # 两次自动连接之间的最小间隔（跨进程共用，见 _claim_wifi_attempt）
CONNECT_ATTEMPTS_FAST = 3    # 连续失败几次后转慢速重试
CONNECT_SLOW_GAP = 60.0      # 慢速重试间隔（信号范围外别 10 秒一次猛试）
# --- 「位置信息」节流 ---
# 查「当前 SSID」走 WlanQueryInterface(current_connection)，Windows 会记成一次
# **位置访问**。所以只在真正必要时机查，并且各自限频：
SSID_CHECK_GAP = 0.0         # >0 才周期性检查 WiFi；默认 0 = 只在掉线需要时查
SSID_DISPLAY_GAP = 120.0     # 仅为界面显示刷新的间隔（窗口隐藏时完全不查）

# 2.4G / 5G 是同一个网络的两个频段，名字通常只差一个后缀
_BAND_SUFFIX = re.compile(r"[-_\s]?(?:5g|2\.4g|24g|5ghz|2\.4ghz)$", re.I)


def _strip_band(name):
    return _BAND_SUFFIX.sub("", (name or "").strip()).lower()


def clean_ssid(s):
    """
    把可能被误写成「界面标签」的 SSID 还原成纯 SSID。

    界面下拉框标签形如 'GiWiFi-xxx-5G   ·   90%   ·   已连接'，其中的信号强度
    随时会变 —— 一旦有旧版本把整串存进了 config.json，_wifi_ok() 就会永远
    判定「WiFi 不对」，从而**直接跳过登录**（表现为「开机不自动登录」）。
    UI 侧已修好；这里再加一层自愈，兜住历史脏配置和手工误改。
    """
    s = (s or "").strip()
    if "·" in s:
        head = s.split("·")[0].strip()
        if head:
            return head
    return s


def _same_network(a, b):
    """判断两个 SSID 是否指向同一个网络（忽略 2.4G/5G 后缀差异）"""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    return _strip_band(a) == _strip_band(b)


class Monitor:
    """线程化的监控引擎，通过回调把状态与日志抛给界面"""

    def __init__(self, cfg, on_log=None, on_state=None, on_alert=None):
        self.cfg = cfg
        self._on_log = on_log or (lambda level, msg: None)
        self._on_state = on_state or (lambda state, detail="": None)
        # 认证失败要「弹提示告知用户」时回调（界面/静默模式各自实现弹窗方式）
        self._on_alert = on_alert or (lambda msg: None)
        self._alert_armed = True         # 每个「失败连续段」只提醒一次，成功后重新武装
        self._retry_interval = max(3.0, float(cfg.get("retry_interval") or RETRY_INTERVAL))

        self._thread = None
        self._stop_evt = threading.Event()
        self._wake_evt = threading.Event()
        self._lock = threading.RLock()

        self.state = S_WAIT
        self.detail = ""
        self.paused = False

        # 运行统计
        self.total_logins = 0
        self.total_fails = 0
        self.last_login_time = ""
        self.last_check_time = ""
        self.consecutive_fails = 0
        self._last_attempt = 0.0
        self._connect_cooldown = 0.0     # 自动连接 WiFi 的冷却时间戳
        self._last_radio_check = 0.0     # 上次检查 Wi-Fi 开关的时间

        # ---- v1.12：「尊重用户手动关 WiFi」+「断了就自动连回去」的状态机 ----
        self._radio_seen_on = False      # 本次运行里是否见过无线开关打开
        self._radio_was_off = False      # 上一次读到的开关状态（用于识别 关→开 这个动作）
        self._user_off_notified = False  # 「你自己关的，我不替你打开」只提示一次
        self._connect_fails = 0          # 连续自动连接失败次数（转慢速重试用）
        self._portal_down = 0            # 连续几轮"认证网关不可达"（判定不在校园网）
        self.link_state = None           # 最近的链路状态：up / linking / down / None

        # ---- 「当前 WiFi」显示（与登录判断无关，是位置访问的唯一非必要来源）----
        # display_ssid: 界面是否可见；不可见（收进托盘 / --silent 后台）时**不查**
        self.display_ssid = False
        self._ssid_display_ts = 0.0
        self._ssid_force = False
        self._ssid_check_gap = float(cfg.get("ssid_query_interval") or 0)
        self._ssid_display_gap = float(cfg.get("ssid_display_interval") or SSID_DISPLAY_GAP)

        # 供界面读取的「已算好的」快照 —— 界面绝不自己做网络/进程调用，
        # 否则会阻塞 Tk 主循环，导致窗口卡死、无法输入
        self.current_ssid = ""
        self.portal_ok = None            # None=还没测过
        self.online = None

    # ------------------------------------------------------------ 对外控制
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="giwifi-monitor",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_evt.set()
        self._wake_evt.set()

    def pause(self, flag=True):
        with self._lock:
            self.paused = bool(flag)
        self._wake_evt.set()
        if self.paused:
            self._set_state(S_PAUSED, "手动暂停")

    def resume(self):
        self.pause(False)

    def wake(self):
        """叫醒循环，立刻做一次检测（用于「立即登录」）"""
        self.consecutive_fails = 0
        self._last_attempt = 0.0            # 允许立刻发起一次
        self._wake_evt.set()

    # ------------------------------------------------------------ 内部工具
    def _log(self, msg, level="INFO"):
        line = "[%s] %s" % (level, msg)
        try:
            self._on_log(level, msg)
        except Exception:
            pass
        self._write_file(line)

    def _write_file(self, line):
        path = self.cfg.get("log_file") or "giwifi.log"
        if not os.path.isabs(path):
            path = os.path.join(giwifi.app_dir() if hasattr(giwifi, "app_dir")
                                else os.path.dirname(os.path.abspath(__file__)), path)
        try:
            # 简单轮转，超过 512KB 就滚成 .1
            if os.path.exists(path) and os.path.getsize(path) > 512 * 1024:
                bak = path + ".1"
                if os.path.exists(bak):
                    os.remove(bak)
                os.replace(path, bak)
            with open(path, "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except Exception:
            pass

    def _set_state(self, state, detail=""):
        self.state = state
        self.detail = detail
        try:
            self._on_state(state, detail)
        except Exception:
            pass

    def _sleep(self, seconds):
        """可被 stop / wake 打断的等待"""
        self._wake_evt.wait(timeout=seconds)
        self._wake_evt.clear()

    def _matches(self, ssid, want):
        """SSID 是否算命中目标：完全相同 / 前缀 / 去掉 2.4G-5G 后缀后相同"""
        return bool(ssid) and bool(want) and (
            ssid == want or ssid.startswith(want) or _same_network(ssid, want))

    def _radio_policy(self):
        """Wi-Fi 开关的处理策略：startup（默认）/ always / never"""
        pol = str(self.cfg.get("wifi_radio_policy") or "startup").strip().lower()
        return pol if pol in ("startup", "always", "never") else "startup"

    def _can_open_radio(self):
        """按当前策略，程序此刻允不允许替用户把 Wi-Fi 开关打开"""
        pol = self._radio_policy()
        if not self.cfg.get("ensure_wifi_on", True):
            return False
        return pol == "always" or (pol == "startup" and not self._radio_seen_on)

    def _check_wifi_radio(self, startup=False):
        """
        看 Wi-Fi 无线电（开关）状态，返回 True 表示「开着」。

        **这里是「尊重用户」规则唯一落地的地方。**
        旧版本每 45 秒就把开关打开一次 —— 用户手动关掉 Wi-Fi，程序过一会儿又给
        打开，等于跟用户对着干（而且他明明只是想临时断网）。现在由 `wifi_radio_policy`
        决定（默认 `startup`）：

            startup：只在本次运行里**还没见过开关打开**时才尝试打开一次
                     （= 保留 v1.4「开机自动打开 Wi-Fi」的行为）；
                     一旦见它开过，之后用户再关掉就**绝不再碰**。
            always ：发现关着就打开（v1.10 及以前的老行为）
            never  ：永远不碰开关，完全交给用户
            （另外 `ensure_wifi_on: false` 也能一票否决所有自动打开）

        读开关状态走 `wlanapi.radio_state()`，**不涉及位置信息**，可以每轮都问。
        """
        if not self.cfg.get("ensure_wifi_on", True):
            return True
        now = time.time()
        try:
            import wlanapi
            on = wlanapi.radio_state()
        except Exception as e:
            self._log("检查 Wi-Fi 开关时出错: %s" % e, "WARN")
            return True                      # 判断不了就别挡着后面的登录
        if on is None:
            return True                      # 同上：查询不支持时按"开着"处理

        if on:
            self._radio_seen_on = True
            if self._radio_was_off:
                # 用户自己把 Wi-Fi 打开了 → 立刻允许重连，不等退避
                self._radio_was_off = False
                self._user_off_notified = False
                self._connect_fails = 0
                self._connect_cooldown = 0.0
                self._log("检测到 Wi-Fi 已打开，立即检查连接…")
            self._last_radio_check = now
            return True

        # ---- 开关是关着的 ----
        self._radio_was_off = True
        if not self._can_open_radio():
            if not self._user_off_notified:
                self._user_off_notified = True
                self._log("Wi-Fi 已被关闭 —— 这是你的操作，程序不会替你打开；"
                          "等你打开后会自动连接并认证", "WARN")
            return False

        # 允许打开：失败只隔几秒就重试，不用等满 RADIO_CHECK_GAP
        if not startup and (now - self._last_radio_check) < RADIO_CHECK_GAP:
            return False
        try:
            ok, msg = wlanapi.ensure_radio_on()
        except Exception as e:
            self._log("打开 Wi-Fi 时出错: %s: %s" % (type(e).__name__, e), "WARN")
            self._last_radio_check = now - RADIO_CHECK_GAP + RADIO_RETRY_GAP
            return False
        if ok is True:
            self._radio_seen_on = True
            self._last_radio_check = now
            if msg:
                self._log(msg)               # 刚帮它打开了（开机那一次）
            return True
        self._log(msg, "WARN")
        self._last_radio_check = now - RADIO_CHECK_GAP + RADIO_RETRY_GAP
        return False

    def _wifi_link(self):
        """
        Wi-Fi 链路状态（**零位置访问、零子进程**）：up / linking / down / None。

        有了它才能在「不查 SSID」的前提下知道"到底连上没有" —— 而查 SSID 会被
        Windows 记成一次位置访问。顺带解决了老问题：以前想确认连接是否成功，
        只能反复查 SSID（一次连接最多查 8 次 = 8 次位置访问），现在改用链路状态轮询，
        只在最后确认是否为目标网络时才查一次。
        """
        try:
            import wlanapi
            self.link_state = wlanapi.wifi_link_state()
        except Exception:
            self.link_state = None
        return self.link_state

    def request_ssid_refresh(self):
        """
        请求尽快刷新一次「当前 WiFi」显示（界面刚打开时调用）。

        只设标记 + 叫醒循环，**不在调用线程里直接查** —— 查 SSID 会触发一次
        位置访问，必须统一由监控线程按节流规则发起，避免界面反复点击就反复查。
        """
        self._ssid_force = True
        try:
            self._wake_evt.set()
        except Exception:
            pass

    def _refresh_ssid_for_display(self):
        """
        仅为「界面显示」刷新当前 SSID —— 这是本程序**唯一**在「不限制 WiFi」
        时还会碰到位置相关 API 的地方，所以有三重约束：

          1. 窗口不可见（收进托盘 / `--silent` 后台）时**直接不查** → 零位置访问；
          2. 距上次刷新不足 `ssid_display_interval` 秒不查；
          3. 结果只写进 `self.current_ssid` 供界面读取，**不参与任何登录判断**。
        """
        if not self.display_ssid:
            return
        now = time.time()
        if not self._ssid_force and (now - self._ssid_display_ts) < self._ssid_display_gap:
            return
        self._ssid_force = False
        self._ssid_display_ts = now
        try:
            self.current_ssid = giwifi.get_current_ssid(force=True)
        except Exception:
            pass

    def _target_wifi(self):
        """
        要自动连接的目标网络：优先 `wifi_ssid`，没填就用无线配置文件名兜底
        （有些人是直接编辑 config.json 只填了 wifi_profile）。
        返回空串表示"没有目标网络"，此时不会做任何自动连接。
        """
        return (clean_ssid(self.cfg.get("wifi_ssid"))
                or clean_ssid(self.cfg.get("wifi_profile")))

    def _wifi_ok(self, fresh=False):
        """
        SSID 过滤 + 可选的「自动连接」。

        wifi_ssid 留空 = 不限制（任何 WiFi 下都尝试登录，最省心）。
        填了则支持三种匹配：完全相同、前缀、去掉 2.4G/5G 后缀后相同。
        勾了「记住并自动连接」时，若当前没连上目标 WiFi，就主动去连它。

        ⚠ `fresh=True` 表示「这次判断必须准确」（掉线、准备登录时用），会跳过缓存
        立刻查一次；否则走 ssid_check_gap 的缓存。缓存的意义就是**少访问几次位置信息**。
        """
        want = clean_ssid(self.cfg.get("wifi_ssid"))

        # ---- 「不限制」分支：登录判断与 SSID 无关，**绝不查询** ----
        # 这是本次「位置信息」修复的关键：默认配置下 wifi_ssid 为空，
        # 以前这里每轮（10 秒）都会查一次当前 SSID，等于每 10 秒访问一次位置信息。
        if not want:
            return True, ""

        ssid = giwifi.get_current_ssid(
            ttl=(0 if fresh else max(self._ssid_check_gap, giwifi.SSID_TTL_DEFAULT)),
            force=bool(fresh))
        self.current_ssid = ssid
        if self._matches(ssid, want):
            return True, ssid
        if not self.cfg.get("auto_connect_wifi"):
            return False, ssid
        ok, now = self._connect_target(want, "（当前连的是 %r）" % (ssid or "未连接"))
        return ok, (now or ssid)

    # ------------------------------------------------------------ 自动连接
    def _wifi_attempt_path(self):
        return os.path.join(giwifi.app_dir(), "giwifi.wifi")

    def _claim_wifi_attempt(self):
        """
        跨进程让位：机器上可能同时跑着两个实例（开机自启的后台 + 你手动打开的界面），
        两边都会想去连 WiFi。实测过两条 `netsh wlan connect` 互相打断 ——
        日志里两个进程都报「自动连接超时未成功」，然后各自白等 60 秒。
        所以这里用一个文件时间戳做闸门，同一时刻只让一个实例真的下发连接命令。

        返回 True = 这次归我连；False = 刚有人连过，我先让一让。
        """
        path = self._wifi_attempt_path()
        try:
            last = os.path.getmtime(path)
        except OSError:
            last = 0.0
        if time.time() - last < WIFI_ATTEMPT_GAP:
            return False
        try:
            with open(path, "a", encoding="utf-8"):
                pass
            os.utime(path, None)
        except Exception:
            pass                              # 写不了就照常连，不能因此不干活
        return True

    def _connect_target(self, want, reason=""):
        """
        主动连接到目标 WiFi。返回 (成功?, 实际 SSID)。

        与旧实现的三点区别：
          1. **不需要先知道当前 SSID** —— 最典型的调用场景就是「链路已经断了」，
             这时链路状态（零位置访问）已经说明问题，再查一次 SSID 纯属浪费；
          2. 确认连接结果改用**链路状态轮询**（零位置访问），只在最后核对
             "连的是不是目标网络"时才查一次 SSID —— 旧实现一次连接要查最多 8 次；
          3. 跨进程去重 + 连续失败退避（信号范围外不会 10 秒一次猛试）。
        """
        if time.time() < self._connect_cooldown:
            return False, ""
        if not self._claim_wifi_attempt():
            return False, ""

        profile = (self.cfg.get("wifi_profile") or "").strip() \
            or giwifi.find_profile_for(want)
        if not profile:
            self._log("本机没有「%s」的无线配置文件，无法自动连接。"
                      "请先在系统 WiFi 列表里手动连一次这个网络。" % want, "WARN")
            self._connect_cooldown = time.time() + CONNECT_SLOW_GAP
            return False, ""

        self._log("正在自动连接到 %r%s" % (want, reason))
        try:
            # 必须静默执行，否则会闪出命令窗口。
            # ⚠️ netsh 有时会一直阻塞到超时（实测 15s 都不返回），但**连接其实已经下发了**。
            #    所以超时只记个提示，绝不能直接放弃 —— 后面照样轮询链路状态确认结果。
            winutil.run_hidden(["netsh", "wlan", "connect", "name=" + profile],
                               timeout=8)
        except Exception as e:
            self._log("自动连接命令未正常返回（%s），继续等待连接结果…"
                      % type(e).__name__, "WARN")

        tries = max(1, int(CONNECT_WAIT / CONNECT_POLL))
        for _ in range(tries):
            self._sleep(CONNECT_POLL)
            st = self._wifi_link()
            if st == "linking":
                continue                      # 已关联、正在拿 IP —— 再等等，别打断
            if st != "up":
                continue
            # 链路起来了 → 确认到底连的是不是目标（这一步才需要查 SSID）
            now = giwifi.get_current_ssid(force=True)
            self.current_ssid = now
            if not now or self._matches(now, want):
                self._connect_fails = 0
                self._connect_cooldown = time.time() + WIFI_ATTEMPT_GAP
                self._log("✅ 已自动连接到 %r" % (now or want))
                return True, (now or want)
            self._log("连上了 %r，但不是目标网络 %r，稍后重试" % (now, want), "WARN")
            break

        self._connect_fails += 1
        gap = (WIFI_ATTEMPT_GAP if self._connect_fails < CONNECT_ATTEMPTS_FAST
               else CONNECT_SLOW_GAP)
        self._connect_cooldown = time.time() + gap
        self._log("自动连接 %r 未成功（连续第 %d 次），%.0f 秒后重试"
                  % (want, self._connect_fails, gap), "WARN")
        return False, ""

    def _maybe_alert_failure(self, info):
        """
        首次认证失败时**弹提示告知用户**，然后继续按 retry_interval 自动重试。

        提醒次数由配置 `alert_after_fails` 控制：
            1（默认）= 第一次失败就提醒
            0        = 完全不提醒（只写日志）
            3        = 连续失败 3 次才提醒

        每个「失败连续段」只提醒一次 —— 认证成功后重新武装；
        这样不会因为一直在重试而反复弹窗骚扰。
        """
        limit = int(self.cfg.get("alert_after_fails") or 0)
        if limit <= 0 or not self._alert_armed:
            return
        if self.consecutive_fails < limit:
            return
        self._alert_armed = False
        msg = ("校园网认证失败：%s\n\n"
               "程序会每 %.0f 秒自动重试，直到认证成功。\n"
               "若一直失败，请检查账号密码是否正确。" % (info, self._retry_interval))
        try:
            self._on_alert(msg)
        except Exception:
            pass

    # ------------------------------------------------- 跨进程限频（文件锁）
    def _throttle_path(self):
        base = giwifi.app_dir()
        return os.path.join(base, "giwifi.throttle")

    def _wait_global_gap(self):
        """
        门户限制 5 秒内不能重复提交。但同一个门户上可能同时跑着
        两个实例（开机自启的 --silent + 用户手动打开的界面），
        各自的内存计时器管不到对方，所以用一个文件的时间戳做「跨进程限频」，
        保证机器上任何时刻都只有一个实例在真正请求门户。
        """
        path = self._throttle_path()
        while True:
            try:
                last = os.path.getmtime(path)
            except OSError:
                last = 0.0
            gap = time.time() - last
            if gap >= MIN_ATTEMPT_GAP:
                return
            wait = MIN_ATTEMPT_GAP - gap
            if wait >= 2.0:      # 几秒钟的微调不必刷屏
                self._log("距上次登录仅 %.1fs，等待 %.1fs 以避开门户限频" % (gap, wait))
            self._sleep(wait)

    def _mark_attempt(self):
        try:
            with open(self._throttle_path(), "a", encoding="utf-8"):
                pass
            os.utime(self._throttle_path(), None)
        except Exception:
            pass

    def _do_login(self):
        """执行一次登录；返回 (成功?, 描述)"""
        account = self.cfg.get("username", "").strip()
        import secure_store
        password = secure_store.get_password(self.cfg)
        if not account or not password:
            return None, "未配置账号或密码"

        # 限频（跨进程）：距上次真实请求不足 MIN_ATTEMPT_GAP 秒就等一等
        self._wait_global_gap()
        self._last_attempt = time.time()
        self._mark_attempt()

        try:
            portal = giwifi.GiwifiPortal(self.cfg, log=self._log)
            ok, info, raw = portal.login(account, password)
        except Exception as e:
            # 网络抖动 / 门户改版 / 解析失败，都当成一次失败而不是让线程崩掉
            return False, "%s: %s" % (type(e).__name__, e)
        if ok:
            self.total_logins += 1
            self.last_login_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.consecutive_fails = 0
            return True, info

        # 门户返回的常见业务拒绝，给出人话解释
        hint = ""
        for code, text in (("32", "该账号还未设置密码，请先在门户「忘记密码」里设置"),
                           ("40", "需要先完善个人信息，请登录门户补充"),
                           ("41", "需要先绑定手机号"),
                           ("43", "需要重新绑定设备 MAC")):
            if '"reasoncode":%s' % code in raw.replace(" ", ""):
                hint = text
                break
        if not hint and "认证拒绝" in raw:
            hint = "账号或密码错误（请检查 config.json / 界面里的账号密码）"
        if "操作过于频繁" in raw:
            hint = "触发了门户频率限制，稍后自动重试"
        return False, (info + ("｜" + hint if hint else ""))

    def _verify_online(self, tries=3, wait=2.0):
        for i in range(tries):
            ok, why = giwifi.probe_online(self.cfg)
            if ok:
                return True, why
            if i < tries - 1:
                time.sleep(wait)
        return False, why

    # ------------------------------------------------------------ 主循环
    def _loop(self):
        """
        主循环。启动阶段的设计原则：**能不等就不等，要等就等真信号**。

        改造前的启动序列（实测开机 30 秒才联网，其中 27 秒是纯等待）：
            盲等 startup_delay 5 秒 → 开 Wi-Fi → 又盲等 6 秒 → 连接 → 认证
                                                                  → 失败后退避 16 秒
        改造后：
            立即开 Wi-Fi → 立刻进循环发起连接 → 失败后每 5 秒重试直到成功
        """
        t_start = time.time()
        self._set_state(S_WAIT, "正在准备 Wi-Fi")
        self._log("程序启动，正在准备网络")

        # ── 打开 Wi-Fi 无线电 ──
        # 只在**这一刻**帮用户打开一次（v1.4 的「开机自动开 Wi-Fi」）。
        # 之后用户要是自己关掉，程序就不再碰（见 _check_wifi_radio 的说明）。
        deadline = t_start + max(0.0, float(self.cfg.get("startup_delay", 5)))
        while not self._stop_evt.is_set():
            if self._check_wifi_radio(startup=True):
                break
            if not self._can_open_radio():
                break                       # 策略不允许打开 → 没必要干等
            if time.time() >= deadline:
                self._log("等待 Wi-Fi 就绪超时，仍继续尝试", "WARN")
                break
            self._sleep(READY_POLL)

        interval = max(3.0, float(self.cfg.get("check_interval", 10)))
        self._log("进入监控循环（Wi-Fi 准备用时 %.1f 秒），检测间隔 %.0f 秒"
                  % (time.time() - t_start, interval))

        while not self._stop_evt.is_set():
            try:
                if self.paused:
                    self._sleep(2)
                    continue

                self.last_check_time = time.strftime("%H:%M:%S")

                # 0) Wi-Fi 开关。用户自己关掉的就尊重他：不打开、不查 SSID、
                #    也不去戳门户 —— 只是每 10 秒看一眼他有没有重新打开。
                if not self._check_wifi_radio():
                    self._set_state(S_WIFI_OFF,
                                    "Wi-Fi 已关闭（你自己关的），打开后会自动连接并认证")
                    self._sleep(interval)
                    continue

                # 1) 仅为「界面显示」刷新一次当前 WiFi。
                #    它自带三重约束：窗口隐藏时**完全不查**、有 120 秒节流、
                #    且不参与任何登录判断。见 _refresh_ssid_for_display 的说明。
                self._refresh_ssid_for_display()

                # 2) 账号密码没配好就没有可做的事
                import secure_store
                if not (self.cfg.get("username") and secure_store.get_password(self.cfg)):
                    self._set_state(S_NOPWD, "请在界面填写账号密码并保存")
                    self._sleep(max(interval, 15))
                    continue

                # 3) Wi-Fi 链路状态（**零位置访问**）：断了就立刻连回去。
                #    这是 v1.12 的核心修复 —— 旧实现只有"外网不通"才会走到自动连接，
                #    而且连接前后要反复查 SSID（每次都是一次位置访问），
                #    失败后还要等 60 秒，所以用户手动关开 Wi-Fi 后往往几分钟才恢复。
                want = self._target_wifi()
                link = self._wifi_link()
                self.portal_ok = None

                if want and self.cfg.get("auto_connect_wifi"):
                    need, why = False, ""
                    if link == "down":
                        # 链路根本没起来 —— 不需要任何额外探测，直接连
                        need, why = True, "（Wi-Fi 已断开）"
                    elif self.cfg.get("switch_to_target_wifi"):
                        # **可选行为**（`switch_to_target_wifi`，默认关）：
                        # 连的是别的网络（手机热点 / 家里）时也切回宿舍 WiFi。
                        # 默认关是为了不打扰"当前明明能上网"的情况。
                        # 判据用**认证网关是否可达** —— 它是内网地址，
                        # 在家/热点上根本不可达，而且零位置访问。
                        self.portal_ok = giwifi.portal_reachable(self.cfg)
                        if self.portal_ok:
                            self._portal_down = 0
                        else:
                            self._portal_down += 1
                            if self._portal_down >= 2:
                                # 连续两轮都不在校园网内（而不是门户抖了一下）才动手
                                need, why = True, "（当前不在校园网内）"
                    if need:
                        ok_c, ssid_c = self._connect_target(want, why)
                        if ok_c:
                            self.current_ssid = ssid_c
                        else:
                            self._set_state(S_NO_WIFI, "Wi-Fi 未连接，正在自动连接 %s" % want)
                            self._sleep(interval)
                            continue

                # 4) 外网是否通 —— **通了就什么都不用做**。
                self._set_state(S_CHECK, "探测外网连通性")
                online, why = giwifi.probe_online(self.cfg)
                self.online = online
                if online:
                    if self.consecutive_fails:
                        self._log("网络已恢复（%s）" % why)
                    self.consecutive_fails = 0
                    self._set_state(S_ONLINE, why)
                    # 默认不再周期性核对 SSID（那会变成周期性的位置访问）。
                    # 想让程序「一直死盯着必须连在指定 WiFi 上」的人，把配置里的
                    # ssid_query_interval 设成大于 0（例如 60）即可恢复旧行为。
                    if self._ssid_check_gap > 0:
                        ok_w, ssid_w = self._wifi_ok()
                        if not ok_w:
                            self._set_state(S_NO_WIFI,
                                            "当前 SSID: %s" % (ssid_w or "未连接"))
                    self._sleep(interval)
                    continue

                # 5) 掉线了。网关可达性上面可能已经测过；没测过（没配目标网络）就补测。
                if self.portal_ok is None:
                    self.portal_ok = giwifi.portal_reachable(self.cfg)
                #    网关都够不着 → 大概率根本没连在校园网上，跳过登录（不去试错）。
                if not self.portal_ok:
                    self._set_state(S_NO_PORTAL, "认证网关 %s 不可达"
                                    % giwifi.gateway_host(self.cfg))
                    self._sleep(interval)
                    continue

                # 6) 网关可达 → 提交认证
                self._log("检测到未联网（%s），准备自动登录" % why, "WARN")

                self._set_state(S_LOGIN, "正在提交认证…")
                ok, info = self._do_login()

                if ok is None:
                    self._set_state(S_NOPWD, info)
                    self._sleep(max(interval, 15))
                    continue

                if ok:
                    self._alert_armed = True          # 成功后重新武装失败提示
                    self._log("✅ 登录成功：%s" % info)
                    online2, why2 = self._verify_online()
                    if online2:
                        self._set_state(S_ONLINE, "自动登录成功 · " + why2)
                    else:
                        self._set_state(S_ONLINE, "认证已通过（探测点暂未通过）")
                    self._sleep(interval)
                    continue

                # 4) 失败 → **固定间隔快速重试，直到成功**
                self.consecutive_fails += 1
                self.total_fails += 1
                self._log("❌ 登录失败（第 %d 次）：%s，%.0f 秒后重试"
                          % (self.consecutive_fails, info, self._retry_interval), "ERROR")
                self._set_state(S_FAIL, "%s（%.0f 秒后重试）" % (info, self._retry_interval))
                self._maybe_alert_failure(info)
                # 不做指数退避：原来第 1 次失败要等 16 秒，而认证往往第 2 次就成功，
                # 那 16 秒是白等的。改成 retry_interval（默认 5 秒）一直重试。
                self._sleep(self._retry_interval)

            except Exception as e:
                self._log("监控循环异常：%s: %s" % (type(e).__name__, e), "ERROR")
                self._sleep(interval)

        self._set_state(S_PAUSED, "已退出监控")
        self._log("监控已停止")
