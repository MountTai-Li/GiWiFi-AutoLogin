# -*- coding: utf-8 -*-
"""
portal_guard.py —— 拦截「连接 WiFi 时自动弹出的浏览器登录页」

为什么会弹
----------
Windows 的 NCSI（网络连接状态指示器）会周期性发一个**明文 HTTP** 探测：

    http://www.msftconnecttest.com/connecttest.txt   期望正文 "Microsoft Connect Test"

校园网网关会把这条明文请求劫持 / 重定向到认证门户，于是 Windows 判定
「这是一个强制门户（Captive Portal）」，然后**按设计立即打开浏览器**把登录页推到前台
（微软官方文档原文：Windows supports captive portal networks by immediately opening
the web browser when it detects a captive portal）。

本程序本来就能静默完成认证，所以这个弹窗纯属多余 —— 这里把它关掉。

怎么关（都写在 HKLM，**需要管理员权限**）
-----------------------------------------
1) 官方组策略项（推荐，微软文档里有）：
   HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\NetworkConnectivityStatusIndicator
       NoActiveProbe = 1        「关闭 NCSI 主动测试」
2) 服务参数（经典做法，两条一起写可覆盖各种系统版本）：
   HKLM\\SYSTEM\\CurrentControlSet\\Services\\NlaSvc\\Parameters\\Internet
       EnableActiveProbing = 0

两处都写；恢复默认就是把它们设回 0（或在界面上取消勾选）。

副作用（要如实告诉用户）
------------------------
* Windows 不再主动探测连通性 → 任务栏网络图标**可能不再显示「无 Internet」的感叹号**；
* 少数依赖 NCSI 判断联网状态的应用（Microsoft Store、天气等）可能判断不准、反应变慢；
* **不影响本程序**：它用的是自己的探测点（connect.rom.miui.com / msftconnecttest），
  和 NCSI 互不相干；
* 改完需要**重连网络或重启**后完全生效（NCSI 只在网络状态变化时才会重新判定）。
"""
import ctypes
import os
import sys
import winreg

IS_WIN = os.name == "nt"

POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Windows\NetworkConnectivityStatusIndicator"
NLA_KEY = r"SYSTEM\CurrentControlSet\Services\NlaSvc\Parameters\Internet"

POLICY_VALUE = "NoActiveProbe"          # 1 = 关闭 NCSI 主动测试
NLA_VALUE = "EnableActiveProbing"       # 0 = 关闭主动探测

NEED_ADMIN_MSG = "需要管理员权限"

if IS_WIN:
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)


# ------------------------------------------------------------------ 状态查询
def _read_dword(hklm, path, name):
    """读不到就返回 None（键/值不存在）"""
    if not IS_WIN:
        return None
    try:
        with winreg.OpenKey(hklm, path) as k:
            v, _t = winreg.QueryValueEx(k, name)
            return int(v)
    except Exception:
        return None


def probe_state():
    """
    返回当前状态字典（读注册表不需要管理员权限）：
        policy / nla  : 两个注册表值的原始值（None = 不存在）
        blocked       : 是否已经处于「拦截」状态
        admin         : 当前进程是否有管理员权限
    """
    policy = _read_dword(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY, POLICY_VALUE)
    nla = _read_dword(winreg.HKEY_LOCAL_MACHINE, NLA_KEY, NLA_VALUE)
    # NoActiveProbe=1 或 EnableActiveProbing=0 任一成立即视为已拦截
    blocked = (policy == 1) or (nla == 0)
    return {"policy": policy, "nla": nla, "blocked": blocked,
            "admin": is_admin()}


def is_blocked():
    return probe_state()["blocked"]


def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ------------------------------------------------------------------ 写入开关
def set_blocked(enable):
    """
    打开 / 关闭拦截。返回 (成功?, 给用户看的一句话)。

    普通权限下会失败并返回 NEED_ADMIN_MSG —— 调用方据此决定要不要提权重来。
    """
    if not IS_WIN:
        return False, "仅支持 Windows"
    try:
        if enable:
            k = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY, 0,
                                   winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, POLICY_VALUE, 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(k)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NLA_KEY, 0,
                                winreg.KEY_SET_VALUE) as k2:
                winreg.SetValueEx(k2, NLA_VALUE, 0, winreg.REG_DWORD, 0)
        else:
            k = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY, 0,
                                   winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, POLICY_VALUE, 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(k)
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, NLA_KEY, 0,
                                winreg.KEY_SET_VALUE) as k2:
                winreg.SetValueEx(k2, NLA_VALUE, 0, winreg.REG_DWORD, 1)
    except PermissionError:
        return False, NEED_ADMIN_MSG + "（写入 HKLM 注册表需要提权）"
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)

    # 回读校验，别只说"已写入"
    st = probe_state()
    if enable and not st["blocked"]:
        return False, "写入后校验失败（NoActiveProbe=%s, EnableActiveProbing=%s）" % (
            st["policy"], st["nla"])
    if not enable and st["blocked"]:
        return False, "恢复后校验失败（仍有开关处于关闭状态）"
    if enable:
        return True, ("已拦截：连接 WiFi 时不再自动弹出浏览器登录页。\n\n"
                      "· 重连一次 WiFi 或重启后完全生效\n"
                      "· 任务栏网络图标可能不再显示「无 Internet」感叹号\n"
                      "· 不影响本程序的自动认证")
    return True, "已恢复 Windows 默认行为（连接 WiFi 时会自动弹出浏览器登录页）"


def describe(short=False):
    """给界面 / 诊断输出用的一句话状态"""
    st = probe_state()
    if st["blocked"]:
        base = "已拦截" if short else "已拦截（连接 WiFi 时不再弹出浏览器登录页）"
    else:
        base = "未拦截" if short else "未拦截（连接 WiFi 时会自动弹出浏览器登录页）"
    return base


def relaunch_elevated(argv_tail):
    """
    以**管理员身份**重新启动本程序（会弹一次 UAC 授权框），
    并把 argv_tail 作为参数传过去，例如 ["--portal-guard", "on"]。

    返回 True 表示用户已授权并成功发起；False 表示被拒绝或发起失败。
    """
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = list(argv_tail)
    else:
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.isfile(exe):
            exe = sys.executable
        args = ["-S", os.path.abspath(_main_script())] + list(argv_tail)
    try:
        _shell32.ShellExecuteW.restype = ctypes.c_void_p
        _shell32.ShellExecuteW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                          ctypes.c_wchar_p, ctypes.c_wchar_p,
                                          ctypes.c_wchar_p, ctypes.c_int]
        # 用户点"否"会得到 <=32 的失败码；成功是 33 以上
        rc = _shell32.ShellExecuteW(None, "runas", exe, _cmdline(args), None, 1)
        return int(rc or 0) > 32
    except Exception:
        return False


def _cmdline(args):
    """按 Windows 命令行规则拼参数（带空格/中文的路径要加引号）"""
    return " ".join('"%s"' % a if (" " in a or "\t" in a) else a for a in args)


def _main_script():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "GiwifiAutoLogin.pyw")


# ------------------------------------------------------------------ 命令行入口
def _cli():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "status":
        st = probe_state()
        print("NoActiveProbe       =", st["policy"])
        print("EnableActiveProbing =", st["nla"])
        print("拦截浏览器登录页    :", describe())
        print("管理员权限          :", "有" if st["admin"] else "无")
        return 0
    if cmd in ("on", "off"):
        ok, msg = set_blocked(cmd == "on")
        print(("[OK] " if ok else "[失败] ") + msg)
        return 0 if ok else 1
    print("用法: python portal_guard.py [status|on|off]")
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
