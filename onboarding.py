# -*- coding: utf-8 -*-
"""首次运行引导：把「去系统设置授权」变成看得见的界面。

为什么必须有这个：
  液态岛是菜单栏常驻应用（LSUIElement，不在 Dock 显示），没有主窗口。
  缺权限时原来只 print 到 stdout —— 用户根本看不到，表现就是「装完了什么都没发生」。
  自己用时还能翻日志，分发给朋友时这就等于软件是坏的。

为什么用原生 AppKit 控件而不是像设置面板那样用 WKWebView：
  引导页是第一个要弹出来的东西，**不能依赖 WebKit 加载成功**（打包、权限、
  沙箱都可能让它失败）。原生控件没有这个失败点，而且更像正经 Mac 软件。
"""

import os
import sys
import time

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSButton, NSColor, NSFont,
    NSImage, NSImageView, NSMakeRect, NSObject, NSScreen, NSTextField, NSTimer, NSView,
    NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskTitled, NSWorkspace,
)
from Foundation import NSURL

import config as cfgmod
import core
from resources import resource_path
from version import APP_NAME, APP_VERSION

# 系统设置里对应的隐私页（macOS 13+ 的 URL scheme）
URL_AX = ("x-apple.systempreferences:com.apple.preference.security"
          "?Privacy_Accessibility")
URL_SCREEN = ("x-apple.systempreferences:com.apple.preference.security"
              "?Privacy_ScreenCapture")

GREEN = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.16, 0.75, 0.42, 1.0)
ORANGE = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.62, 0.16, 1.0)
GREY = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.45, 0.45, 0.48, 1.0)

W, H = 560, 452


def app_assets_dir():
    """定位 assets 目录（图标）。

    源码运行时在代码旁边；py2app 打包后被打进 Contents/Resources，
    所以要顺着可执行文件路径再找一层。两种都要覆盖。
    """
    return os.path.dirname(resource_path("assets", "AppIcon.icns"))


def center_on_main(win):
    """把窗口居中到【主屏】（含菜单栏那块，CGMainDisplayID 对应的屏）。

    ⚠️ 不能用 NSWindow.center()：多屏布局下它可能选到副屏——引导窗、
    设置窗弹在副屏，用户盯着主屏就等于「毫无反应」（实测踩过：
    引导窗弹在 x=3240 的副屏，老大在主屏什么都看不到）。
    ⚠️ PyObjC 方法名：setFrame_display_（不是 setFrame_！后者会解析成
    不存在的 setFrame: selector，AttributeError 被 except 吞掉后
    静默 fallback 回 center()——又弹副屏，实测踩过）。
    """
    try:
        from Quartz import CGMainDisplayID
        main_id = CGMainDisplayID()
        screens = NSScreen.screens()
        target = None
        for s in screens:
            desc = s.deviceDescription()
            if desc and desc.get("NSScreenNumber") == main_id:
                target = s
                break
        if target is None:
            target = screens[0] if screens else None
        if target is None:
            win.center()
            return
        sf = target.frame()
        wf = win.frame()
        x = sf.origin.x + (sf.size.width - wf.size.width) / 2.0
        y = sf.origin.y + (sf.size.height - wf.size.height) / 2.0
        win.setFrame_display_(NSMakeRect(x, y, wf.size.width, wf.size.height), True)
    except Exception:
        try:
            win.center()
        except Exception:
            pass


def _y(top, height):
    """从上往下的布局坐标 → AppKit 的 y（原点在左下）。"""
    return H - top - height


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


