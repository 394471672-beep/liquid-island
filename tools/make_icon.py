#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成液态岛 App 图标：玻璃胶囊造型（iOS 灵动岛 + 蓝色强调点）。

为什么用脚本生成而不是在仓库里塞一张设计稿：
  图标参数化后，谁都能改个配色/比例重出图，不用依赖设计软件；
  仓库里只留生成脚本 + 最终 .icns，改起来是可复现的。

用法：
    python tools/make_icon.py

产出：
    assets/AppIcon.iconset/*.png   （iconutil 的输入）
    assets/AppIcon.icns            （打包进 .app 的最终图标）
"""

import os
import shutil
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(BASE, "assets")
ICONSET = os.path.join(ASSETS, "AppIcon.iconset")
ICNS = os.path.join(ASSETS, "AppIcon.icns")

# 强调色：和设置面板里的 accent 默认值保持一致
ACCENT = (0x4D, 0xC1, 0xFF)
# 玻璃底：比 UI 里的浮层再深一点，图标才在 Dock/访达里立得住
GLASS_TOP = (0.14, 0.14, 0.18)
GLASS_BOT = (0.07, 0.07, 0.10)

# iconset 规范：每档都要一份 1x 和一份 @2x
SIZES = (16, 32, 128, 256, 512)


def _rounded_mask(n, box, radius):
    """画一个圆角矩形，返回 0..1 的 float mask。"""
    img = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(box, radius=radius, fill=255)
    return np.asarray(img, dtype=np.float32) / 255.0


def _circle_mask(n, cx, cy, r, feather=0.0):
    """画一个圆，feather 是边缘羽化宽度（0 = 硬边）。"""
    img = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(img)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    a = np.asarray(img, dtype=np.float32) / 255.0
    if feather > 0:
        a = np.asarray(Image.fromarray((a * 255).astype(np.uint8)).filter(
            ImageFilter.GaussianBlur(feather)), dtype=np.float32) / 255.0
    return a


def draw_icon(n):
    """画一张 n×n 的 RGBA 图标。"""
    # 超采样 2 倍再缩下来：小尺寸下圆角和蓝点才不会有锯齿
    N = n * 2
    out = np.zeros((N, N, 4), dtype=np.float32)

    # ── 胶囊本体 ──
    w, h = N * 0.86, N * 0.34
    x0, y0 = (N - w) / 2, (N - h) / 2
    cap = _rounded_mask(N, (x0, y0, x0 + w, y0 + h), h / 2)

    # 竖向渐变（顶略亮 → 底更深），玻璃感的基础
    ys = np.linspace(0.0, 1.0, N, dtype=np.float32)[:, None]
    grad = (ys - y0 / N) / max(h / N, 1e-6)
    grad = np.clip(grad, 0.0, 1.0)
    r = GLASS_TOP[0] + (GLASS_BOT[0] - GLASS_TOP[0]) * grad
    g = GLASS_TOP[1] + (GLASS_BOT[1] - GLASS_TOP[1]) * grad
    b = GLASS_TOP[2] + (GLASS_BOT[2] - GLASS_TOP[2]) * grad

    out[..., 0] = r * cap
    out[..., 1] = g * cap
    out[..., 2] = b * cap
    out[..., 3] = cap

    # ── 顶部高光：只在胶囊上半部，越往上越亮（玻璃反光） ──
    gloss_t = np.clip(1.0 - grad / 0.55, 0.0, 1.0) ** 1.4
    gloss = gloss_t * cap * 0.30
    # 叠加高光（白）：alpha 用 max 合并会发灰，这里直接做 source-over
    a_src = gloss
    out[..., 0] = out[..., 0] * (1 - a_src) + 1.0 * a_src
    out[..., 1] = out[..., 1] * (1 - a_src) + 1.0 * a_src
    out[..., 2] = out[..., 2] * (1 - a_src) + 1.0 * a_src
    out[..., 3] = np.clip(out[..., 3] + a_src, 0.0, 1.0)

    # ── 亮边：mask 减去腐蚀后的 mask = 一圈描边 ──
    eroded = np.asarray(
        Image.fromarray((cap * 255).astype(np.uint8)).filter(
            ImageFilter.MinFilter(5)), dtype=np.float32) / 255.0
    edge = np.clip(cap - eroded, 0.0, 1.0)
    edge_a = edge * 0.55
    out[..., 0] = out[..., 0] * (1 - edge_a) + 1.0 * edge_a
    out[..., 1] = out[..., 1] * (1 - edge_a) + 1.0 * edge_a
    out[..., 2] = out[..., 2] * (1 - edge_a) + 1.0 * edge_a
    out[..., 3] = np.clip(out[..., 3] + edge_a, 0.0, 1.0)

    # ── 胶囊内的两个点 ──
    cy = y0 + h / 2
    # 左：摄像头像的深色凹点
    cam_r = h * 0.13
    cam = _circle_mask(N, x0 + w * 0.20, cy, cam_r, feather=N * 0.004)
    cam_a = cam * 0.55
    out[..., 0] = out[..., 0] * (1 - cam_a) + 0.02 * cam_a
    out[..., 1] = out[..., 1] * (1 - cam_a) + 0.02 * cam_a
    out[..., 2] = out[..., 2] * (1 - cam_a) + 0.03 * cam_a
    out[..., 3] = np.clip(out[..., 3] + cam_a * 0.6, 0.0, 1.0)

    # 右：蓝色活动指示点（带辉光）
    dot_r = h * 0.15
    dx = x0 + w * 0.80
    glow = _circle_mask(N, dx, cy, dot_r * 2.6, feather=N * 0.02)
    glow_a = np.clip(glow * 0.42 - cam * 0, 0.0, 1.0) * cap
    ar, ag, ab = (ACCENT[0] / 255.0, ACCENT[1] / 255.0, ACCENT[2] / 255.0)
    out[..., 0] = out[..., 0] * (1 - glow_a) + ar * glow_a
    out[..., 1] = out[..., 1] * (1 - glow_a) + ag * glow_a
    out[..., 2] = out[..., 2] * (1 - glow_a) + ab * glow_a
    out[..., 3] = np.clip(out[..., 3] + glow_a, 0.0, 1.0)

    dot = _circle_mask(N, dx, cy, dot_r, feather=N * 0.003)
    dot_a = dot * 1.0 * cap
    out[..., 0] = out[..., 0] * (1 - dot_a) + ar * dot_a
    out[..., 1] = out[..., 1] * (1 - dot_a) + ag * dot_a
    out[..., 2] = out[..., 2] * (1 - dot_a) + ab * dot_a
    out[..., 3] = np.clip(out[..., 3] + dot_a, 0.0, 1.0)

    img = Image.fromarray((np.clip(out, 0.0, 1.0) * 255).astype(np.uint8), "RGBA")
    if N != n:
        img = img.resize((n, n), Image.LANCZOS)
    return img


def build_iconset():
    if os.path.isdir(ICONSET):
        shutil.rmtree(ICONSET)
    os.makedirs(ICONSET, exist_ok=True)
    for s in SIZES:
        draw_icon(s).save(os.path.join(ICONSET, f"icon_{s}x{s}.png"))
        draw_icon(s * 2).save(os.path.join(ICONSET, f"icon_{s}x{s}@2x.png"))
    print(f"[icon] iconset 已生成：{ICONSET}")


def build_icns():
    if os.path.exists(ICNS):
        os.remove(ICNS)
    r = subprocess.run(["iconutil", "-c", "icns", ICONSET, "-o", ICNS],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("[icon] iconutil 失败：", r.stderr.strip())
        return False
    size = os.path.getsize(ICNS) if os.path.exists(ICNS) else 0
    print(f"[icon] icns 已生成：{ICNS}（{size} 字节）")
    return size > 0


if __name__ == "__main__":
    os.makedirs(ASSETS, exist_ok=True)
    build_iconset()
    ok = build_icns()
    # 顺手出一张 512 的 PNG，README 里当封面图用
    draw_icon(512).save(os.path.join(ASSETS, "icon_512.png"))
    print("[icon] 完成" if ok else "[icon] 未完成")
