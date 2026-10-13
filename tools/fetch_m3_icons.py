# -*- coding: utf-8 -*-
"""把 Material Symbols 图标抓成本地 SVG sprite。

为什么不用 CDN：设置面板是 WKWebView 加载的本地页面，用户可能离线，
而且打包成 .app 之后不该再依赖外网。图标内联进 HTML 最稳。

用法：
    python tools/fetch_m3_icons.py            # 抓图标，生成 assets/m3_icons.html
    python tools/fetch_m3_icons.py --inject   # 抓完直接注入 prefs.html 的占位处
"""

import os
import re
import sys
import urllib.request

BASE = "https://cdn.jsdelivr.net/npm/@material-symbols/svg-400@0.25.3/outlined"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 图标名 → sprite 里的 id。命名按面板实际用途起，改用途时只改这张表
ICONS = {
    "tablet_mac": "island",
    "grid_view": "cards",
    "swap_horiz": "switcher",
    "settings": "general",
    "info": "about",
    "restart_alt": "restart",
    "folder_open": "folder",
    "history": "restore",
    "accessibility_new": "accessibility",
    "videocam": "screenrec",
    "rocket_launch": "onboard",
    "monitor_heart": "diagnose",
    "open_in_new": "opennew",
    "palette": "palette",
    "monitor": "monitor",
    "aspect_ratio": "resize",
    "dark_mode": "theme",
    "touch_app": "hotzone",
}


def fetch(name):
    url = "%s/%s.svg" % (BASE, name)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def extract_path(svg):
    """只要 path 的 d，viewBox 统一用官方的 0 -960 960 960。"""
    m = re.search(r'<path[^>]*\sd="([^"]+)"', svg)
    if not m:
        raise ValueError("没找到 path")
    return m.group(1)


def main():
    inject = "--inject" in sys.argv
    parts = []
    ok, fail = 0, []
    for name, sid in ICONS.items():
        try:
            d = extract_path(fetch(name))
            ok += 1
        except Exception as e:
            fail.append((name, str(e)[:60]))
            continue
        parts.append(
            '<symbol id="ic-%s" viewBox="0 -960 960 960">'
            '<path d="%s"/></symbol>' % (sid, d)
        )
    sprite = ('<svg xmlns="http://www.w3.org/2000/svg" style="display:none" '
              'aria-hidden="true">\n  ' + "\n  ".join(parts) + "\n</svg>")

    out = os.path.join(HERE, "assets", "m3_icons.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(sprite + "\n")
    print("✅ 抓到 %d/%d 个图标 → %s（%.1f KB）"
          % (ok, len(ICONS), os.path.relpath(out, HERE), len(sprite) / 1024))
    for n, e in fail:
        print("   ⚠️ %s 失败：%s" % (n, e))

    if inject:
        target = os.path.join(HERE, "prefs.html")
        src = open(target, encoding="utf-8").read()
        marker = "<!--M3_ICONS-->"
        if marker not in src:
            print("❌ prefs.html 里没有 %s 占位，跳过注入" % marker)
            return
        src = src.replace(marker, sprite)
        with open(target, "w", encoding="utf-8") as f:
            f.write(src)
        print("✅ 已注入 prefs.html")


if __name__ == "__main__":
    main()
