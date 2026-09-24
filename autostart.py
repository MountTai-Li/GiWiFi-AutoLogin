# -*- coding: utf-8 -*-
"""
autostart.py —— 开机自启 & 快捷方式管理

本机环境限制（已踩坑）：
  * PowerShell 执行策略是 Restricted，.ps1 跑不了；
  * 沙箱会拦截 COM 实例化，WScript.Shell 用不了；
  所以统一用纯 Python 的 pylnk3 直接生成 .lnk 二进制文件。

开机自启的做法：往「启动」文件夹放一个指向 pythonw.exe 的 .lnk，
由 pythonw 启动主程序，全程无黑框。
"""
import os
import struct
import sys
import winreg

# 项目自带 vendor/pylnk3.py（LGPL，见 vendor/pylnk3-LICENSE.txt），
# 这样不装任何第三方包也能生成快捷方式。
_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "vendor")
if os.path.isdir(_VENDOR) and _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

try:
    import pylnk3
    _HAS_PYLNK3 = True
except Exception:
    _HAS_PYLNK3 = False

APP_NAME = "GiWiFi自动登录"
MAIN_SCRIPT = "GiwifiAutoLogin.pyw"

# ---------------------------------------------------- pylnk3 中文路径修复补丁
_ENTRY_IDS = {"FOLDER": 53, "FILE": 54}    # UNICODE 变体的 shitemid 类型号


def _patch_unicode_entry_type():
    """
    pylnk3 的 PathSegmentEntry.bytes 在把 self.type 加上 " (UNICODE)" 之前
    就取走了 entry_type，导致短名含非 ASCII 时写出的类型号仍是 ANSI 版，
    Windows 解析会错位 —— 这里把前两个字节改回 UNICODE 类型号。
    """
    if not _HAS_PYLNK3:
        return
    orig = pylnk3.PathSegmentEntry.bytes.fget

    def fixed(self):
        data = orig(self)
        if data is None:
            return None
        name = getattr(self, "short_name", None)
        if not isinstance(name, str):
            return data
        try:
            name.encode("ascii")
            return data
        except UnicodeEncodeError:
            pass
        base = "FOLDER" if str(self.type).startswith("FOLDER") else "FILE"
        return struct.pack("<H", _ENTRY_IDS[base]) + data[2:]

    pylnk3.PathSegmentEntry.bytes = property(fixed)


_patch_unicode_entry_type()


# ------------------------------------------------------------------ 路径工具
def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def pythonw_path():
    """找同目录下的 pythonw.exe（无控制台窗口），找不到就退回当前解释器"""
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.isfile(cand) else exe


def main_script():
    return os.path.join(app_dir(), MAIN_SCRIPT)


def icon_path():
    """
    快捷方式该用的图标。
    打包成 exe 后返回 None —— 让快捷方式直接用 exe 自身内嵌的图标，
    避免指向 PyInstaller 的临时解包目录（那是个会被清掉的路径）。
    """
    if getattr(sys, "frozen", False):
        return None
    p = os.path.join(app_dir(), "giwifi.ico")
    return p if os.path.isfile(p) else None


def startup_dir():
    return os.path.join(os.environ.get("APPDATA", ""),
                        r"Microsoft\Windows\Start Menu\Programs\Startup")


