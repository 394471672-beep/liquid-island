# -*- coding: utf-8 -*-
"""设置面板：一个真窗口 + WKWebView 承载 prefs.html。

为什么用 HTML 做设置界面而不是原生控件：
  玻璃拟态、圆角、悬停反馈这些用 CSS 几行就能写，原生控件要堆很多代码；
  以后加配置项也只改 HTML。
桥接：JS 通过 window.webkit.messageHandlers.bridge.postMessage(...) 回传，
Python 侧写 config.json。
"""
import json
import os
import subprocess
import sys

import objc
from AppKit import (
    NSApplication, NSBackingStoreBuffered, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject, NSURL
from WebKit import WKWebView, WKWebViewConfiguration, WKUserContentController

import config as cfgmod
from resources import resource_path

BASE = os.path.dirname(os.path.abspath(__file__))
HTML = resource_path("prefs.html")

_win = None
_holder = None      # 保持引用，避免被 GC 掉


class PrefsBridge(NSObject):
    """页面 ↔ Python 的桥。

    ⚠️ 非 ObjC 回调的方法必须加 @objc.python_method：
    PyObjC 会把 NSObject 子类的方法统统导出成 selector，
    名字一旦撞上系统私有方法（save / _save / cycle / _cycle…）就
    直接 BadPrototypeError 崩在 import 阶段（实测踩了好几个）。
    """

    def initWithCfg_(self, cfg):
        self = objc.super(PrefsBridge, self).init()
        if self is None:
            return None
        self.cfg = cfg
        self.web = None
        return self

    # ── WKScriptMessageHandler ────────────────────────────
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        try:
            body = message.body()
            if isinstance(body, dict):
                action, data = body.get("action"), body.get("config")
            else:
                action, data = str(body), None
            if action == "save" and isinstance(data, dict):
                self._save(data)
            elif action == "defaults":
                cfgmod.reset()
                self._push()
            elif action == "restart":
                self._restart()
            elif action == "onboard":
                self._open_onboard()
            elif action == "diagnostics":
                self._open_diagnostics()
            elif action == "openconfig":
                subprocess.Popen(["open", cfgmod.CONFIG_DIR])
        except Exception as e:
            print("[prefs] 消息处理失败:", e, flush=True)

    # ── WKNavigationDelegate ──────────────────────────────
    def webView_didFinishNavigation_(self, web, nav):
        self._push()

    @objc.python_method
    def _push(self):
        if self.web is None:
            return
        js = json.dumps(cfgmod.load(), ensure_ascii=False)
        self.web.evaluateJavaScript_completionHandler_(
            f"window.__loadConfig({js})", None)

    # ── 业务 ─────────────────────────────────────────────
    @objc.python_method
    def _save(self, data):
        """只覆盖支持改的键，配置结构里没有的键直接忽略。"""
        cfg = cfgmod.load()
        old_auto = bool(cfg["general"].get("launch_at_login", False))
        for section, values in data.items():
            if not isinstance(values, dict) or section not in cfg:
                continue
            for k, v in values.items():
                if isinstance(cfg[section].get(k), dict) and isinstance(v, dict):
                    cfg[section][k].update(v)
                else:
                    cfg[section][k] = v
        cfgmod.save(cfg)
        # 开机自启是「配置 + 系统登录项」两件事，要同步到 LaunchAgent
        new_auto = bool(cfg["general"].get("launch_at_login", False))
        if new_auto != old_auto:
            try:
                import autostart
                ok, msg = autostart.apply(new_auto)
                print(f"[prefs] 开机自启: {msg}", flush=True)
                if self.web is not None:
                    self.web.evaluateJavaScript_completionHandler_(
                        f"window.__toast({json.dumps(msg, ensure_ascii=False)})", None)
            except Exception as e:
                print("[prefs] 开机自启设置失败:", e, flush=True)
        print("[prefs] 配置已保存", flush=True)

    @objc.python_method
    def _open_onboard(self):
        """设置面板里点「权限引导」。"""
        try:
            import onboarding
            cfg = cfgmod.load()
            self.cfg = cfg
            onboarding.open_window(cfg)
        except Exception as e:
            print("[prefs] 打开引导失败:", e, flush=True)

    @objc.python_method
    def _open_diagnostics(self):
        """设置面板里点「诊断与自检」。"""
        try:
            import panels
            panels.open_diagnostics()
        except Exception as e:
            print("[prefs] 打开诊断失败:", e, flush=True)

    @objc.python_method
    def _restart(self):
        try:
            subprocess.Popen([sys.executable, os.path.join(BASE, "main.py")],
                             cwd=BASE)
        except Exception as e:
            print("[prefs] 重启失败:", e, flush=True)
        NSApplication.sharedApplication().terminate_(None)


def open_window(cfg):
    """打开（或前置）设置窗口。"""
    global _win, _holder
    app = NSApplication.sharedApplication()
    if _win is not None:
        _win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
        return _win

    holder = PrefsBridge.alloc().initWithCfg_(cfg)
    conf = WKWebViewConfiguration.alloc().init()
    ucc = WKUserContentController.alloc().init()
    ucc.addScriptMessageHandler_name_(holder, "bridge")
    conf.setUserContentController_(ucc)

    rect = NSMakeRect(0, 0, 1000, 780)
    web = WKWebView.alloc().initWithFrame_configuration_(rect, conf)
    web.setNavigationDelegate_(holder)
    holder.web = web

    mask = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
    win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, mask, NSBackingStoreBuffered, False)
    win.setTitle_("液态岛 · 设置")
    win.setContentView_(web)
    win.center()
    win.makeKeyAndOrderFront_(None)

    url = NSURL.fileURLWithPath_(HTML)
    web.loadFileURL_allowingReadAccessToURL_(
        url, url.URLByDeletingLastPathComponent())

    _win, _holder = win, holder
    app.activateIgnoringOtherApps_(True)
    return win
