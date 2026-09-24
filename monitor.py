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
# --- 「位置信息」节流 ---
# 查「当前 SSID」走 WlanQueryInterface(current_connection)，Windows 会记成一次
# **位置访问**。所以只在真正必要时机查，并且各自限频：
SSID_CHECK_GAP = 0.0         # >0 才周期性检查 WiFi；默认 0 = 只在掉线需要时查
SSID_DEADEND_GAP = 60.0      # 掉线但「连的不是目标 WiFi 且无法自动连接」时的重查下限
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

        # ---- 「当前 WiFi」显示（与登录判断无关，是位置访问的唯一非必要来源）----
        # display_ssid: 界面是否可见；不可见（收进托盘 / --silent 后台）时**不查**
        self.display_ssid = False
        self._ssid_display_ts = 0.0
        self._ssid_force = False
        self._ssid_check_gap = float(cfg.get("ssid_query_interval") or 0)
        self._ssid_deadend_gap = float(cfg.get("ssid_deadend_interval") or SSID_DEADEND_GAP)
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

    def _ensure_wifi_radio(self, force=False):
        """
        确保 Wi-Fi 无线电（开关）是打开的，返回 True 表示「现在是开的」。

        默认开启，可在配置里 `ensure_wifi_on: false` 关掉
        （比如你不想让程序碰你的无线开关）。走当前登录用户的 WLAN 会话，
        不需要管理员权限。

        ⚠️ 这里**刻意不再"等 6 秒让它关联"**：紧接着的自动连接流程本身就会发起
        连接并轮询结果，盲等只会让开机画面停在那里（实测那 6 秒纯属浪费）。
        """
        if not self.cfg.get("ensure_wifi_on", True):
            return True
        now = time.time()
        if not force and (now - self._last_radio_check) < RADIO_CHECK_GAP:
            return True
        try:
            import wlanapi
            ok, msg = wlanapi.ensure_radio_on()
        except Exception as e:
            self._log("检查 Wi-Fi 开关时出错: %s" % e, "WARN")
            self._last_radio_check = now - RADIO_CHECK_GAP + RADIO_RETRY_GAP
            return False
        if ok is True and not msg:
            self._last_radio_check = now      # 本来就开着 → 45 秒后再看
            return True
        if ok is True:
            self._log(msg)                    # 刚帮它打开了
            self._last_radio_check = now
            return True
        self._log(msg, "WARN")
        # 打开失败（开机时 WLAN 服务可能还没起来）→ 只隔几秒就重试，别等满 45 秒
        self._last_radio_check = now - RADIO_CHECK_GAP + RADIO_RETRY_GAP
        return False

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

        # ---- 自动连接 ----
        if time.time() < self._connect_cooldown:
            return False, ssid                       # 刚试过，别连着猛试
        self._connect_cooldown = time.time() + 60

        profile = (self.cfg.get("wifi_profile") or "").strip() \
            or giwifi.find_profile_for(want)
        if not profile:
            self._log("本机没有「%s」的无线配置文件，无法自动连接。"
                      "请先在系统 WiFi 列表里手动连一次这个网络。" % want, "WARN")
            return False, ssid

        self._log("当前 WiFi 为 %r，正在自动连接到 %r（配置文件 %r）"
                  % (ssid or "未连接", want, profile))
        try:
            # 必须静默执行，否则会闪出命令窗口。
            # ⚠️ netsh 有时会一直阻塞到超时（实测 15s 都不返回），但**连接其实已经下发了**。
            #    所以这里超时只记个提示，绝不能直接放弃 —— 后面照样去轮询 SSID 确认结果。
            winutil.run_hidden(["netsh", "wlan", "connect", "name=" + profile],
                               timeout=8)
        except Exception as e:
            self._log("自动连接命令未正常返回（%s），继续等待连接结果…"
                      % type(e).__name__, "WARN")

        # 轮询确认：由 2 秒 × 10 次（最长 20 秒）改成 1.5 秒 × 8 次（最长 12 秒），
        # 并且一匹配上就立刻返回 —— 实测连接本身只要 2 秒左右，等满 20 秒纯属浪费。
        tries = max(1, int(CONNECT_WAIT / CONNECT_POLL))
        for _ in range(tries):
            self._sleep(CONNECT_POLL)
            now = giwifi.get_current_ssid(force=True)
            self.current_ssid = now
            if self._matches(now, want):
                self._log("✅ 已自动连接到 %r" % now)
                return True, now
        self._log("自动连接 %r 超时未成功，稍后重试" % want, "WARN")
        return False, ssid

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

        # ── 立即打开 Wi-Fi 无线电 ──
        # 这一步原来排在 5 秒盲等**之后**，而无线电启动 + 关联恰恰是最慢的一环，
        # 本该最先做。开机时 WLAN 服务可能还没起来，给它一个重试窗口（上限
        # 就是 startup_delay），一旦打开立刻继续，绝不多等。
        deadline = t_start + max(0.0, float(self.cfg.get("startup_delay", 5)))
        while not self._stop_evt.is_set():
            if self._ensure_wifi_radio(force=True):
                break
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

                # 0) 先把 Wi-Fi 开关打开（默认行为，可用 ensure_wifi_on=false 关闭）
                self._ensure_wifi_radio()

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

                # 3) 外网是否通 —— **通了就什么都不用做**。
                #    ⚠ 这里刻意不去判断「连的是不是宿舍 WiFi」：既然已经在线，
                #      就不需要登录，知道当前 SSID 也毫无用处 —— 而查 SSID 会
                #      访问一次位置信息。原实现在这里每轮都查，等于每 10 秒访问
                #      一次位置信息，正是本次修复的核心问题。
                self._set_state(S_CHECK, "探测外网连通性")
                online, why = giwifi.probe_online(self.cfg)
                self.online = online
                if online:
                    if self.consecutive_fails:
                        self._log("网络已恢复（%s）" % why)
                    self.consecutive_fails = 0
                    self._set_state(S_ONLINE, why)
                    # 默认不再周期性检查 WiFi（那会变成周期性的位置访问）。
                    # 想让程序「一直主动保持在指定 WiFi 上」的人，把配置里的
                    # ssid_query_interval 设成大于 0（例如 60）即可恢复旧行为。
                    if self._ssid_check_gap > 0:
                        ok_w, ssid_w = self._wifi_ok()
                        if not ok_w:
                            self._set_state(S_NO_WIFI,
                                            "当前 SSID: %s" % (ssid_w or "未连接"))
                    self._sleep(interval)
                    continue

                # 4) 掉线了 —— **这才真正需要知道连的是不是目标 WiFi**
                #    （决定「直接登录」还是「先连上宿舍 WiFi」），属于必要时机。
                ok_wifi, ssid = self._wifi_ok(fresh=True)
                if not ok_wifi:
                    self._set_state(S_NO_WIFI, "当前 SSID: %s" % (ssid or "未连接"))
                    # 此刻无事可做（要等用户自己连上目标 WiFi），把周期拉长，
                    # 免得反复查询 —— 每次查询都会记一次位置访问。
                    self._sleep(max(interval, self._ssid_deadend_gap))
                    continue

                # 5) 门户是否可达 → 提交认证
                self._log("检测到未联网（%s），准备自动登录" % why, "WARN")
                self.portal_ok = giwifi.portal_reachable(self.cfg)
                if not self.portal_ok:
                    self._set_state(S_NO_PORTAL, "认证网关 %s 不可达" %
                                    giwifi.gateway_host(self.cfg))
                    self._sleep(interval)
                    continue

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
