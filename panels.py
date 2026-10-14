# -*- coding: utf-8 -*-
"""「关于」窗口 + 「诊断与自检」窗口。

诊断面板存在的理由：
  这类常驻浮层软件出问题时，用户能提供的信息只有「它不动了」。
  诊断把权限状态、登录项、屏幕信息、冲突软件、日志路径一次性摊开，
  还能一键复制——朋友遇到问题时，让你问一句「把诊断复制给我」就够了，
  不用反复猜。
"""

import os
import subprocess
import sys

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSImage,
    NSImageView, NSMakeRect, NSObject, NSPasteboard, NSScreen, NSScrollView,
    NSTextField, NSTextView, NSView, NSWindow, NSWindowStyleMaskClosable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled, NSWorkspace,
)
from Foundation import NSURL

import autostart
import config as cfgmod
import core
from onboarding import app_assets_dir, center_on_main
from version import (APP_NAME, APP_NAME_EN, APP_VERSION, APP_BUILD, COPYRIGHT,
                     REPO_URL)

# 已知会和液态岛抢 Option+Tab / 抢顶部热区的同类工具
CONFLICTS = (
    ("AltTab", "com.lwouis.alt-tab-macos", "默认也占 Option+Tab，会和切换器抢热键"),
    ("Command-Tab Plus", "com.exner.commandtabplus", "同类窗口切换器"),
    ("Contexts", "com.contextsformac.Contexts", "同类窗口切换器"),
    ("Witch", "com.manytricks.Witch", "同类窗口切换器"),
)


def _label(text, size, bold=False, color=None):
    tf = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 10, 10))
    tf.setStringValue_(text)
    tf.setBezeled_(False)
    tf.setDrawsBackground_(False)
    tf.setEditable_(False)
    tf.setSelectable_(False)
    tf.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                else NSFont.systemFontOfSize_(size))
    tf.setTextColor_(color or NSColor.labelColor())
    return tf


def _bool_line(ok, name, desc=""):
    mark = "✅" if ok else "⚠️"
    return f"{mark} {name}" + (f"  —— {desc}" if desc else "")


# ══════════ 关于 ══════════

class AboutController(NSObject):
    def init(self):
        self = objc.super(AboutController, self).init()
        self.win = None
        return self

    @objc.python_method
    def build(self):
        W, H = 380, 300
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        win.setTitle_(f"关于 {APP_NAME}")
        win.setReleasedWhenClosed_(False)
        cv = win.contentView()

        iv = NSImageView.alloc().initWithFrame_(
            NSMakeRect((W - 96) / 2, H - 34 - 96, 96, 96))
        p = os.path.join(app_assets_dir(), "AppIcon.icns")
        if os.path.exists(p):
            img = NSImage.alloc().initWithContentsOfFile_(p)
            if img is not None:
                iv.setImage_(img)
        cv.addSubview_(iv)

        t = _label(APP_NAME, 22, bold=True)
        t.setFrame_(NSMakeRect(0, H - 152, W, 30))
        t.setAlignment_(1)
        cv.addSubview_(t)

        v = _label(f"版本 {APP_VERSION}（build {APP_BUILD}）", 13)
        v.setFrame_(NSMakeRect(0, H - 178, W, 20))
        v.setAlignment_(1)
        cv.addSubview_(v)

        e = _label(f"{APP_NAME_EN} · 灵动岛 + 窗口切换器", 12,
                   color=NSColor.secondaryLabelColor())
        e.setFrame_(NSMakeRect(0, H - 198, W, 18))
        e.setAlignment_(1)
        cv.addSubview_(e)

        c = _label(COPYRIGHT, 11, color=NSColor.secondaryLabelColor())
        c.setFrame_(NSMakeRect(0, 34, W, 18))
        c.setAlignment_(1)
        cv.addSubview_(c)

        b = NSButton.alloc().initWithFrame_(NSMakeRect((W - 140) / 2, 12, 140, 26))
        b.setTitle_("打开项目主页")
        b.setBezelStyle_(1)
        b.setTarget_(self)
        b.setAction_("actRepo:")
        cv.addSubview_(b)

        self.win = win

    def actRepo_(self, sender):
        try:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(REPO_URL))
        except Exception:
            pass

    @objc.python_method
    def show(self):
        if self.win is None:
            self.build()
        center_on_main(self.win)
        self.win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


_about = None


def open_about():
    global _about
    if _about is None:
        _about = AboutController.alloc().init()
    _about.show()


# ══════════ 诊断 ══════════

