# -*- coding: utf-8 -*-
"""资源文件定位（prefs.html、图标……）。

为什么需要它：
  py2app 会把 Python 模块打进 site-packages.zip，此时 __file__ 指向的是
  **压缩包内部的路径**，`os.path.join(os.path.dirname(__file__), 'prefs.html')`
  必然找不到文件。而资源是被放到 Contents/Resources/ 下的。
  源码运行时两种路径都可能存在，所以按优先级逐个试。

用法：resource_path("prefs.html") / resource_path("assets", "AppIcon.icns")
"""

import os
import sys


def resource_path(*parts):
    """返回第一个真实存在的候选路径；都不存在就退回代码目录（方便看报错）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    cands = [
        os.path.join(here, *parts),                              # 源码运行
        os.path.join(exe_dir, os.pardir, "Resources", *parts),   # py2app 打包
        os.path.join(exe_dir, *parts),                           # 少数布局
    ]
    for c in cands:
        if os.path.exists(c):
            return os.path.abspath(c)
    return os.path.join(here, *parts)


def app_version_path():
    """日志文件位置：打包后写到用户目录下，避免往只读的 .app 里写。"""
    import tempfile
    d = os.path.expanduser("~/Library/Logs/liquid-island")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "app.log")
    except Exception:
        return os.path.join(tempfile.gettempdir(), "liquid-island.log")
