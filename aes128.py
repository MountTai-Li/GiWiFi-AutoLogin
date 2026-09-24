# -*- coding: utf-8 -*-
"""
aes128.py —— 纯 Python 实现的 AES-128-CBC + ZeroPadding
作用：复刻 GiWiFi 校园网登录页 CryptoJS 的加密行为，且不依赖任何第三方库。

对应前端 JS:
    CryptoJS.AES.encrypt(data, Utf8.parse('1234567887654321'),
                         {iv: Utf8.parse(iv), mode: CBC, padding: ZeroPadding}).toString()
"""
import base64
import binascii

# ---------------------------------------------------------------- AES 基础表
_SBOX = [
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b, 0xfe, 0xd7, 0xab, 0x76,
    0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0, 0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0,
    0xb7, 0xfd, 0x93, 0x26, 0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2, 0xeb, 0x27, 0xb2, 0x75,
    0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0, 0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84,
    0x53, 0xd1, 0x00, 0xed, 0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f, 0x50, 0x3c, 0x9f, 0xa8,
    0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5, 0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2,
    0xcd, 0x0c, 0x13, 0xec, 0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14, 0xde, 0x5e, 0x0b, 0xdb,
    0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c, 0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79,
    0xe7, 0xc8, 0x37, 0x6d, 0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f, 0x4b, 0xbd, 0x8b, 0x8a,
    0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e, 0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e,
    0xe1, 0xf8, 0x98, 0x11, 0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f, 0xb0, 0x54, 0xbb, 0x16,
]
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1b) & 0xff
    return a


def _mul(a, b):
    """GF(2^8) 乘法"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p & 0xff


def _expand_key(key):
    """AES-128 密钥扩展 → 11 组轮密钥(每组16字节)"""
    assert len(key) == 16
    w = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = t[1:] + t[:1]                       # RotWord
            t = [_SBOX[b] for b in t]               # SubWord
            t[0] ^= _RCON[i // 4 - 1]
        w.append([w[i - 4][j] ^ t[j] for j in range(4)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _add_round_key(s, rk):
    return [s[i] ^ rk[i] for i in range(16)]


def _sub_bytes(s, box):
    return [box[b] for b in s]


def _shift_rows(s):
    # 状态按列优先存放: s[c*4 + r]
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = s[((c + r) % 4) * 4 + r]
    return out


def _inv_shift_rows(s):
    out = [0] * 16
    for r in range(4):
        for c in range(4):
            out[c * 4 + r] = s[((c - r) % 4) * 4 + r]
    return out


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
        out[c * 4 + 1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
        out[c * 4 + 2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
        out[c * 4 + 3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a = s[c * 4:c * 4 + 4]
        out[c * 4 + 0] = _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9)
        out[c * 4 + 1] = _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13)
        out[c * 4 + 2] = _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11)
        out[c * 4 + 3] = _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)
    return out


def encrypt_block(block, rks):
    s = _add_round_key(list(block), rks[0])
    for rnd in range(1, 10):
        s = _sub_bytes(s, _SBOX)
        s = _shift_rows(s)
        s = _mix_columns(s)
        s = _add_round_key(s, rks[rnd])
    s = _sub_bytes(s, _SBOX)
    s = _shift_rows(s)
    s = _add_round_key(s, rks[10])
    return bytes(s)


def decrypt_block(block, rks):
    s = _add_round_key(list(block), rks[10])
    for rnd in range(9, 0, -1):
        s = _inv_shift_rows(s)
        s = _sub_bytes(s, _INV_SBOX)
        s = _add_round_key(s, rks[rnd])
        s = _inv_mix_columns(s)
    s = _inv_shift_rows(s)
    s = _sub_bytes(s, _INV_SBOX)
    s = _add_round_key(s, rks[0])
    return bytes(s)


# ------------------------------------------------------- CBC + ZeroPadding 封装
def _zeropad(data: bytes) -> bytes:
    """ZeroPadding：不足 16 字节补 0x00；已对齐则不再补整块（与 CryptoJS 一致）"""
    rem = len(data) % 16
    if rem:
        data += b"\x00" * (16 - rem)
    return data


def encrypt_cbc_zeropad(plaintext: str, key: str, iv: str) -> str:
    """
    UTF-8 明文 -> ZeroPadding -> AES-128-CBC -> Base64
    对应 CryptoJS.AES.encrypt(...).toString()
    """
    raw = _zeropad(plaintext.encode("utf-8"))
    rks = _expand_key(key.encode("utf-8"))
    prev = iv.encode("utf-8")
    out = bytearray()
    for off in range(0, len(raw), 16):
        blk = bytes(a ^ b for a, b in zip(raw[off:off + 16], prev))
        enc = encrypt_block(blk, rks)
        out += enc
        prev = enc
    return base64.b64encode(bytes(out)).decode("ascii")


def decrypt_cbc_zeropad(cipher_b64: str, key: str, iv: str) -> str:
    """反向操作，主要供自检 / 排错使用"""
    raw = base64.b64decode(cipher_b64)
    rks = _expand_key(key.encode("utf-8"))
    prev = iv.encode("utf-8")
    out = bytearray()
    for off in range(0, len(raw), 16):
        blk = raw[off:off + 16]
        dec = decrypt_block(blk, rks)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = blk
    return bytes(out).rstrip(b"\x00").decode("utf-8", "replace")


# ------------------------------------------------------------------- 自检
def _selftest():
    ok = True
    # FIPS-197 附录 B：AES-128 单块
    k = binascii.unhexlify("000102030405060708090a0b0c0d0e0f")
    p = binascii.unhexlify("00112233445566778899aabbccddeeff")
    c = encrypt_block(p, _expand_key(k))
    exp = "69c4e0d86a7b0430d8cdb78070b4c55a"
    r = binascii.hexlify(c).decode()
    print(f"[1] FIPS-197 ECB 加密: {r} == {exp} -> {'PASS' if r == exp else 'FAIL'}")
    ok &= r == exp

    # NIST SP800-38A F.2.1：AES-128-CBC 单块
    k = binascii.unhexlify("2b7e151628aed2a6abf7158809cf4f3c")
    iv = binascii.unhexlify("000102030405060708090a0b0c0d0e0f")
    p = binascii.unhexlify("6bc1bee22e409f96e93d7e117393172a")
    prev = bytes(a ^ b for a, b in zip(p, iv))
    r = binascii.hexlify(encrypt_block(prev, _expand_key(k))).decode()
    exp = "7649abac8119b246cee98e9b12e9197d"
    print(f"[2] NIST CBC 加密    : {r} == {exp} -> {'PASS' if r == exp else 'FAIL'}")
    ok &= r == exp

    # 解密回环
    d = decrypt_block(binascii.unhexlify(exp), _expand_key(k))
    back = bytes(a ^ b for a, b in zip(d, iv))
    print(f"[3] CBC 解密回环     : {binascii.hexlify(back).decode()} -> "
          f"{'PASS' if back == p else 'FAIL'}")
    ok &= back == p

    # 与 CryptoJS 语义一致性（ZeroPadding + CBC + Base64 回环）
    msg = "sign=abc%2B%2Bd&iv=4e73a7dcdd4afd3b&name=13800138000&password=x"
    enc = encrypt_cbc_zeropad(msg, "1234567887654321", "4e73a7dcdd4afd3b")
    dec = decrypt_cbc_zeropad(enc, "1234567887654321", "4e73a7dcdd4afd3b")
    print(f"[4] ZeroPad 回环     : {enc[:24]}... -> {'PASS' if dec == msg else 'FAIL'}")
    print(f"    解密结果: {dec}")
    ok &= dec == msg
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