def collect_report():
    """生成纯文本诊断报告。"""
    core.refresh_perm_cache()
    cfg = cfgmod.load()
    L = []
    L.append(f"{APP_NAME} {APP_VERSION} (build {APP_BUILD})")
    L.append(f"  配置文件：{cfgmod.CONFIG_PATH}")
    L.append(f"  运行方式：{sys.executable}")
    L.append(f"  进程 PID：{os.getpid()}")
    L.append("")
    L.append("—— 权限 ——")
    L.append(_bool_line(core.has_ax_perm(), "辅助功能",
                        "缺失则窗口列表为空、切换器不可用"))
    L.append(_bool_line(core.has_screen_perm(), "屏幕录制",
                        "缺失则无窗口缩略图（切换本身不受影响）"))
    L.append("")
    L.append("—— 屏幕 ——")
    try:
        for i, s in enumerate(NSScreen.screens()):
            f = s.frame()
            mf = s.visibleFrame()
            L.append(f"  #{i} frame=({f.origin.x:.0f},{f.origin.y:.0f}) "
                     f"{f.size.width:.0f}×{f.size.height:.0f}  "
                     f"可见区高 {mf.size.height:.0f}"
                     + ("  [主屏]" if f.origin.x == 0 and f.origin.y == 0 else ""))
    except Exception as e:
        L.append(f"  读取失败：{e}")
    L.append(f"  灵动岛显示位置：{cfg['island'].get('position')}"
             f" / 屏幕 {cfg['island'].get('monitor')}")
    L.append("")
    L.append("—— 登录项 ——")
    L.append(f"  开机自启：{'开' if autostart.is_on() else '关'}"
             f"（配置里 launch_at_login="
             f"{cfg['general'].get('launch_at_login')}）")
    L.append(f"  plist：{autostart.PLIST}")
    L.append("")
    L.append("—— 同类软件冲突 ——")
    found = False
    for name, bid, why in CONFLICTS:
        hit = False
        try:
            r = subprocess.run(["pgrep", "-x", name], capture_output=True,
                               timeout=5)
            hit = r.returncode == 0
        except Exception:
            pass
        if hit:
            found = True
            L.append(f"  ⚠️ {name} 正在运行 —— {why}")
    if not found:
        L.append("  未发现正在运行的同类切换器")
    try:
        r = subprocess.run(["ls", os.path.expanduser("~/Library/LaunchAgents")],
                           capture_output=True, text=True, timeout=5)
        for name, bid, why in CONFLICTS:
            if f"{bid}.plist" in (r.stdout or ""):
                L.append(f"  ⚠️ {name} 设了开机自启（{bid}.plist）—— {why}")
    except Exception:
        pass
    L.append("")
    L.append("—— 日志 ——")
    logp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.log")
    L.append(f"  {logp}")
    if os.path.exists(logp):
        try:
            with open(logp, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-15:]
            L.append("  最近 15 行：")
            for ln in tail:
                L.append("    " + ln.rstrip())
        except Exception as e:
            L.append(f"  读取失败：{e}")
    else:
        L.append("  （尚无日志；从 /Applications 启动时才会写）")
    return "\n".join(L)


class DiagController(NSObject):
    def init(self):
        self = objc.super(DiagController, self).init()
        self.win = None
        self.text = None
        return self

    @objc.python_method
    def build(self):
        W, H = 620, 520
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskResizable),
            NSBackingStoreBuffered, False)
        win.setTitle_(f"{APP_NAME} · 诊断与自检")
        win.setReleasedWhenClosed_(False)
        cv = win.contentView()

        self.text = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H - 60))
        self.text.setEditable_(False)
        self.text.setFont_(NSFont.userFixedPitchFontOfSize_(12))
        self.text.setAutomaticLinkDetectionEnabled_(False)
        sv = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 56, W - 24, H - 68))
        sv.setDocumentView_(self.text)
        sv.setHasVerticalScroller_(True)
        sv.setBorderType_(1)
        cv.addSubview_(sv)

        b1 = NSButton.alloc().initWithFrame_(NSMakeRect(12, 14, 130, 30))
        b1.setTitle_("重新检测")
        b1.setBezelStyle_(1)
        b1.setTarget_(self)
        b1.setAction_("actRefresh:")
        cv.addSubview_(b1)

        b2 = NSButton.alloc().initWithFrame_(NSMakeRect(W - 12 - 170, 14, 170, 30))
        b2.setTitle_("复制诊断信息")
        b2.setBezelStyle_(1)
        b2.setTarget_(self)
        b2.setAction_("actCopy:")
        cv.addSubview_(b2)

        self.win = win

    @objc.python_method
    def reload(self):
        if self.text is not None:
            self.text.setString_(collect_report())

    def actRefresh_(self, sender):
        self.reload()

    def actCopy_(self, sender):
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(collect_report(), "public.utf8-plain-text")

    @objc.python_method
    def show(self):
        if self.win is None:
            self.build()
        self.reload()
        center_on_main(self.win)
        self.win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


_diag = None


def open_diagnostics():
    global _diag
    if _diag is None:
        _diag = DiagController.alloc().init()
    _diag.show()
