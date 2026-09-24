# -*- coding: utf-8 -*-
"""
make_release.py —— 一键生成给别人用的发布压缩包

用法：
    python make_release.py                     # 输出到桌面
    python make_release.py "D:\\somewhere"      # 输出到指定目录

它会做四件事：
    1) 调 PyInstaller 把主程序打成**单文件 exe**（目标机器无需装 Python）
    2) 组装发布目录：exe + 干净配置 + 图标 + 说明 + 源码备用
    3) **安全检查**：确保包里的 config.json 账号/密码为空，
       且全文搜不到你本机真实配置里的账号与密码密文，也不会混进日志
    4) 打成 zip

前置：需要一个装了 pyinstaller 的解释器
      （pip install pyinstaller；本机是 venv 里的那个 Python）
"""
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "GiWiFi自动登录助手"
EXE_NAME = "GiwifiAutoLogin"

# 发布包里要带的源码文件（放「源码-备用」目录，exe 被安全软件拦住时可用）
SRC_FILES = ["GiwifiAutoLogin.pyw", "giwifi.py", "aes128.py", "monitor.py",
             "secure_store.py", "winutil.py", "wlanapi.py", "tray.py", "autostart.py",
             "make_icon.py", "make_release.py", "giwifi.ico", "启动.bat",
             "打包exe.bat", "README.md", "使用说明.txt"]
VENDOR_FILES = ["pylnk3.py", "pylnk3-LICENSE.txt"]


def log(msg):
    print(msg, flush=True)


def clean_config() -> dict:
    """生成不含任何个人信息的配置模板"""
    sys.path.insert(0, HERE)
    import giwifi
    cfg = dict(giwifi.DEFAULT_CONFIG)
    cfg["username"] = ""
    cfg["password_enc"] = ""
    cfg["wifi_ssid"] = ""
    cfg["wifi_profile"] = ""
    return cfg


def build_exe(out_dir: str):
    """用 PyInstaller 打单文件 exe；返回 exe 路径"""
    dist = os.path.join(out_dir, "dist")
    work = os.path.join(out_dir, "work-%d" % int(time.time()))
    log("[1/4] PyInstaller 打包中…（约 20 秒）")
    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--onefile", "--noconsole",
           "--name", EXE_NAME,
           "--icon", os.path.join(HERE, "giwifi.ico"),
           # ⚠ --add-data 的路径是相对 .spec 目录解析的，必须写绝对路径
           "--add-data", os.path.join(HERE, "vendor") + ";vendor",
           "--add-data", os.path.join(HERE, "giwifi.ico") + ";.",
           "--distpath", dist, "--workpath", work, "--specpath", out_dir,
           os.path.join(HERE, "GiwifiAutoLogin.pyw")]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        log(p.stdout[-3000:])
        log(p.stderr[-3000:])
        raise SystemExit("PyInstaller 失败（是否装了 pyinstaller？）")
    exe = os.path.join(dist, EXE_NAME + ".exe")
    if not os.path.isfile(exe):
        raise SystemExit("没找到生成的 exe: " + exe)
    log("      -> %s  (%.1f MB)" % (exe, os.path.getsize(exe) / 1048576))
    return exe


def assemble(stage: str, exe: str):
    log("[2/4] 组装发布目录…")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    src_dir = os.path.join(stage, "源码-备用")
    os.makedirs(src_dir)

    clean = clean_config()
    shutil.copy(exe, stage)
    shutil.copy(os.path.join(HERE, "giwifi.ico"), stage)
    for f in ("README.md", "使用说明.txt"):
        p = os.path.join(HERE, f)
        if os.path.isfile(p):
            shutil.copy(p, stage)
    with open(os.path.join(stage, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)

    for f in SRC_FILES:
        p = os.path.join(HERE, f)
        if os.path.isfile(p):
            shutil.copy(p, src_dir)
    with open(os.path.join(src_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(clean, fh, ensure_ascii=False, indent=2)
    os.makedirs(os.path.join(src_dir, "vendor"))
    for f in VENDOR_FILES:
        shutil.copy(os.path.join(HERE, "vendor", f), os.path.join(src_dir, "vendor"))


def security_check(stage: str):
    log("[3/4] 安全检查…")
    real_path = os.path.join(HERE, "config.json")
    real_acct = real_enc = ""
    if os.path.isfile(real_path):
        try:
            real = json.load(open(real_path, encoding="utf-8"))
            real_acct = (real.get("username") or "").strip()
            real_enc = (real.get("password_enc") or "").strip()
        except Exception:
            pass

    bad = []
    for root, _dirs, files in os.walk(stage):
        for fn in files:
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, stage)
            if fn.lower().endswith((".log", ".bak", ".pyc")):
                bad.append("不该打包进来: " + rel)
                continue
            if fn == "config.json":
                c = json.load(open(p, encoding="utf-8"))
                if (c.get("username") or "").strip():
                    bad.append(rel + " 里带着账号")
                if (c.get("password_enc") or "").strip():
                    bad.append(rel + " 里带着密码密文")
                if "password" in c:
                    bad.append(rel + " 里带着明文密码字段")
            blob = open(p, "rb").read()
            if real_acct and real_acct.encode("utf-8") in blob:
                bad.append(rel + " 含本机真实账号")
            if real_enc and real_enc.encode("utf-8") in blob:
                bad.append(rel + " 含本机真实密码密文")
    if bad:
        for b in bad:
            log("      ✗ " + b)
        raise SystemExit("安全检查未通过，已中止打包")
    log("      ✓ 包内不含账号 / 密码密文 / 日志")


def make_zip(stage: str, out_dir: str):
    log("[4/4] 压缩…")
    zip_path = os.path.join(out_dir, APP_NAME + ".zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _dirs, files in os.walk(stage):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.join(APP_NAME, os.path.relpath(full, stage))
                z.write(full, rel.replace("\\", "/"))
    log("      -> %s  (%.2f MB)" % (zip_path, os.path.getsize(zip_path) / 1048576))
    return zip_path


def desktop_dir():
    import winreg
    for root, key in ((winreg.HKEY_CURRENT_USER,
                       r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"),
                      (winreg.HKEY_CURRENT_USER,
                       r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")):
        try:
            with winreg.OpenKey(root, key) as k:
                v, _ = winreg.QueryValueEx(k, "Desktop")
                p = os.path.expandvars(v)
                if os.path.isdir(p):
                    return p
        except Exception:
            pass
    return os.path.expanduser("~")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    out_dir = sys.argv[1] if len(sys.argv) > 1 else desktop_dir()
    os.makedirs(out_dir, exist_ok=True)
    build_root = os.path.join(HERE, "release")
    os.makedirs(build_root, exist_ok=True)
    stage = os.path.join(build_root, APP_NAME)

    log("=" * 56)
    log(" 生成发布包 -> %s" % out_dir)
    log("=" * 56)
    exe = build_exe(build_root)
    assemble(stage, exe)
    security_check(stage)
    zip_path = make_zip(stage, out_dir)
    log("")
    log("完成 ✅  %s" % zip_path)
    log("把 zip 发给别人，解压后双击 %s.exe 即可（无需装 Python）" % EXE_NAME)


if __name__ == "__main__":
    main()
