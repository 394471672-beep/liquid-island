# -*- coding: utf-8 -*-
"""py2app 打包配置：把 Python + PyObjC 一起塞进 .app，朋友拿到就能双击运行。

为什么必须打包成自包含 App：
  直接发源码的话，对方得自己装 Python、装 PyObjC、还要一个个装依赖，
  99% 的人会在第一步就放弃。py2app 产出的 .app 里自带 Python 运行时，
  对方不需要机器上装任何东西。

用法（一般走 ./build.sh，它会先建好 venv）：
    .build-venv/bin/python setup.py py2app
"""

import os
import sys

from setuptools import setup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from version import (APP_NAME, APP_NAME_EN, APP_BUILD, APP_VERSION)  # noqa: E402


def _workaround_py313_zlib():
    """py2app ≤0.28.10 在 Python 3.13 上会崩：

    3.13 把 zlib 编译进了解释器（内置模块），`zlib.__file__` 不存在，
    而 py2app 的 build_executable 里无条件执行
    `copy_file(zlib.__file__, ...)` → AttributeError 直接挂掉。

    内置模块本来就不用额外复制 .so（解释器自带），
    所以给它指一个 0 字节占位文件，让 copy_file 有东西可拷即可。
    """
    import sys
    import tempfile
    import zlib

    if hasattr(zlib, "__file__") or sys.version_info < (3, 13):
        return
    stub_dir = tempfile.mkdtemp(prefix="py2app-zlib-stub-")
    stub = os.path.join(stub_dir, "zlib_builtin.so")
    with open(stub, "wb"):
        pass
    zlib.__file__ = stub
    print(f"[setup] 已绕过 py2app 的 zlib.__file__ 问题（Python {sys.version.split()[0]}）")


_workaround_py313_zlib()

# 这些是被**函数内动态 import** 的模块（onboarding / panels / prefs）。
# py2app 靠静态分析找依赖，动态 import 有漏网风险，这里显式列出来兜底。
INCLUDES = [
    "config", "core", "hotkey", "island", "switcher", "prefs", "autostart",
    "onboarding", "panels", "resources", "version",
    # PyObjC 的框架桥接层：WebKit 是设置面板（WKWebView）用的，
    # ApplicationServices 是辅助功能（AX）API 用的
    "WebKit", "ApplicationServices", "Quartz", "AppKit", "Foundation",
    "objc", "PyObjCTools",
]

OPTIONS = {
    "argv_emulation": False,     # 菜单栏应用不接命令行参数
    "iconfile": "assets/AppIcon.icns",
    "includes": INCLUDES,
    # 资源会被放进 Contents/Resources/，代码里用 resources.resource_path 取
    "resources": ["prefs.html", "assets"],
    "plist": {
        "LSUIElement": True,                 # 菜单栏常驻，不在 Dock 显示
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleGetInfoString": f"{APP_NAME} {APP_VERSION}",
        "CFBundleIdentifier": "local.laomian.liquidisland",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_BUILD,
        "CFBundleExecutable": APP_NAME_EN,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "NSHumanReadableCopyright": "Copyright © 2026 液态岛 contributors",
        "NSAppleEventsUsageDescription": (
            "液态岛需要辅助功能权限来列出并切换其他应用的窗口"),
        "NSPrincipalClass": "NSApplication",
    },
    # 不 strip：包会大几 MB，但出错时还能看到符号，方便排错
    "strip": False,
    "optimize": 0,
}

setup(
    app=["main.py"],
    name=APP_NAME,
    version=APP_VERSION,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
