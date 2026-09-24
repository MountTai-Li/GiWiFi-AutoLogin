# -*- coding: utf-8 -*-
"""
make_icon.py —— 纯 Python 生成程序图标 giwifi.ico（不依赖 Pillow）

做法：
  1) 在 1024×1024 上用有符号距离场（SDF）做软件光栅化 —— 天然抗锯齿；
  2) 用面积加权（box）重采样到各尺寸，非整数倍率也能正确缩放；
  3) 按 Windows ICO 规范组装：小尺寸用 BMP(DIB) 条目，128/256 用 PNG 条目。
"""
import math
import os
import struct
import zlib

MASTER = 768
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]

# ---------------- 视觉参数（相对 MASTER 的归一化比例） ----------------
RADIUS_RATIO = 0.225                       # 外框圆角半径
TOP_COLOR = (0x3B, 0x82, 0xF6)             # 背景渐变：上
BOT_COLOR = (0x13, 0x3A, 0x9E)             # 背景渐变：下
RING_COLOR = (0x3D, 0xDC, 0x84)            # 「自动」绿环
RING_INSET = 0.050                         # 绿环中心线距外框内缩
RING_WIDTH = 0.028
CX, CY = 0.5, 0.715                        # WiFi 弧圆心
ARC_R = (0.135, 0.240, 0.345)              # 三圈弧半径
ARC_HALF_W = 0.034                         # 弧线半宽
ARC_LO, ARC_HI = -156.0, -24.0             # 角度区间（atan2 坐标系，向上张开）
DOT_R = 0.058                              # 中心圆点半径


def _clamp(v, a, b):
    return a if v < a else (b if v > b else v)


def _rounded_rect_sdf(x, y, w, r):
    qx = abs(x - w * 0.5) - (w * 0.5 - r)
    qy = abs(y - w * 0.5) - (w * 0.5 - r)
    return (math.hypot(max(qx, 0.0), max(qy, 0.0))
            + min(max(qx, qy), 0.0) - r)


def render_master():
    """返回 MASTER×MASTER RGBA bytearray"""
    W = MASTER
    buf = bytearray(W * W * 4)
    rad = RADIUS_RATIO * W
    cx, cy = CX * W, CY * W
    dot_r = DOT_R * W
    ring_c = RING_INSET * W                 # 绿环中心线的 SDF 值 = -ring_c
    ring_hw = RING_WIDTH * W * 0.5
    arcs = [r * W for r in ARC_R]
    aw = ARC_HALF_W * W
    near_min = arcs[0] - aw
    near_max = arcs[-1] + aw

    for y in range(W):
        base = y * W * 4
        for x in range(W):
            i = base + x * 4
            px, py = x + 0.5, y + 0.5
            d = _rounded_rect_sdf(px, py, W, rad)
            if d > 0.5:
                continue                                   # 圆角之外保持透明

            t = y / W
            r = int(TOP_COLOR[0] + (BOT_COLOR[0] - TOP_COLOR[0]) * t)
            g = int(TOP_COLOR[1] + (BOT_COLOR[1] - TOP_COLOR[1]) * t)
            b = int(TOP_COLOR[2] + (BOT_COLOR[2] - TOP_COLOR[2]) * t)

            # 绿色环
            dd = abs(d + ring_c)
            if dd <= ring_hw:
                cov = _clamp(ring_hw - dd + 0.5, 0.0, 1.0)
                r = int(r + (RING_COLOR[0] - r) * cov)
                g = int(g + (RING_COLOR[1] - g) * cov)
                b = int(b + (RING_COLOR[2] - b) * cov)

            # WiFi 弧 + 中心点
            dx, dy = px - cx, py - cy
            dist = math.hypot(dx, dy)
            if dist <= dot_r:
                r = g = b = 255
            elif near_min <= dist <= near_max:
                ang = math.degrees(math.atan2(dy, dx))
                if ARC_LO <= ang <= ARC_HI:
                    for ar in arcs:
                        e = abs(dist - ar)
                        if e <= aw:                        # 三段弧的重叠区无所谓
                            cov = _clamp(aw - e + 0.5, 0.0, 1.0)
                            r = int(r + (255 - r) * cov)
                            g = int(g + (255 - g) * cov)
                            b = int(b + (255 - b) * cov)

            buf[i] = r
            buf[i + 1] = g
            buf[i + 2] = b
            buf[i + 3] = 255
    return buf


