# -*- coding: utf-8 -*-
"""
make_installer.py —— 构建单文件「安装程序」（内嵌主程序）
========================================================

用法：
    python make_installer.py                     # 自动找主程序 exe，输出到 release/installer/
    python make_installer.py <主程序exe> [输出目录]

前置：
    · 主程序 exe 已构建（先跑 make_release.py，或单独用 PyInstaller 构建）；
      默认从 release/dist/GiwifiAutoLogin.exe 读取。
    · 当前解释器装有 PyInstaller。

产物：
    <输出目录>/GiWiFi自动登录助手-安装程序-v<版本>.exe
        —— 用户双击即可图形化安装（释放文件 / 快捷方式 / 卸载登记），
           安装目录里的「卸载.exe」支持完整卸载。
"""
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE_NAME = "GiwifiAutoLogin.exe"


def log(msg):
    print(msg, flush=True)


def read_version():
    with open(os.path.join(HERE, "giwifi.py"), encoding="utf-8") as f:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        raise SystemExit("无法从 giwifi.py 读取版本号")
    return m.group(1)


def find_main_exe():
    for p in (os.path.join(HERE, "release", "dist", EXE_NAME),
              os.path.join(HERE, "release", "GiWiFi自动登录助手", EXE_NAME)):
        if os.path.isfile(p):
            return p
    return None


def build(main_exe=None, out_dir=None):
    # 前置检查：缺 tkinter 的解释器会打包出打不开界面的程序（2026-10-06 踩过）
    try:
        import tkinter  # noqa: F401
    except Exception:
        raise SystemExit("❌ 当前解释器缺少 tkinter，安装程序界面无法打包。\n"
                         "   请改用带 tkinter 的完整版 Python"
                         "（如 Python Install Manager 的 pythoncore / 官方安装版）。")
    ver = read_version()
    main_exe = main_exe or find_main_exe()
    if not main_exe or not os.path.isfile(main_exe):
        raise SystemExit("找不到主程序 exe：请先运行 make_release.py，"
                         "或把 exe 路径作为参数传入。")
    out_dir = out_dir or os.path.join(HERE, "release", "installer")
    build_root = os.path.join(HERE, "release", "installer-build")
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(build_root, exist_ok=True)

    dist = os.path.join(build_root, "dist")
    work = os.path.join(build_root, "work-%d" % int(time.time()))
    log("[1/2] PyInstaller 打包安装程序…（内嵌主程序 %.1f MB）"
        % (os.path.getsize(main_exe) / 1048576))

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--onefile", "--noconsole",
           "--name", "GiWiFiSetup",
           "--icon", os.path.join(HERE, "giwifi.ico"),
           "--distpath", dist, "--workpath", work, "--specpath", build_root,
           "--add-data", main_exe + ";app",
           "--add-data", os.path.join(HERE, "giwifi.ico") + ";app",
           "--add-data", os.path.join(HERE, "使用说明.txt") + ";app",
           "--add-data", os.path.join(HERE, "giwifi.py") + ";.",
           "--add-data", os.path.join(HERE, "autostart.py") + ";.",
           "--add-data", os.path.join(HERE, "vendor") + ";vendor",
           os.path.join(HERE, "installer.py")]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        log(p.stdout[-3000:])
        log(p.stderr[-3000:])
        raise SystemExit("PyInstaller 失败（检查 pyinstaller 是否可用）")

    built = os.path.join(dist, "GiWiFiSetup.exe")
    if not os.path.isfile(built):
        raise SystemExit("没找到构建产物: " + built)

    final = os.path.join(out_dir, "GiWiFi自动登录助手-安装程序-v%s.exe" % ver)
    tmp = final + ".tmp"
    shutil.copy2(built, tmp)
    os.replace(tmp, final)          # 原子替换，避免"同名删建"类问题
    log("[2/2] 完成 ✅  %s  (%.1f MB)"
        % (final, os.path.getsize(final) / 1048576))
    return final


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    args = sys.argv[1:]
    build(args[0] if len(args) > 0 else None,
          args[1] if len(args) > 1 else None)
