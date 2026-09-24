# -*- coding: utf-8 -*-
"""
secure_store.py —— 用 Windows 数据保护接口 (DPAPI) 加密保存密码

为什么需要它：
    config.json 是明文文件，直接写密码不安全。DPAPI 用当前 Windows
    用户的凭据派生密钥，加密后的密文只有本机本用户能解开——即使别人
    拷贝走 config.json，也拿不到你的密码。

实现：纯 ctypes 调用 crypt32.dll，不需要安装 pywin32 / cryptography。
"""
import base64
import ctypes
import ctypes.wintypes as wt

try:
    _CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _AVAILABLE = True
except Exception:                                    # 非 Windows 平台
    _CRYPT32 = _KERNEL32 = None
    _AVAILABLE = False

_ENTROPY = b"GiwifiAutoLogin/DPAPI/v1"               # 附加熵，进一步限定用途


class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _make_blob(data: bytes):
    """返回 (BLOB, 持有内存的 buffer)，buffer 必须由调用方保持引用"""
    if not data:
        return _BLOB(0, None), None
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


if _AVAILABLE:
    _CRYPT32.CryptProtectData.argtypes = [
        ctypes.POINTER(_BLOB), wt.LPCWSTR, ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB)]
    _CRYPT32.CryptProtectData.restype = wt.BOOL
    _CRYPT32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_BLOB), ctypes.POINTER(wt.LPWSTR), ctypes.POINTER(_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_BLOB)]
    _CRYPT32.CryptUnprotectData.restype = wt.BOOL
    _KERNEL32.LocalFree.argtypes = [wt.HLOCAL]
    _KERNEL32.LocalFree.restype = wt.HLOCAL


def available() -> bool:
    return _AVAILABLE


def protect(text: str) -> str:
    """明文 -> Base64 密文（绑定当前用户 + 本机）"""
    if not _AVAILABLE:
        raise RuntimeError("当前系统不支持 DPAPI")
    blob_in, keep = _make_blob(text.encode("utf-8"))
    ent, ent_keep = _make_blob(_ENTROPY)
    blob_out = _BLOB()
    ok = _CRYPT32.CryptProtectData(ctypes.byref(blob_in), "GiwifiAutoLogin",
                                   ctypes.byref(ent), None, None, 0,
                                   ctypes.byref(blob_out))
    del keep, ent_keep
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        _KERNEL32.LocalFree(blob_out.pbData)


def unprotect(cipher_b64: str) -> str:
    """Base64 密文 -> 明文"""
    if not _AVAILABLE:
        raise RuntimeError("当前系统不支持 DPAPI")
    blob_in, keep = _make_blob(base64.b64decode(cipher_b64))
    ent, ent_keep = _make_blob(_ENTROPY)
    blob_out = _BLOB()
    ok = _CRYPT32.CryptUnprotectData(ctypes.byref(blob_in), None,
                                     ctypes.byref(ent), None, None, 0,
                                     ctypes.byref(blob_out))
    del keep, ent_keep
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        _KERNEL32.LocalFree(blob_out.pbData)


# --------------------------------------------------------------- 对配置的封装
PLAIN_KEY = "password"          # 明文密码键（仅在你显式允许时才写）
ENC_KEY = "password_enc"        # DPAPI 密文键


def set_password(cfg: dict, plain: str, allow_plaintext: bool = False) -> str:
    """
    把明文密码写入配置字典。

    默认**只接受 DPAPI 加密存储**：加密失败就直接抛异常，绝不悄悄降级成明文。
    返回存储方式："dpapi" / "plain" / "none"。
    """
    cfg.pop(PLAIN_KEY, None)
    cfg.pop(ENC_KEY, None)
    if not plain:
        return "none"
    try:
        cfg[ENC_KEY] = protect(plain)
        return "dpapi"
    except Exception:
        if not allow_plaintext:
            raise RuntimeError(
                "DPAPI 加密不可用，出于安全考虑已拒绝保存明文密码。"
                "如需强行保存明文，请手工在 config.json 里填写 password 字段。")
        cfg[PLAIN_KEY] = plain
        return "plain"


def storage_kind(cfg: dict) -> str:
    """当前密码的存储方式：dpapi / plain / none"""
    if cfg.get(ENC_KEY):
        return "dpapi"
    if cfg.get(PLAIN_KEY):
        return "plain"
    return "none"


def get_password(cfg: dict) -> str:
    """从配置字典取出明文密码"""
    if cfg.get(ENC_KEY):
        try:
            return unprotect(cfg[ENC_KEY])
        except Exception:
            return ""
    return cfg.get(PLAIN_KEY, "") or ""


if __name__ == "__main__":
    s = "测试密码 test-123!"
    c = protect(s)
    print("密文:", c[:40], "...")
    print("解密:", unprotect(c))
    print("回环:", "PASS" if unprotect(c) == s else "FAIL")