# ------------------------------------------------------------ 面积加权缩放
def resize_area(rgba, src_w, dst_w):
    """分离式面积加权重采样（支持任意倍率，预乘 alpha 避免边缘发黑）"""
    if src_w == dst_w:
        return bytearray(rgba)
    # --- 横向
    tmp = bytearray(dst_w * src_w * 4)
    scale = src_w / dst_w
    starts = [i * scale for i in range(dst_w)]
    for y in range(src_w):
        row = y * src_w * 4
        for ox, s0 in enumerate(starts):
            s1 = s0 + scale
            i0, i1 = int(s0), min(int(math.ceil(s1)), src_w)
            ar = ag = ab = aa = 0.0
            for sx in range(i0, i1):
                wgt = min(s1, sx + 1) - max(s0, sx)
                if wgt <= 0:
                    continue
                si = row + sx * 4
                a = rgba[si + 3]
                ar += rgba[si] * a * wgt
                ag += rgba[si + 1] * a * wgt
                ab += rgba[si + 2] * a * wgt
                aa += a * wgt
            di = (y * dst_w + ox) * 4
            if aa > 0:
                tmp[di] = int(ar / aa)
                tmp[di + 1] = int(ag / aa)
                tmp[di + 2] = int(ab / aa)
            tmp[di + 3] = int(_clamp(aa / scale, 0, 255))
    # --- 纵向
    out = bytearray(dst_w * dst_w * 4)
    for oy, s0 in enumerate(starts):
        s1 = s0 + scale
        i0, i1 = int(s0), min(int(math.ceil(s1)), src_w)
        for x in range(dst_w):
            ar = ag = ab = aa = 0.0
            for sy in range(i0, i1):
                wgt = min(s1, sy + 1) - max(s0, sy)
                if wgt <= 0:
                    continue
                si = (sy * dst_w + x) * 4
                a = tmp[si + 3]
                ar += tmp[si] * a * wgt
                ag += tmp[si + 1] * a * wgt
                ab += tmp[si + 2] * a * wgt
                aa += a * wgt
            di = (oy * dst_w + x) * 4
            if aa > 0:
                out[di] = int(ar / aa)
                out[di + 1] = int(ag / aa)
                out[di + 2] = int(ab / aa)
            out[di + 3] = int(_clamp(aa / scale, 0, 255))
    return out


# ---------------------------------------------------------------- 编码器
def encode_png(rgba, w):
    raw = bytearray()
    for y in range(w):
        raw.append(0)                                   # filter type 0
        raw += rgba[y * w * 4:(y + 1) * w * 4]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, w, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def encode_dib(rgba, w):
    """ICO 内的 BMP 条目：BITMAPINFOHEADER + 自下而上 BGRA + AND 掩码"""
    hdr = struct.pack("<IiiHHIIiiII", 40, w, w * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(w - 1, -1, -1):
        for x in range(w):
            i = (y * w + x) * 4
            xor += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))
    row_bytes = ((w + 31) // 32) * 4
    andm = bytearray()
    for y in range(w - 1, -1, -1):
        bits = bytearray(row_bytes)
        for x in range(w):
            if rgba[(y * w + x) * 4 + 3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        andm += bits
    return hdr + bytes(xor) + bytes(andm)


def build_ico(images):
    entries, blobs, offset = [], b"", 6 + 16 * len(images)
    for size in sorted(images):
        data = (encode_png(images[size], size) if size >= 128
                else encode_dib(images[size], size))
        entries.append(struct.pack("<BBBBHHII",
                                   size if size < 256 else 0,
                                   size if size < 256 else 0,
                                   0, 0, 1, 32, len(data), offset))
        blobs += data
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(entries)) + b"".join(entries) + blobs


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    print("正在渲染 %d×%d 母版…" % (MASTER, MASTER))
    master = render_master()
    base256 = resize_area(master, MASTER, 256)

    images = {}
    for s in SIZES:
        images[s] = base256 if s == 256 else resize_area(base256, 256, s)

    ico = os.path.join(here, "giwifi.ico")
    with open(ico, "wb") as f:
        f.write(build_ico(images))
    print("已生成图标:", ico, os.path.getsize(ico), "字节, 含尺寸", SIZES)

    png = os.path.join(here, "icon_preview.png")
    with open(png, "wb") as f:
        f.write(encode_png(base256, 256))
    print("预览图:", png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
