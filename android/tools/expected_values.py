# -*- coding: utf-8 -*-
"""生成协议一致性测试的 Python 侧期望值（与 Java 自测输出对比）。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))      # android/tools → android → GiwifiAutoLogin
sys.path.insert(0, ROOT)

import aes128
from giwifi import jq_enc, parse_login_fields

print("AES1=" + aes128.encrypt_cbc_zeropad(
    "name=19819524363&password=abc123&iv=abcdefghijklmnop",
    "1234567887654321", "abcdefghijklmnop"))
print("AES2=" + aes128.encrypt_cbc_zeropad(
    "name=测试账号&password=密码123&sign=abc",
    "1234567887654321", "1234567890abcdef"))
print("AES3=" + aes128.encrypt_cbc_zeropad(
    "1234567890abcdef", "1234567887654321", "1234567890abcdef"))

print("JQ1=" + jq_enc("中文 a+b=c&d/e?f=g h'()!~*"))

with open(os.path.join(HERE, "login_page_sample.html"), encoding="utf-8") as f:
    html = f.read()
fields = parse_login_fields(html, "frmLogin")
print("FIELDS_START")
for k, v in fields:
    print("%s=%s" % (k, v))
print("FIELDS_END")

# 完整序列化（假账号）
parts = []
iv = None
for k, v in fields:
    if k == "name":
        v = "19819524363"
    elif k == "password":
        v = "test-pass-123"
    if k == "iv":
        iv = v
    parts.append("%s=%s" % (jq_enc(k), jq_enc(v)))
plain = "&".join(parts)
print("PLAIN=" + plain)
print("SERIALIZED_AES=" + aes128.encrypt_cbc_zeropad(plain, "1234567887654321", iv))
