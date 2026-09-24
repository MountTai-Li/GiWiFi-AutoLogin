# -*- coding: utf-8 -*-
"""
纯 ctypes 实现的 Windows 系统托盘图标（零第三方依赖）。

为什么不直接用 pystray / Pillow：
本项目的原则是「拷到任何机器都能跑」，不想引入需要 pip 安装的依赖。
托盘只需要 shell32 的 Shell_NotifyIcon，自己包一层就够了。

用法::

    tray = TrayIcon(icon_path,
                    tooltip="GiWiFi 自动登录助手",
                    on_command=my_cmd_handler,      # 菜单项被点
                    on_activate=my_open_handler,    # 双击图标
                    menu_provider=my_menu_builder)  # 每次右键时动态取菜单
    tray.start()                       # 起线程跑消息循环
    tray.notify("标题", "内容")         # 弹气泡
    tray.update_tooltip("新提示")
    tray.set_visible(False)            # 临时把图标从托盘区撤掉（窗口和消息循环还在）
    tray.set_visible(True)             # 再放回去，可反复切换
    tray.stop()

要点：
* 窗口与消息循环**必须在同一个线程**里创建，所以 start() 会自己开线程。
* 窗口过程（WNDPROC）的回调在**托盘线程**里执行 —— 调用方要自己保证线程安全。
  GUI 场景下请把动作投递回主线程（例如队列 / ``root.after``）。
* ``menu_provider()`` 返回 ``[(key, label), ("---", None), (key, label)]``，
  ``key`` 是任意可哈希对象，点中后会原样回传给 ``on_command``。
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading

IS_WIN = sys.platform == "win32"

# ------------------------------------------------------------------ 常量
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_QUIT = 0x0012

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 0x01, 0x02, 0x03

MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_NONOTIFY = 0x0002, 0x0100, 0x0080

HWND_MESSAGE = -3
IMAGE_ICON = 1
LR_LOADFROMFILE, LR_DEFAULTSIZE, LR_SHARED = 0x0010, 0x0040, 0x8000
IDI_APPLICATION = 32512

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t,      # LRESULT
                             wt.HWND, wt.UINT,      # hWnd, msg
                             ctypes.c_size_t,       # WPARAM
                             ctypes.c_ssize_t)      # LPARAM


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HANDLE),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", ctypes.c_wchar * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wt.HANDLE),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HANDLE),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]


def _load_libs():
    u = ctypes.WinDLL("user32", use_last_error=True)
    s = ctypes.WinDLL("shell32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    return u, s, k


if IS_WIN:
    _user32, _shell32, _kernel32 = _load_libs()

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = ctypes.c_ushort
    _user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, wt.HINSTANCE]
    _user32.CreateWindowExW.argtypes = [
        wt.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HANDLE, wt.HINSTANCE, ctypes.c_void_p]
    _user32.CreateWindowExW.restype = wt.HWND
    _user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT,
                                       ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.DestroyWindow.argtypes = [wt.HWND]
    _user32.PostMessageW.argtypes = [wt.HWND, wt.UINT,
                                     ctypes.c_size_t, ctypes.c_ssize_t]
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND,
                                    wt.UINT, wt.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
    _user32.CreatePopupMenu.restype = wt.HANDLE
    _user32.AppendMenuW.argtypes = [wt.HANDLE, wt.UINT,
                                    ctypes.c_size_t, ctypes.c_wchar_p]
    _user32.DestroyMenu.argtypes = [wt.HANDLE]
    _user32.SetForegroundWindow.argtypes = [wt.HWND]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
    _user32.LoadImageW.argtypes = [wt.HINSTANCE, ctypes.c_wchar_p, wt.UINT,
                                   ctypes.c_int, ctypes.c_int, wt.UINT]
    _user32.LoadImageW.restype = wt.HANDLE
    _user32.LoadIconW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
    _user32.LoadIconW.restype = wt.HANDLE
    _user32.DestroyIcon.argtypes = [wt.HANDLE]
    _user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int,
                                          ctypes.c_ssize_t]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.TrackPopupMenu.argtypes = [
        wt.HANDLE, wt.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wt.HWND, ctypes.c_void_p]
    _user32.TrackPopupMenu.restype = ctypes.c_int

    _shell32.Shell_NotifyIconW.argtypes = [wt.DWORD,
                                           ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wt.BOOL

    _kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    _kernel32.GetModuleHandleW.restype = wt.HINSTANCE

    # 64 位下没有 SetWindowLongPtrW 的旧写法兼容；32 位系统没有该导出
    if not hasattr(_user32, "SetWindowLongPtrW"):
        _user32.SetWindowLongPtrW = _user32.SetWindowLongW
        _user32.GetWindowLongPtrW = _user32.GetWindowLongW


class TrayIcon:
    """Windows 系统托盘图标。创建失败时 available 为 False，调用方可降级。"""

    MENU_ID_BASE = 1000          # 菜单命令 ID 起点
    GWLP_USERDATA = -21

    def __init__(self, icon_path=None, tooltip="GiWiFi",
                 on_command=None, on_activate=None, menu_provider=None,
                 class_name=None):
        self.icon_path = icon_path
        self.tooltip = tooltip
        self.on_command = on_command
        self.on_activate = on_activate
        self.menu_provider = menu_provider
        self.class_name = class_name or ("GiWiFiTrayWnd_%d" % id(self))
        self.available = bool(IS_WIN)
        self.visible = False            # 图标当前是否真的在托盘区（见 set_visible）
        self._hwnd = None
        self._thread = None
        self._ready = threading.Event()
        self._wndproc = None            # 必须持引用，否则被 GC → 崩溃
        self._menu_keys = []            # 本次弹出的菜单：命令 ID -> key
        self._nid = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 对外接口
    def start(self, timeout=6.0):
        if not self.available:
            return False
        self._thread = threading.Thread(target=self._run, name="tray",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._hwnd is None:
            self.available = False
        return self.available

    def stop(self, timeout=4.0):
        if self._hwnd:
            try:
                _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout)
        self._hwnd = None
        self.visible = False

    def update_tooltip(self, text):
        with self._lock:
            self.tooltip = text
        self._modify(NIF_TIP)

    def set_visible(self, visible):
        """
        把托盘图标放上去 / 撤下来，**不销毁窗口和消息循环**，可以反复切换。

        用途：程序同时存在「后台常驻实例」和「控制面板窗口」时，
        两边各建一个图标会出现两个一模一样的 GiWiFi 图标。
        解决办法是后台实例在控制面板打开期间把自己的图标撤掉，
        窗口关掉后再放回来 —— 全程只有一个图标，且后台进程不受影响。

        返回 True 表示当前状态已经是期望值。
        """
        nid = self._nid
        if not nid or not self._hwnd:
            return False
        want = bool(visible)
        if self.visible == want:
            return True
        try:
            # 复位成基础标志位：notify() 会把 NIF_INFO 留在 uFlags 上，
            # 直接拿去 NIM_ADD 会让系统把旧的气泡内容再弹一遍
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.szTip = self.tooltip[:127]
            action = NIM_ADD if want else NIM_DELETE
            ok = bool(_shell32.Shell_NotifyIconW(action, ctypes.byref(nid)))
        except Exception:
            return False
        if ok:
            self.visible = want
        return ok

    def notify(self, title, text, flags=NIIF_INFO):
        """弹气泡通知"""
        nid = self._nid
        if not nid:
            return
        nid.szInfoTitle = title[:63]
        nid.szInfo = text[:255]
        nid.dwInfoFlags = flags
        self._modify(NIF_INFO)

    # ------------------------------------------------------------ 内部实现
    def _modify(self, flags):
        nid = self._nid
        if not nid or not self._hwnd:
            return
        try:
            nid.uFlags = flags | NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.szTip = self.tooltip[:127]
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def _make_nid(self, hwnd):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self.tooltip[:127]
        return nid

    def _load_icon(self):
        h = None
        if self.icon_path and os.path.isfile(self.icon_path):
            try:
                h = _user32.LoadImageW(None, self.icon_path, IMAGE_ICON,
                                       0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            except Exception:
                h = None
        if not h:
            # 退回到 exe 自身图标；再不行用系统默认图标
            try:
                if getattr(sys, "frozen", False):
                    h = _user32.LoadImageW(None, sys.executable, IMAGE_ICON,
                                           0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            except Exception:
                h = None
        if not h:
            h = _user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        return h

    def _run(self):
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc_cb
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = 0
            wc.hCursor = 0
            wc.hbrBackground = 0
            wc.lpszMenuName = None
            wc.lpszClassName = self.class_name
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                # 已注册（重名）也继续
                err = ctypes.get_last_error()
                if err not in (0, 1410):
                    self._ready.set()
                    return

            hwnd = _user32.CreateWindowExW(
                0, self.class_name, "GiWiFiTray", 0,
                0, 0, 0, 0, wt.HWND(HWND_MESSAGE), None, hinst, None)
            if not hwnd:
                self._ready.set()
                return
            self._hwnd = hwnd

            # 把 hwnd -> self 记进模块级字典，供窗口过程取回实例
            _WINDOWS[int(hwnd)] = self

            nid = self._make_nid(hwnd)
            self._nid = nid
            if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self._hwnd = None
                _user32.DestroyWindow(hwnd)
                self._ready.set()
                return
            self.visible = True
            self._ready.set()

            msg = wt.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))

            # 退出：删图标、销毁窗口
            if self.visible:
                try:
                    _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
                except Exception:
                    pass
                self.visible = False
            try:
                _user32.DestroyWindow(hwnd)
            except Exception:
                pass
            _WINDOWS.pop(int(hwnd), None)
        except Exception:
            self._ready.set()

    # ---------------------------------------------------------- 窗口过程
    @property
    def _wndproc_cb(self):
        if self._wndproc is None:
            self._wndproc = WNDPROC(self._on_message)
        return self._wndproc

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            ev = lparam & 0xFFFF
            if ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                self._popup_menu(hwnd)
            elif ev in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                self._fire(self.on_activate)
            return 0
        if msg == WM_CLOSE:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, ctypes.c_size_t(wparam),
                                      ctypes.c_ssize_t(lparam))

    def _popup_menu(self, hwnd):
        items = []
        if self.menu_provider:
            try:
                items = list(self.menu_provider() or [])
            except Exception:
                items = []
        if not items:
            return
        hmenu = _user32.CreatePopupMenu()
        self._menu_keys = []
        for key, label in items:
            if key is None or label is None:
                _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                continue
            cmd = self.MENU_ID_BASE + len(self._menu_keys)
            self._menu_keys.append(key)
            _user32.AppendMenuW(hmenu, MF_STRING, cmd, label)
        pt = wt.POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        _user32.SetForegroundWindow(hwnd)         # 不设的话菜单点了不消失
        choice = _user32.TrackPopupMenu(
            hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
            pt.x, pt.y, 0, hwnd, None)
        _user32.DestroyMenu(hmenu)
        if choice:
            idx = choice - self.MENU_ID_BASE
            if 0 <= idx < len(self._menu_keys):
                self._fire(self.on_command, self._menu_keys[idx])

    @staticmethod
    def _fire(fn, *a):
        if fn:
            try:
                fn(*a)
            except Exception:
                pass


_WINDOWS = {}       # hwnd(int) -> TrayIcon，供窗口过程取实例


def is_supported():
    return IS_WIN


if __name__ == "__main__":
    import time
    sys.stdout.reconfigure(encoding="utf-8")
    here = os.path.dirname(os.path.abspath(__file__))
    ico = os.path.join(here, "giwifi.ico")
    print("ICO 存在:", os.path.isfile(ico))
    log = []
    menu = [("打开主窗口", "open"), ("---", None), ("退出", "quit")]
    t = TrayIcon(ico, "GiWiFi 测试托盘（3 秒后自动消失）",
                 on_command=lambda k: log.append(("cmd", k)),
                 on_activate=lambda: log.append(("activate", None)),
                 menu_provider=lambda: menu)
    ok = t.start()
    print("托盘创建:", "成功" if ok else "失败")
    print("hwnd:", t._hwnd)
    t.notify("GiWiFi 自动登录助手", "托盘功能自检：图标已就绪")
    time.sleep(3)
    t.update_tooltip("提示已更新")
    time.sleep(1)
    t.stop()
    print("事件记录:", log)
    print("已清理，退出")
