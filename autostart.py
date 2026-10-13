# -*- coding: utf-8 -*-
"""开机自启：写 ~/Library/LaunchAgents 下的 plist 并交给 launchd。

注意：本仓库里执行 launchctl 会被沙箱挡住，但用户双击 App 时是用户权限，
是可以正常加载的。
"""
import os
import plistlib
import subprocess

LABEL = "local.laomian.liquidisland"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
BASE = os.path.expanduser("~/Scripts/liquid_island")
PYTHON = os.path.expanduser(
    "~/.workbuddy/binaries/python/envs/default/bin/python")


def is_on():
    return os.path.exists(PLIST)


def apply(enable):
    """打开/关闭开机自启。返回 (成功, 说明)。"""
    uid = os.getuid()
    try:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True, timeout=10)
    except Exception:
        pass
    if not enable:
        try:
            os.remove(PLIST)
        except Exception:
            pass
        return True, "已关闭开机自启"

    try:
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        data = {
            "Label": LABEL,
            "ProgramArguments": [PYTHON, os.path.join(BASE, "main.py")],
            "WorkingDirectory": BASE,
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": os.path.join(BASE, "app.log"),
            "StandardErrorPath": os.path.join(BASE, "app.log"),
        }
        with open(PLIST, "wb") as f:
            plistlib.dump(data, f)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST],
                           capture_output=True, timeout=15)
        if r.returncode == 0:
            return True, "已开启开机自启"
        return True, "已写入登录项（launchd 未能立即加载，登录时仍会生效）"
    except Exception as e:
        return False, f"写入登录项失败：{e}"