def desktop_dir():
    """取真实桌面路径（本机被重定向到 D:\\桌面，不能写死）"""
    for root, key in ((winreg.HKEY_CURRENT_USER,
                       r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"),
                      (winreg.HKEY_CURRENT_USER,
                       r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")):
        try:
            with winreg.OpenKey(root, key) as k:
                val, _ = winreg.QueryValueEx(k, "Desktop")
                p = os.path.expandvars(val)
                if os.path.isdir(p):
                    return p
        except Exception:
            pass
    p = os.path.join(os.path.expanduser("~"), "Desktop")
    return p if os.path.isdir(p) else app_dir()


def lnk_path(folder=None):
    return os.path.join(folder or startup_dir(), APP_NAME + ".lnk")


def exe_lnk_path():
    """打包成 exe 后的自启快捷方式路径"""
    return lnk_path()


# ------------------------------------------------------------------ 快捷方式
def make_lnk(output, target, arguments="", workdir=None, description="",
             icon=None, icon_index=0):
    """生成 .lnk 并回读校验；返回 (成功?, 说明)"""
    if not _HAS_PYLNK3:
        return False, "缺少 pylnk3（请用目录里的 安装.bat 或 pip install pylnk3）"
    if not os.path.isfile(target):
        return False, "目标不存在: %s" % target
    workdir = workdir or os.path.dirname(target)
    icon = icon or target
    try:
        os.makedirs(os.path.dirname(output), exist_ok=True)
        if os.path.exists(output):
            os.remove(output)
        pylnk3.for_file(target, lnk_name=output, arguments=arguments,
                        description=description or APP_NAME,
                        icon_file=icon, icon_index=icon_index, work_dir=workdir)
        back = pylnk3.parse(output)
        if os.path.normcase(back.path) != os.path.normcase(target):
            return False, "回读路径不一致: %s" % back.path
        if (back.arguments or "").strip() != arguments.strip():
            return False, "回读参数不一致: %r" % back.arguments
        return True, output
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def target_and_args(silent=True):
    """
    返回自启该用的 (可执行文件, 参数)。

    参数里固定带 **`-S`**：跳过 `site` 模块，实测省约 18 MB 工作集。
    本项目是纯标准库程序，不需要 site-packages，`-S` 不影响任何功能。
    （解释见主程序里的 `ensure_low_memory()`。）
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ("--silent" if silent else "")
    return pythonw_path(), '-S "%s"%s' % (main_script(), " --silent" if silent else "")


# ------------------------------------------------------------------ 开机自启
def is_enabled():
    return os.path.isfile(lnk_path())


def enable(silent=True):
    exe, args = target_and_args(silent)
    icon = icon_path()
    ok, msg = make_lnk(lnk_path(), exe, args, workdir=app_dir(),
                       description=APP_NAME + " - 开机自动认证校园网", icon=icon)
    return ok, msg


def disable():
    p = lnk_path()
    try:
        if os.path.isfile(p):
            os.remove(p)
        return True, "已关闭开机自启"
    except Exception as e:
        return False, str(e)


def create_desktop_shortcut(silent=False):
    out = os.path.join(desktop_dir(), APP_NAME + ".lnk")
    exe, args = target_and_args(silent)
    if not silent:
        args = args.replace(" --silent", "")           # 桌面图标打开控制面板
    icon = icon_path()
    ok, msg = make_lnk(out, exe, args, workdir=app_dir(),
                       description=APP_NAME, icon=icon)
    return ok, msg


def create_start_menu_shortcut(silent=False):
    out = os.path.join(os.environ.get("APPDATA", ""),
                       r"Microsoft\Windows\Start Menu\Programs", APP_NAME + ".lnk")
    exe, args = target_and_args(silent)
    if not silent:
        args = args.replace(" --silent", "")
    icon = icon_path()
    return make_lnk(out, exe, args, workdir=app_dir(),
                    description=APP_NAME, icon=icon)


# ------------------------------------------------------------------ 命令行入口
def _cli():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()

    if cmd == "status":
        print("程序目录 :", app_dir())
        print("解释器   :", pythonw_path())
        print("主程序   :", main_script(), "存在" if os.path.isfile(main_script()) else "缺失")
        print("启动文件夹:", startup_dir())
        print("桌面目录 :", desktop_dir())
        print("开机自启 :", "已开启 -> " + lnk_path() if is_enabled() else "未开启")
        return 0

    if cmd in ("install", "enable"):
        results = []
        results.append(("开机自启", enable(silent=True)))
        results.append(("桌面快捷方式", create_desktop_shortcut(silent=False)))
        results.append(("开始菜单", create_start_menu_shortcut(silent=False)))
        allok = True
        for name, (ok, msg) in results:
            allok &= ok
            print(("[OK]  " if ok else "[失败] ") + name + ": " + str(msg))
        print("\n" + ("全部完成 ✅" if allok else "部分失败 ❌"))
        return 0 if allok else 1

    if cmd in ("uninstall", "disable"):
        targets = [
            ("开机自启", lnk_path()),
            ("桌面快捷方式", os.path.join(desktop_dir(), APP_NAME + ".lnk")),
            ("开始菜单", os.path.join(os.environ.get("APPDATA", ""),
                                      r"Microsoft\Windows\Start Menu\Programs",
                                      APP_NAME + ".lnk")),
        ]
        for name, path in targets:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    print("[OK]  %s: 已删除 %s" % (name, path))
                else:
                    print("[--]  %s: 本来就不存在" % name)
            except Exception as e:
                print("[失败] %s: %s" % (name, e))
        return 0

    print("用法: python autostart.py [status|install|uninstall]")
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
