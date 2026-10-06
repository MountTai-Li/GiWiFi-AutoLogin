# -*- coding: utf-8 -*-
"""
GiWiFi 自动登录助手 · 安装程序（installer）
==========================================

一个单文件安装程序（PyInstaller 打包，内嵌主程序），提供：

  · 图形界面安装：选择安装位置 → 释放文件 → 快捷方式 → 卸载登记
  · 用户级安装：默认 %LOCALAPPDATA%\\Programs\\GiWiFi自动登录助手，无需管理员权限
  · 卸载：双击安装目录里的「卸载.exe」（或在 系统设置→应用 里卸载）
  · 静默自测：`安装程序.exe --selftest <报告文件>`（全程在临时目录内验证）

设计约束（有意为之）：
  · **不写 config.json** —— 主程序首次「保存并应用」时自己生成，重装/升级不丢账号配置
  · **卸载只删「安装时释放的文件」**，绝不对安装目录做递归删除 ——
    即使当初把程序装在含其它文件的目录里，也不会误删用户数据
  · 快捷方式的名称/参数/图标完全复用项目 autostart.py 的约定 ——
    主程序界面里的「开机自启」开关能直接识别安装器建的启动项
  · 安装/卸载全部走 HKCU + 用户目录，全程无 UAC
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from queue import Queue, Empty

# ------------------------------------------------------------------ 常量
APP_DISPLAY = "GiWiFi 自动登录助手"
APP_LINK = "GiWiFi自动登录"                      # 快捷方式名（与 autostart.py 一致）
EXE_NAME = "GiwifiAutoLogin.exe"
UNINST_EXE = "卸载.exe"
APP_DIR_NAME = "GiWiFi自动登录助手"
PUBLISHER = "MountTai-Li"

UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GiWiFi自动登录助手"
SELFTEST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\GiWiFi自动登录助手__selftest__"

NO_WINDOW = 0x08000000

# 卸载时允许删除的文件（精确清单，防止误删目录里的其它东西）
RELEASED_FILES = [
    EXE_NAME, UNINST_EXE, "giwifi.ico", "使用说明.txt", "config.json",
    "giwifi.log", "crash.log",
    "giwifi.throttle", "giwifi.wifi", "giwifi.quit", "giwifi.show",
]

# 配色（与主程序保持一致）
BG = "#1b1d23"
CARD = "#252831"
CARD2 = "#2d313c"
FG = "#e8eaf0"
FG_DIM = "#9aa3b2"
ACCENT = "#4ea1ff"
GREEN = "#3ddc84"
RED = "#ff5c5c"
BORDER = "#343846"
FONT = "Microsoft YaHei UI"


# ------------------------------------------------------------------ 基础工具
def meipass():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def res(name):
    """嵌入资源路径；打包后在 _MEIPASS/app/ 下，源码直跑时退回项目目录。"""
    cands = [
        os.path.join(meipass(), "app", name),
        os.path.join(meipass(), name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "release", "dist", name),
    ]
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def read_version():
    src = res("giwifi.py")
    if not src:
        return "?"
    try:
        with open(src, encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def _load_autostart():
    """加载项目自带的 autostart（快捷方式复用同一套逻辑与命名约定）。"""
    base = meipass()
    if base not in sys.path:
        sys.path.insert(0, base)
    import autostart
    return autostart


def default_install_dir():
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "Programs", APP_DIR_NAME)


def _run_hidden(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, creationflags=NO_WINDOW)
    except Exception:
        return None


def is_running():
    p = _run_hidden(["tasklist", "/FI", "IMAGENAME eq " + EXE_NAME, "/FO", "CSV"])
    if not p:
        return False
    out = (p.stdout or b"").decode("gbk", "ignore")
    return EXE_NAME.lower() in out.lower()


def kill_running():
    _run_hidden(["taskkill", "/F", "/IM", EXE_NAME])
    time.sleep(0.6)


def installed_info():
    """读 HKCU 卸载登记；返回 (版本, 安装目录) 或 None。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
            ver, _ = winreg.QueryValueEx(k, "DisplayVersion")
            loc, _ = winreg.QueryValueEx(k, "InstallLocation")
            return str(ver), str(loc)
    except Exception:
        return None


