# -*- coding: utf-8 -*-
"""
winutil.py —— Windows 进程/控制台相关的底层工具

为什么需要这个模块：
    `pythonw.exe` 启动的进程**没有控制台**。此时如果调用 `subprocess` 去跑
    `netsh.exe` 这类控制台程序，Windows 会**为子进程新建一个控制台窗口**——
    表现就是屏幕上不断闪出黑色命令窗口。
    必须给子进程加上 CREATE_NO_WINDOW（并配合 STARTUPINFO 隐藏），才能真正静默。

同时提供：
    * is_windows()          平台判断
    * run_hidden()          静默执行命令并取输出（替代 subprocess.run）
    * popen_detached()      静默启动一个后台进程
    * hide_own_console()    万一自己被控制台启动，把自己的控制台窗口收掉
"""
import os
import subprocess
import threading
import sys

IS_WIN = os.name == "nt"

# Windows 进程创建标志
CREATE_NO_WINDOW = 0x08000000      # 不创建控制台窗口
DETACHED_PROCESS = 0x00000008      # 完全脱离控制台


def _startupinfo():
    """让子进程窗口不显示（对 CREATE_NO_WINDOW 是双保险）"""
    if not IS_WIN:
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def hidden_kwargs():
    """给 subprocess 调用用的「静默」关键字参数"""
    if not IS_WIN:
        return {}
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": _startupinfo()}


def decode_output(raw, encoding=None, errors="replace"):
    """
    把子进程的原始字节解码成字符串。

    ⚠ 坑：本机实测 `netsh` 输出的是 **UTF-8**，而系统 ANSI 代码页是 GBK(cp936)。
      如果写死按 GBK 解，中文会全变乱码（"接口名称" → "鎺ュ彛鍚嶇О"）。
      之前的 get_current_ssid 没暴露这个问题，只是因为 WiFi 名恰好是 ASCII。
    策略：显式指定优先；否则先严格按 UTF-8 试，失败再按 GBK。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if encoding:
        try:
            return raw.decode(encoding, errors)
        except LookupError:
            pass
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors)


# --------------------------------------------------------------- 原生消息框
MB_OK = 0x00000000
MB_ICONWARNING = 0x00000030
MB_ICONINFORMATION = 0x00000040
MB_TOPMOST = 0x00040000
MB_SETFOREGROUND = 0x00010000


def message_box(title, text, flags=None):
    """
    弹一个原生 Windows 消息框（走 user32.MessageBoxW）。

    为什么不用 tkinter 的 messagebox：`--silent` 后台模式根本没有 tkinter 窗口，
    但也需要能把「认证失败」这类事情弹给用户看。

    ⚠️ 这个调用会**阻塞当前线程**直到用户点掉。后台/监控线程里请用
       `show_alert_async()`，避免把业务线程卡住。
    """
    if not IS_WIN:
        return None
    if flags is None:
        flags = MB_OK | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _user32.MessageBoxW.restype = ctypes.c_int
        _user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_wchar_p, ctypes.c_uint]
        return _user32.MessageBoxW(None, str(text), str(title), int(flags))
    except Exception:
        return None


def show_alert_async(title, text, flags=None):
    """
    在**独立守护线程**里弹消息框，立刻返回，不阻塞调用者。
    用于监控线程 / 后台静默模式 —— 它们绝不能被一个模态框卡住，
    否则重试、退出联动这些都会一起停摆。
    """
    t = threading.Thread(target=message_box, args=(title, text, flags),
                         name="giwifi-alert", daemon=True)
    t.start()
    return t


def run_hidden(cmd, timeout=10, text=True, encoding=None, errors="replace", **kw):
    """
    静默执行命令并返回 CompletedProcess。
    等价于 subprocess.run(..., capture_output=True)，但绝不会闪出黑框，
    并且能自动识别输出编码（见 decode_output）。
    需要固定编码时显式传 encoding="gbk" 之类即可。
    """
    kw.setdefault("capture_output", True)
    kwargs = hidden_kwargs()
    kwargs.update(kw)
    proc = subprocess.run(cmd, timeout=timeout, **kwargs)
    if not text:
        return proc
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        decode_output(proc.stdout, encoding, errors),
        decode_output(proc.stderr, encoding, errors))


def popen_detached(cmd, cwd=None, env=None):
    """静默启动一个后台进程（不阻塞、无窗口）"""
    if IS_WIN:
        flags = DETACHED_PROCESS | CREATE_NO_WINDOW
        return subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True,
                                creationflags=flags, startupinfo=_startupinfo())
    return subprocess.Popen(cmd, cwd=cwd, env=env, close_fds=True,
                            start_new_session=True)


def hide_own_console():
    """
    如果本进程是被控制台（python.exe / cmd）拉起来的，尝试把自己的控制台收掉。
    对没有控制台的进程（pythonw）调用是安全的空操作。
    """
    if not IS_WIN:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hwnd = k32.GetConsoleWindow()
        if not hwnd:
            return False                      # 本来就没有控制台
        k32.FreeConsole()
        return True
    except Exception:
        return False


def flash_console_window(hwnd_owner_name="GiWiFi自动登录"):
    """
    某些 Windows 终端会保留一个「幽灵」控制台窗口。
    这里主动把它隐藏掉（ShowWindow SW_HIDE），避免用户看到残留黑框。
    """
    if not IS_WIN:
        return False
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            u32.ShowWindow(hwnd, 0)           # SW_HIDE
            return True
    except Exception:
        pass
    return False


def pythonw_path():
    """返回同目录下的 pythonw.exe（找不到就返回 None）"""
    if not IS_WIN:
        return None
    exe = os.path.abspath(sys.executable)
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.isfile(cand) else None
