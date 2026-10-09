# -*- coding: utf-8 -*-
"""
build_apk.py —— 安卓版手动构建脚本（无需 Gradle / Android Studio）
====================================================================

用法：
    python build_apk.py

依赖（已就位）：
    · JDK（javac / keytool / jar）        —— 本机 Oracle JDK 21
    · Android build-tools（aapt2 / d8 / zipalign / apksigner）
    · android.jar（platform-34）
    默认从 <工作区>/_android_sdk 读取，可用环境变量 GIWIFI_ANDROID_SDK 覆盖。

流程：aapt2 compile → aapt2 link → javac → jar → d8 → 装 dex → zipalign → apksigner
产物：out/GiWiFi自动登录-v1.0.0.apk
"""
import glob
import os
import shutil
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))            # android/
ROOT = os.path.dirname(HERE)                                 # GiwifiAutoLogin/
WORKSPACE = os.path.dirname(ROOT)
SDK = os.environ.get("GIWIFI_ANDROID_SDK") or os.path.join(WORKSPACE, "_android_sdk")

BUILD = os.path.join(HERE, "build")
OUT = os.path.join(HERE, "out")

AAPT2 = os.path.join(SDK, "build-tools", "aapt2.exe")
D8 = os.path.join(SDK, "build-tools", "d8.bat")
ZIPALIGN = os.path.join(SDK, "build-tools", "zipalign.exe")
APKSIGNER = os.path.join(SDK, "build-tools", "apksigner.bat")
ANDROID_JAR = os.path.join(SDK, "platform", "android.jar")

APP_NAME = "GiWiFi自动登录"
VERSION = "1.0.0"
KEYSTORE = os.path.join(HERE, "giwifi-release.jks")
KS_PASS_FILE = os.path.join(HERE, ".keystore-pass")   # 本地口令文件（.gitignore 已排除）
KEY_ALIAS = "giwifi"


def load_ks_pass():
    """签名口令：优先环境变量 GIWIFI_KS_PASS，其次本地口令文件（均不入库）"""
    p = os.environ.get("GIWIFI_KS_PASS")
    if p:
        return p.strip()
    if os.path.isfile(KS_PASS_FILE):
        try:
            with open(KS_PASS_FILE, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None


def save_ks_pass(p):
    with open(KS_PASS_FILE, "w", encoding="utf-8") as f:
        f.write(p + "\n")


def log(msg):
    print("[build] %s" % msg, flush=True)


def find_java_home():
    jh = os.environ.get("JAVA_HOME", "")
    if jh and os.path.isfile(os.path.join(jh, "bin", "javac.exe")):
        return jh
    base = r"C:\Program Files\Java"
    if os.path.isdir(base):
        for d in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, d)
            if os.path.isfile(os.path.join(p, "bin", "javac.exe")):
                return p
    exe = shutil.which("javac")
    if exe:
        return os.path.dirname(os.path.dirname(exe))
    raise SystemExit("找不到 JDK（javac）")


def mask_cmd(cmd):
    """打印命令时遮蔽口令参数，避免构建日志泄露"""
    out, hide_next = [], False
    for c in cmd:
        if hide_next:
            out.append("pass:******" if c.startswith("pass:") else "******")
            hide_next = False
        else:
            out.append(c)
            if c in ("--ks-pass", "--key-pass", "-storepass", "-keypass"):
                hide_next = True
    return out