class OnboardController(NSObject):
    """引导窗口。既是窗口持有者，也是所有按钮的 target。"""

    def initWithCfg_(self, cfg):
        self = objc.super(OnboardController, self).init()
        if self is None:
            return None
        self.cfg = cfg
        self.win = None
        self.rows = {}
        self.hint = None
        self.done_btn = None
        self.timer = None
        self.on_finish = None        # 权限状态变了，main 可以据此重试热键
        self.on_restart = None       # 授权刚办好 → 一键重启（改授权后必须重启 App 才生效）
        self._auto_closing = False   # 两项都开了之后自动收起（只触发一次）
        return self

    # ── 建窗口 ────────────────────────────────────────────
    @objc.python_method
    def build_window(self):
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable,
            NSBackingStoreBuffered, False)
        win.setTitle_(f"{APP_NAME} · 需要两项权限")
        win.setDelegate_(self)
        win.setReleasedWhenClosed_(False)
        cv = win.contentView()

        # 图标
        iv = NSImageView.alloc().initWithFrame_(NSMakeRect((W - 88) / 2, _y(26, 88), 88, 88))
        p = os.path.join(app_assets_dir(), "AppIcon.icns")
        if os.path.exists(p):
            img = NSImage.alloc().initWithContentsOfFile_(p)
            if img is not None:
                iv.setImage_(img)
        cv.addSubview_(iv)

        title = _label(f"欢迎使用{APP_NAME}", 20, bold=True)
        title.setFrame_(NSMakeRect(0, _y(130, 30), W, 30))
        title.setAlignment_(1)          # 居中
        cv.addSubview_(title)

        sub = _label("灵动岛 + 窗口切换器。先给两项系统权限才能正常工作。", 13,
                     color=GREY)
        sub.setFrame_(NSMakeRect(0, _y(160, 22), W, 22))
        sub.setAlignment_(1)
        cv.addSubview_(sub)

        # 分隔线
        sep = NSView.alloc().initWithFrame_(NSMakeRect(24, _y(196, 1), W - 48, 1))
        sep.setWantsLayer_(True)
        sep.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.5, 0.28).CGColor())
        cv.addSubview_(sep)

        self.rows["ax"] = self._row(cv, 214, "辅助功能",
                                   "列出所有窗口、切换应用——切换器的核心，必须开",
                                   "actGrantAx:")
        self.rows["screen"] = self._row(cv, 286, "屏幕录制",
                                        "显示窗口实时缩略图。不开也能切换，只是没有预览图",
                                        "actGrantScreen:")

        self.hint = _label("", 12, color=GREY)
        self.hint.setFrame_(NSMakeRect(24, _y(358, 40), W - 48, 40))
        self.hint.setLineBreakMode_(0)      # 自动换行
        cv.addSubview_(self.hint)

        later = NSButton.alloc().initWithFrame_(
            NSMakeRect(W - 24 - 118 - 10 - 118, _y(406, 34), 118, 34))
        later.setTitle_("稍后再说")
        later.setBezelStyle_(1)             # NSBezelStyleRounded
        later.setTarget_(self)
        later.setAction_("actLater:")
        cv.addSubview_(later)

        self.done_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(W - 24 - 118, _y(406, 34), 118, 34))
        self.done_btn.setTitle_("开始使用")
        self.done_btn.setBezelStyle_(1)
        self.done_btn.setTarget_(self)
        self.done_btn.setAction_("actDone:")
        cv.addSubview_(self.done_btn)

        self.win = win

    @objc.python_method
    def _row(self, parent, top, name, desc, action):
        """一行权限：状态灯 + 名称 + 说明 + 右侧按钮。返回控件字典。"""
        dot = _label("●", 15)
        dot.setFrame_(NSMakeRect(24, _y(top + 6, 18), 18, 18))
        parent.addSubview_(dot)

        t = _label(name, 14, bold=True)
        t.setFrame_(NSMakeRect(48, _y(top + 2, 22), 260, 22))
        parent.addSubview_(t)

        d = _label(desc, 12, color=GREY)
        d.setFrame_(NSMakeRect(48, _y(top + 26, 18), W - 48 - 118 - 24, 18))
        parent.addSubview_(d)

        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(W - 24 - 118, _y(top + 14, 34), 118, 34))
        btn.setTitle_("去授权")
        btn.setBezelStyle_(1)
        btn.setTarget_(self)
        btn.setAction_(action)
        parent.addSubview_(btn)
        return {"dot": dot, "btn": btn}

    # ── 显示 ──────────────────────────────────────────────
    @objc.python_method
    def show(self):
        if self.win is None:
            self.build_window()
        else:
            core.refresh_perm_cache()
        self.refresh()
        center_on_main(self.win)
        self.win.makeKeyAndOrderFront_(None)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        # 用户在系统设置里勾选后，回到这里状态要自己变绿
        if self.timer is None:
            self.timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    0.8, self, "obTick:", None, True))

    def obTick_(self, timer):
        core.refresh_perm_cache()
        self.refresh()

    def obAutoClose_(self, timer):
        """两项权限都到齐：自动收起引导页（1.5 秒后，让用户看见灯变绿）。"""
        self.finish(mark=True)

    @objc.python_method
    def refresh(self):
        ax = core.has_ax_perm()
        sc = core.has_screen_perm()
        self._set_row(self.rows["ax"], ax)
        self._set_row(self.rows["screen"], sc)

        if ax and sc:
            self.hint.setStringValue_(
                "两项权限都已开启。注意：改动授权后必须重启液态岛才生效——"
                "点右下角「重启并开始使用」。"
                if self.on_restart is not None else
                "两项权限都已开启，可以开始使用了。")
            self.done_btn.setTitle_("重启并开始使用" if self.on_restart is not None
                                    else "开始使用")
            self.done_btn.setKeyEquivalent_("\r")
            # 用户从系统设置勾完切回来，灯变绿就该自己收起来。
            # 一直挂着的话，看起来就像「软件卡在引导页没反应」。
            # 但带了 on_restart 时【不能】自动收：授权改动必须重启 App 才生效，
            # 自动收了用户就错过重启，表现还是「勾了权限却没反应」。挂着等他点。
            if not self._auto_closing and self.on_restart is None:
                self._auto_closing = True
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.5, self, "obAutoClose:", None, False)
        elif ax:
            self.hint.setStringValue_(
                "辅助功能已开启，切换器可用。想要窗口缩略图的话再开一下屏幕录制。"
                "（勾选后系统可能会要求重启本应用）")
            self.done_btn.setKeyEquivalent_("")
        else:
            self.hint.setStringValue_(
                "还没开「辅助功能」—— 没它的话窗口列表是空的，切换器完全用不了。"
                "（勾选后系统可能会要求重启本应用）")
            self.done_btn.setKeyEquivalent_("")

    @objc.python_method
    def _set_row(self, row, ok):
        row["dot"].setTextColor_(GREEN if ok else ORANGE)
        if ok:
            row["btn"].setTitle_("已开启")
            row["btn"].setEnabled_(False)
        else:
            row["btn"].setTitle_("去授权")
            row["btn"].setEnabled_(True)

    # ── 动作 ──────────────────────────────────────────────
    def actGrantAx_(self, sender):
        # 先主动申请一次：这样本应用才会出现在系统设置的列表里，用户才勾得到
        core.request_permissions()
        self._open(URL_AX)

    def actGrantScreen_(self, sender):
        self._open(URL_SCREEN)

    def actLater_(self, sender):
        # 真「稍后」：7 天内不再自动弹。否则用户只要一直不开辅助功能，
        # 每次登录都会被窗口打扰一次 —— 那就不是引导，是骚扰了。
        self.finish(mark=True, snooze=7 * 86400)

    def actDone_(self, sender):
        self.finish(mark=True)
        # 授权刚办好：改动授权后必须重启 App 才生效（当前进程拿不到新授权），
        # 主动帮一把，省得用户又「点了开始使用却什么都没发生」。
        if (self.on_restart is not None
                and core.has_ax_perm() and core.has_screen_perm()):
            self.on_restart()

    def windowWillClose_(self, notification):
        # 只收窗、停轮询，【不】标 onboarded。
        # 用户关窗 ≠ 看过引导；而且进程被杀/重启时系统也会发 windowWillClose，
        # 以前这里 mark 会把 onboarded 误标成 True——之后权限再失效时
        # 引导逻辑被打乱，用户干瞪眼（实测踩过：fresh 测试反复不弹）。
        self.finish(mark=False)

    @objc.python_method
    def _open(self, url):
        try:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
        except Exception as e:
            print("[onboard] 打开系统设置失败:", e, flush=True)

    @objc.python_method
    def finish(self, mark=True, snooze=0):
        """关窗口、停轮询、记下「引导看过了」（可选：静默一段时间）。"""
        if self.timer is not None:
            self.timer.invalidate()
            self.timer = None
        if mark and not self.cfg["general"].get("onboarded"):
            self.cfg["general"]["onboarded"] = True
        if snooze:
            self.cfg["general"]["onboard_snooze_until"] = time.time() + snooze
        try:
            cfgmod.save(self.cfg)
        except Exception:
            pass
        if self.win is not None and self.win.isVisible():
            self.win.orderOut_(None)
        if self.on_finish is not None:
            self.on_finish()


_controller = None


def open_window(cfg, on_finish=None, on_restart=None):
    """打开（或前置）引导窗口。

    on_restart：授权办好后供「重启并开始使用」按钮调用（main 传 AppAgent.restart）。
    不传（如 prefs 里调用）时按钮就是普通的「开始使用」。
    """
    global _controller
    if _controller is None:
        _controller = OnboardController.alloc().initWithCfg_(cfg)
    _controller.cfg = cfg
    _controller.on_finish = on_finish
    _controller.on_restart = on_restart
    _controller.show()
    return _controller


def should_show(cfg):
    """要不要在启动时自动弹。

    规则：没看过引导 → 弹；缺「辅助功能」→ 弹（没它切换器是死的，
    而且用户换了启动方式时权限按进程记账会静默失效）；
    但用户点过「稍后再说」的话，静默期内不打扰。
    """
    if not cfg["general"].get("onboarded", False):
        return True
    if core.has_ax_perm():
        return False
    return time.time() > float(cfg["general"].get("onboard_snooze_until", 0))