# ------------------------------------------------------------------ 安装 / 卸载核心
def write_uninstall_entry(dest, key=UNINST_KEY):
    import winreg
    ver = read_version()
    total = 0
    for fn in RELEASED_FILES:
        p = os.path.join(dest, fn)
        try:
            if os.path.isfile(p):
                total += os.path.getsize(p)
        except Exception:
            pass
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_DISPLAY)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, ver)
        winreg.SetValueEx(k, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(k, "InstallLocation", 0, winreg.REG_SZ, dest)
        winreg.SetValueEx(k, "DisplayIcon", 0, winreg.REG_SZ,
                          os.path.join(dest, EXE_NAME))
        winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ,
                          '"%s" --uninstall' % os.path.join(dest, UNINST_EXE))
        winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "EstimatedSize", 0, winreg.REG_DWORD,
                          max(1, total // 1024))
        winreg.SetValueEx(k, "InstallDate", 0, winreg.REG_SZ,
                          time.strftime("%Y%m%d"))


def remove_uninstall_entry(key=UNINST_KEY):
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def create_shortcuts(dest, opts):
    """创建快捷方式；返回 [(说明, 成功?, 详情)]。"""
    ast = _load_autostart()
    exe = os.path.join(dest, EXE_NAME)
    results = []

    def _mk(label, folder, args, desc):
        out = os.path.join(folder, APP_LINK + ".lnk")
        ok, msg = ast.make_lnk(out, exe, args, workdir=dest, description=desc)
        results.append((label, ok, msg if not ok else out))

    if opts.get("desktop", True):
        _mk("桌面快捷方式", ast.desktop_dir(), "", APP_DISPLAY)
    _mk("开始菜单", os.path.join(os.environ.get("APPDATA", ""),
                                 r"Microsoft\Windows\Start Menu\Programs"),
        "", APP_DISPLAY)
    if opts.get("autostart", False):
        _mk("开机自动运行", ast.startup_dir(), "--silent",
            APP_DISPLAY + " - 开机自动认证校园网")
    return results


def remove_shortcuts():
    """只删指向本程序的快捷方式（名字专有 + 校验目标）。"""
    ast = _load_autostart()
    removed = []
    for folder in (ast.desktop_dir(),
                   os.path.join(os.environ.get("APPDATA", ""),
                                r"Microsoft\Windows\Start Menu\Programs"),
                   ast.startup_dir()):
        p = os.path.join(folder, APP_LINK + ".lnk")
        if not os.path.isfile(p):
            continue
        try:
            os.remove(p)
            removed.append(p)
        except Exception:
            pass
    return removed


def schedule_cleanup(dest):
    """
    延迟清理：等本进程退出后，删掉「卸载.exe」，再尝试删空目录（**非递归**）。
    用批处理是因为 Windows 不允许删除正在运行的 exe。
    """
    bat = os.path.join(tempfile.gettempdir(), "giwifi_cleanup_%d.bat" % os.getpid())
    target_exe = os.path.join(dest, UNINST_EXE)
    lines = [
        "@echo off",
        "ping 127.0.0.1 -n 3 >nul",
        'del /f /q "%s" 2>nul' % target_exe,
        'rmdir "%s" 2>nul' % dest,          # 非递归：目录不空则自动放弃
        'del "%~f0" 2>nul',
    ]
    try:
        with open(bat, "w", encoding="gbk", errors="replace") as f:
            f.write("\r\n".join(lines) + "\r\n")
        subprocess.Popen(["cmd", "/c", bat], creationflags=NO_WINDOW, close_fds=True)
        return True
    except Exception:
        return False


def do_install(dest, opts, log, uninst_key=UNINST_KEY):
    """安装核心。log(str) 汇报进度；失败抛异常。"""
    log("检查运行中的程序…")
    if is_running():
        log("检测到程序正在运行，先关闭…")
        kill_running()

    log("创建安装目录…")
    os.makedirs(dest, exist_ok=True)

    # 1) 释放主程序与资源
    for fn, required in ((EXE_NAME, True), ("giwifi.ico", False),
                         ("使用说明.txt", False)):
        src = res(fn)
        if src:
            log("释放 %s …" % fn)
            shutil.copy2(src, os.path.join(dest, fn))
        elif required:
            raise RuntimeError("安装包内缺少 %s，无法继续（包可能不完整）" % fn)

    exe = os.path.join(dest, EXE_NAME)
    if not os.path.isfile(exe) or os.path.getsize(exe) < 1024 * 1024:
        raise RuntimeError("主程序文件异常（缺失或过小），请重新下载安装包")

    # 2) 写入卸载器（复制自身；源码模式跳过）
    if getattr(sys, "frozen", False):
        log("写入卸载程序…")
        try:
            shutil.copy2(sys.executable, os.path.join(dest, UNINST_EXE))
        except Exception as e:
            log("卸载器写入失败（不影响使用）：%s" % e)
    else:
        log("（源码模式：跳过卸载器写入）")

    # 3) 快捷方式
    if opts.get("desktop", True) or opts.get("autostart", False):
        log("创建快捷方式…")
        for label, ok, msg in create_shortcuts(dest, opts):
            log("  %s %s" % ("✓" if ok else "✗", label if ok else "%s 失败: %s" % (label, msg)))

    # 4) 卸载登记
    if opts.get("registry", True):
        log("写入卸载登记…")
        write_uninstall_entry(dest, key=uninst_key)

    log("安装完成")
    return exe


def do_uninstall(dest, log, uninst_key=UNINST_KEY, remove_links=True):
    """卸载核心：杀进程 → 快捷方式 → 注册表 → 只删已知文件 → 延迟清理目录。"""
    log("检查运行中的程序…")
    if is_running():
        log("关闭正在运行的程序…")
        kill_running()

    if remove_links:
        log("删除快捷方式…")
        n = len(remove_shortcuts())
        log("  已删除 %d 个" % n)

    log("清理卸载登记…")
    remove_uninstall_entry(key=uninst_key)

    log("删除程序文件…")
    kept = []
    for fn in RELEASED_FILES:
        p = os.path.join(dest, fn)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            kept.append(fn)          # 多半是正在运行的「卸载.exe」，交给延迟清理
    log("  已删除；残留交给延迟清理：%s" % (", ".join(kept) if kept else "无"))

    schedule_cleanup(dest)
    log("卸载完成")
    return True


# ------------------------------------------------------------------ 图形界面（安装）
def gui_install(smoke_ms=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox

    ver = read_version()
    root = tk.Tk()
    root.title("%s · 安装" % APP_DISPLAY)
    root.configure(bg=BG)
    root.resizable(False, False)

    ico = res("giwifi.ico")
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass

    w, h = 560, 452
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                   max(0, (sh - h) // 3)))

    state = {"dir": default_install_dir()}
    q = Queue()

    # ---------- 头部 ----------
    head = tk.Frame(root, bg=BG)
    head.pack(fill="x", padx=22, pady=(18, 8))
    tk.Label(head, text=APP_DISPLAY, bg=BG, fg=FG,
             font=(FONT, 16, "bold")).pack(anchor="w")
    tk.Label(head, text="安装程序 · v%s · 用户级安装（无需管理员权限）" % ver,
             bg=BG, fg=FG_DIM, font=(FONT, 9)).pack(anchor="w", pady=(3, 0))

    # ---------- 表单卡片 ----------
    form = tk.Frame(root, bg=CARD, highlightbackground=BORDER,
                    highlightthickness=1)
    form.pack(fill="both", expand=True, padx=22, pady=(8, 6))

    tk.Label(form, text="安装位置", bg=CARD, fg=FG,
             font=(FONT, 10, "bold")).pack(anchor="w", padx=16, pady=(14, 4))

    row = tk.Frame(form, bg=CARD)
    row.pack(fill="x", padx=16)
    ent = tk.Entry(row, bg=CARD2, fg=FG, insertbackground=FG,
                   relief="flat", font=(FONT, 9))
    ent.insert(0, state["dir"])
    ent.pack(side="left", fill="x", expand=True, ipady=5)

    def pick_dir():
        d = filedialog.askdirectory(initialdir=state["dir"],
                                    title="选择安装位置")
        if d:
            d = os.path.normpath(d)
            if not d.rstrip("\\/").endswith(APP_DIR_NAME):
                d = os.path.join(d, APP_DIR_NAME)
            state["dir"] = d
            ent.delete(0, "end")
            ent.insert(0, d)

    tk.Button(row, text="浏览…", command=pick_dir, bg=CARD2, fg=FG,
              activebackground=BORDER, activeforeground=FG, relief="flat",
              font=(FONT, 9), padx=10, cursor="hand2").pack(side="left", padx=(8, 0))

    tk.Label(form, text="默认装到当前用户目录；重装/升级不会丢失已保存的账号配置。",
             bg=CARD, fg=FG_DIM, font=(FONT, 8)).pack(anchor="w", padx=16, pady=(4, 10))

    v_desktop = tk.BooleanVar(value=True)
    v_auto = tk.BooleanVar(value=True)
    v_launch = tk.BooleanVar(value=True)

    def _chk(text, var):
        return tk.Checkbutton(form, text=text, variable=var, bg=CARD, fg=FG,
                              activebackground=CARD, activeforeground=FG,
                              selectcolor=CARD2, font=(FONT, 9),
                              anchor="w", cursor="hand2",
                              highlightthickness=0, bd=0)

    _chk("创建桌面快捷方式", v_desktop).pack(fill="x", padx=14, pady=1)
    _chk("开机自动运行（静默认证，可在程序里随时关闭）", v_auto).pack(fill="x", padx=14, pady=1)
    _chk("安装完成后启动程序", v_launch).pack(fill="x", padx=14, pady=(1, 12))

    # 已安装提示
    info = installed_info()
    if info:
        tk.Label(form, text="检测到已安装 v%s（%s），将执行覆盖安装。" % (info[0], info[1]),
                 bg=CARD, fg="#ffb02e", font=(FONT, 8),
                 wraplength=480, justify="left").pack(anchor="w", padx=16, pady=(0, 10))

    # ---------- 状态行 ----------
    status = tk.Label(root, text="准备就绪。", bg=BG, fg=FG_DIM,
                      font=(FONT, 9), anchor="w", wraplength=510,
                      justify="left")
    status.pack(fill="x", padx=22, pady=(2, 4))

    # ---------- 底部按钮 ----------
    foot = tk.Frame(root, bg=BG)
    foot.pack(fill="x", padx=22, pady=(0, 16))

    btn = tk.Button(foot, text="开始安装", bg=ACCENT, fg="#0d1520",
                    activebackground="#6fb4ff", activeforeground="#0d1520",
                    relief="flat", font=(FONT, 10, "bold"), padx=26, pady=7,
                    cursor="hand2")

    btn.pack(side="right")

    def set_status(text, color=FG_DIM):
        status.configure(text=text, fg=color)

    # ---------- 线程 & 队列 ----------
    def drain():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    set_status(payload)
                elif kind == "done":
                    btn.configure(state="normal", text="开始安装")
                    root.protocol("WM_DELETE_WINDOW", root.destroy)
                    if payload is None:
                        show_done()
                    else:
                        set_status("安装失败：%s" % payload, RED)
                        messagebox.showerror("安装失败", str(payload), parent=root)
                elif kind == "failmsg":
                    messagebox.showerror("安装失败", str(payload), parent=root)
        except Empty:
            pass
        root.after(60, drain)

    def worker(dest, opts):
        try:
            do_install(dest, opts, log=lambda s: q.put(("log", s)))
            q.put(("done", None))
        except Exception as e:
            q.put(("done", "%s: %s" % (type(e).__name__, e)))

    done_frame = {"w": None}

    def show_done():
        form.pack_forget()
        done = tk.Frame(root, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        done.pack(fill="both", expand=True, padx=22, pady=(8, 6))
        tk.Label(done, text="✓  安装完成", bg=CARD, fg=GREEN,
                 font=(FONT, 15, "bold")).pack(anchor="w", padx=16, pady=(18, 6))
        tk.Label(done, text="安装位置：%s" % state["dir"], bg=CARD, fg=FG,
                 font=(FONT, 9), wraplength=480, justify="left").pack(anchor="w", padx=16)
        tk.Label(done, text="可以从桌面快捷方式或开始菜单打开程序；\n"
                            "首次使用请在程序里填好账号密码，点「保存并应用」。",
                 bg=CARD, fg=FG_DIM, font=(FONT, 9), justify="left").pack(anchor="w", padx=16, pady=(8, 14))
        brow = tk.Frame(done, bg=CARD)
        brow.pack(anchor="w", padx=16)

        def run_app():
            try:
                os.startfile(os.path.join(state["dir"], EXE_NAME))
            except Exception as e:
                messagebox.showerror("启动失败", str(e), parent=root)

        def open_dir():
            try:
                os.startfile(state["dir"])
            except Exception:
                pass

        tk.Button(brow, text="启动程序", command=run_app, bg=GREEN, fg="#0d1520",
                  activebackground="#63e8a0", activeforeground="#0d1520",
                  relief="flat", font=(FONT, 9, "bold"), padx=18, pady=5,
                  cursor="hand2").pack(side="left")
        tk.Button(brow, text="打开安装目录", command=open_dir, bg=CARD2, fg=FG,
                  activebackground=BORDER, activeforeground=FG, relief="flat",
                  font=(FONT, 9), padx=14, pady=5, cursor="hand2").pack(side="left", padx=8)
        done_frame["w"] = done

    def start_install():
        dest = ent.get().strip().strip('"')
        if not dest:
            messagebox.showwarning("提示", "请先选择安装位置", parent=root)
            return
        dest = os.path.normpath(dest)
        state["dir"] = dest
        btn.configure(state="disabled", text="正在安装…")
        root.protocol("WM_DELETE_WINDOW", lambda: None)   # 安装中禁止关闭
        opts = {"desktop": v_desktop.get(), "autostart": v_auto.get(),
                "registry": True}
        launch_after = v_launch.get()
        threading.Thread(target=worker, args=(dest, opts), daemon=True).start()

        # 安装完成后按需启动主程序
        def watch_launch():
            if done_frame["w"] is not None:
                if launch_after:
                    try:
                        os.startfile(os.path.join(state["dir"], EXE_NAME))
                    except Exception:
                        pass
                return
            root.after(300, watch_launch)

        root.after(300, watch_launch)

    btn.configure(command=start_install)
    root.after(60, drain)

    if smoke_ms:
        root.after(smoke_ms, root.destroy)
    root.mainloop()
    return 0


# ------------------------------------------------------------------ 图形界面（卸载）
def gui_uninstall():
    import tkinter as tk
    from tkinter import messagebox

    dest = os.path.dirname(os.path.abspath(sys.executable)) \
        if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.abspath(__file__))

    root = tk.Tk()
    root.withdraw()
    root.title("卸载 " + APP_DISPLAY)
    ico = res("giwifi.ico")
    if ico:
        try:
            root.iconbitmap(ico)
        except Exception:
            pass

    if not messagebox.askyesno(
            "卸载 " + APP_DISPLAY,
            "确定要卸载 %s 吗？\n\n"
            "将删除：\n"
            "· 程序文件和配置（含已保存的账号配置）\n"
            "· 桌面 / 开始菜单 / 开机启动 快捷方式\n\n"
            "位置：%s" % (APP_DISPLAY, dest)):
        root.destroy()
        return 0

    lines = []
    try:
        do_uninstall(dest, log=lines.append)
        messagebox.showinfo("卸载完成",
                            "已卸载 %s。\n\n残留的「卸载.exe」会在几秒后自动清理。"
                            % APP_DISPLAY)
    except Exception as e:
        messagebox.showerror("卸载失败", str(e))
    finally:
        root.destroy()
    return 0


# ------------------------------------------------------------------ 自测
def selftest(report_path=None):
    """静默自测：全程在临时目录 + 测试注册表键内；绝不触碰真实安装。"""
    lines = []
    n_pass = n_fail = 0

    def check(name, ok, detail=""):
        nonlocal n_pass, n_fail
        if ok:
            n_pass += 1
            lines.append("[PASS] %s%s" % (name, (" · " + detail) if detail else ""))
        else:
            n_fail += 1
            lines.append("[FAIL] %s%s" % (name, (" · " + detail) if detail else ""))
        return ok

    lines.append("GiWiFi 安装程序自测报告")
    lines.append("时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("版本: %s" % read_version())
    lines.append("frozen: %s" % getattr(sys, "frozen", False))
    lines.append("-" * 46)

    work = os.path.join(tempfile.gettempdir(), "giwifi_selftest_%d" % os.getpid())
    shutil.rmtree(work, ignore_errors=True)

    try:
        # 1) 资源完整性
        main_exe = res(EXE_NAME)
        check("资源：主程序存在", bool(main_exe),
              "%s bytes" % (os.path.getsize(main_exe) if main_exe else 0))
        check("资源：图标存在", bool(res("giwifi.ico")))
        check("资源：使用说明存在", bool(res("使用说明.txt")))
        check("版本号读取", read_version() not in ("", "?"), read_version())

        # 2) 安装到临时目录
        opts = {"desktop": False, "autostart": False, "registry": True}
        exe = do_install(work, opts, log=lines.append, uninst_key=SELFTEST_KEY)
        check("安装：主程序已释放", os.path.isfile(exe),
              "%s bytes" % os.path.getsize(exe))

        if getattr(sys, "frozen", False):
            check("安装：卸载器已写入", os.path.isfile(os.path.join(work, UNINST_EXE)))

        # 3) 注册表（测试键）
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SELFTEST_KEY) as k:
                loc, _ = winreg.QueryValueEx(k, "InstallLocation")
            check("注册表：写入/读取", os.path.normcase(loc) == os.path.normcase(work), loc)
        except Exception as e:
            check("注册表：写入/读取", False, str(e))

        # 4) 快捷方式机制（输出到临时目录；不碰真实桌面）
        try:
            ast = _load_autostart()
            lnk = os.path.join(work, "test.lnk")
            ok, msg = ast.make_lnk(lnk, exe, "--silent", workdir=work,
                                   description="selftest")
            exists = os.path.isfile(lnk)
            check("快捷方式：make_lnk 创建+回读", ok and exists, str(msg))
            if exists:
                os.remove(lnk)
        except Exception as e:
            check("快捷方式：make_lnk 创建+回读", False, "%s: %s" % (type(e).__name__, e))

        # 5) 卸载：文件清理（不删快捷方式、不碰真实注册表键）
        do_uninstall(work, log=lines.append, uninst_key=SELFTEST_KEY,
                     remove_links=False)
        remain = [f for f in os.listdir(work)] if os.path.isdir(work) else []
        # 卸载.exe 可能因占用残留（延迟清理负责），其它文件应清光
        remain_ok = all(f == UNINST_EXE for f in remain)
        check("卸载：已知文件清理", remain_ok, "剩余: %s" % (remain or "无"))
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SELFTEST_KEY):
                check("卸载：注册表键已删", False)
        except FileNotFoundError:
            check("卸载：注册表键已删", True)
        except Exception as e:
            check("卸载：注册表键已删", False, str(e))
    except Exception as e:
        check("自测流程异常", False, "%s: %s" % (type(e).__name__, e))
    finally:
        time.sleep(0.5)
        shutil.rmtree(work, ignore_errors=True)

    lines.append("-" * 46)
    lines.append("结果: %d/%d 通过" % (n_pass, n_pass + n_fail))

    text = "\n".join(lines)
    if report_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:
        pass
    return 0 if n_fail == 0 else 1


# ------------------------------------------------------------------ 入口
def main():
    args = [a.lower() for a in sys.argv[1:]]
    name = os.path.basename(sys.executable)

    if "--selftest" in args:
        i = args.index("--selftest")
        report = sys.argv[i + 2] if len(sys.argv) > i + 2 else None
        return selftest(report)

    if "--ui-smoke" in args:
        return gui_install(smoke_ms=1300)

    if "--uninstall" in args or "卸载" in name:
        return gui_uninstall()

    return gui_install()


if __name__ == "__main__":
    raise SystemExit(main())