def run(cmd, env=None, quiet=False):
    if not quiet:
        log("$ " + " ".join('"%s"' % c if " " in c else c for c in mask_cmd(cmd)))
    p = subprocess.run(cmd, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        print(p.stdout[-4000:])
        print(p.stderr[-4000:])
        raise SystemExit("命令失败（rc=%s）: %s" % (p.returncode, cmd[0]))
    return p


def main():
    # ---------- 环境检查 ----------
    for f, name in ((AAPT2, "aapt2"), (D8, "d8"), (ZIPALIGN, "zipalign"),
                    (APKSIGNER, "apksigner"), (ANDROID_JAR, "android.jar")):
        if not os.path.isfile(f):
            raise SystemExit("缺少 %s：%s" % (name, f))
    java_home = find_java_home()
    javac = os.path.join(java_home, "bin", "javac.exe")
    jar = os.path.join(java_home, "bin", "jar.exe")
    keytool = os.path.join(java_home, "bin", "keytool.exe")
    log("JDK: %s" % java_home)

    env = dict(os.environ)
    env["JAVA_HOME"] = java_home

    # ---------- 清理 ----------
    shutil.rmtree(BUILD, ignore_errors=True)
    os.makedirs(BUILD, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    # ---------- 1) 资源编译 ----------
    log("[1/8] aapt2 compile")
    res_zip = os.path.join(BUILD, "res.zip")
    run([AAPT2, "compile", "--dir", os.path.join(HERE, "res"), "-o", res_zip])

    # ---------- 2) 资源链接（生成 R.java + 未签名 APK） ----------
    log("[2/8] aapt2 link")
    unsigned = os.path.join(BUILD, "app-unsigned.apk")
    gen_dir = os.path.join(BUILD, "gen")
    os.makedirs(gen_dir, exist_ok=True)
    run([AAPT2, "link", "-o", unsigned, "-I", ANDROID_JAR,
         "--manifest", os.path.join(HERE, "AndroidManifest.xml"),
         "--java", gen_dir, res_zip])

    # ---------- 3) javac 编译 ----------
    log("[3/8] javac")
    sources = glob.glob(os.path.join(gen_dir, "**", "*.java"), recursive=True)
    sources += glob.glob(os.path.join(HERE, "src", "**", "*.java"), recursive=True)
    classes = os.path.join(BUILD, "classes")
    os.makedirs(classes, exist_ok=True)
    run([javac, "-encoding", "UTF-8", "-source", "8", "-target", "8",
         "-Xlint:-options,-deprecation", "-classpath", ANDROID_JAR,
         "-d", classes] + sources)

    # ---------- 4) 打包 class ----------
    log("[4/8] jar")
    classes_jar = os.path.join(BUILD, "classes.jar")
    run([jar, "cf", classes_jar, "-C", classes, "."])

    # ---------- 5) d8 转 dex ----------
    log("[5/8] d8")
    dex_dir = os.path.join(BUILD, "dex")
    os.makedirs(dex_dir, exist_ok=True)
    run(["cmd", "/c", D8, "--release", "--lib", ANDROID_JAR,
         "--min-api", "23", "--output", dex_dir, classes_jar], env=env)

    # ---------- 6) 把 classes.dex 塞进 APK ----------
    log("[6/8] 装 dex")
    dex_file = os.path.join(dex_dir, "classes.dex")
    if not os.path.isfile(dex_file):
        raise SystemExit("d8 没有产出 classes.dex")
    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex_file, "classes.dex")

    # ---------- 7) zipalign ----------
    log("[7/8] zipalign")
    aligned = os.path.join(BUILD, "app-aligned.apk")
    run([ZIPALIGN, "-f", "4", unsigned, aligned])

    # ---------- 8) 签名 ----------
    log("[8/8] 签名")
    ks_pass = load_ks_pass()
    if not os.path.isfile(KEYSTORE):
        if not ks_pass:
            # 首次构建：生成随机口令并保存到本地口令文件（不入库）
            import secrets
            ks_pass = secrets.token_urlsafe(15)
            save_ks_pass(ks_pass)
            log("已生成随机签名口令 → %s（请妥善保管本文件）" % KS_PASS_FILE)
        log("生成签名密钥: %s" % KEYSTORE)
        run([keytool, "-genkeypair", "-keystore", KEYSTORE, "-alias", KEY_ALIAS,
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
             "-storepass", ks_pass, "-keypass", ks_pass,
             "-dname", "CN=GiWiFi AutoLogin, OU=Personal, O=MountTai-Li, C=CN"])
    elif not ks_pass:
        log("❌ 找不到签名口令：请设置环境变量 GIWIFI_KS_PASS，")
        log("   或在 %s 写入口令（每行一个口令值）" % KS_PASS_FILE)
        sys.exit(1)

    final = os.path.join(OUT, "%s-v%s.apk" % (APP_NAME, VERSION))
    if os.path.exists(final):
        os.remove(final)
    run(["cmd", "/c", APKSIGNER, "sign", "--ks", KEYSTORE,
         "--ks-pass", "pass:" + ks_pass, "--key-pass", "pass:" + ks_pass,
         "--out", final, aligned], env=env)
    run(["cmd", "/c", APKSIGNER, "verify", "--print-certs", final], env=env)

    log("完成 ✅  %s  (%.2f MB)" % (final, os.path.getsize(final) / 1048576))
    return final


if __name__ == "__main__":
    main()
