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
import time

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
        self.on_saved = None      # 保存后回调（主进程用它做热更新）
        self.on_restart = None    # 重启回调（主进程版）
        self.on_quit = None       # 退出回调（主进程版）
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
            elif action == "quit":
                self._quit()
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
        # 立刻通知主进程热应用 —— 改位置/主题/尺寸要当场看到效果，不能等重启。
        # 这条走不到也没关系：main 的 tick 里还有配置文件指纹兜底。
        if self.on_saved is not None:
            try:
                self.on_saved(cfg)
            except Exception as e:
                print("[prefs] 通知主进程失败:", e, flush=True)
        else:
            try:
                self.web.evaluateJavaScript_completionHandler_(
                    "window.__toast('已保存并生效')", None)
            except Exception:
                pass

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
    def _quit(self):
        """退出整个程序（设置面板底部的「退出液态岛」）。

        菜单栏常驻应用没有 Dock 图标可右键退出，菜单栏那一项又可能因为
        没有标准 responder chain 被 AppKit 置灰，所以面板里必须留一个
        一定点得到的出口。
        """
        if self.on_quit is not None:
            try:
                self.on_quit()
                return
            except Exception as e:
                print("[prefs] 退出回调失败，自己退:", e, flush=True)
        try:
            NSApplication.sharedApplication().terminate_(None)
        except Exception:
            pass
        import threading

        def _hard():
            import time as _t
            _t.sleep(0.6)
            os._exit(0)

        threading.Thread(target=_hard, daemon=True).start()

    @objc.python_method
    def _restart(self):
        """重启主程序（兜底用；绝大多数配置现在都能热更新，不必重启）。

        ⚠️ 这里有个坑：如果本设置面板是**独立进程**（不是主程序自己开的），
        直接 Popen main.py 会被主程序的单实例保护拦掉 —— 旧进程还在跑，
        新的当场退出，用户看到的就是「点了重启，毫无反应」。
        所以独立进程时先让旧主程序退干净再拉新的。
        """
        if self.on_restart is not None:
            self.on_restart()          # 主进程自己重启（正常路径）
            return
        try:
            subprocess.run(["pkill", "-f", "liquid_island/main.py"], timeout=6)
            time.sleep(1.2)
            subprocess.Popen([sys.executable, os.path.join(BASE, "main.py")],
                             cwd=BASE, start_new_session=True)
        except Exception as e:
            print("[prefs] 重启失败:", e, flush=True)
        NSApplication.sharedApplication().terminate_(None)


def open_window(cfg, on_saved=None, on_restart=None, on_quit=None):
    """打开（或前置）设置窗口。

    on_saved(cfg)：保存后回调，主进程用它立刻热应用配置。
    on_restart()  ：重启回调，主进程用它重启自己。
    on_quit()     ：退出回调，主进程用它真正退出（不是只关面板）。

    这两个回调**建议一定传**：不传就说明设置面板跑在别的进程里，
    改完配置主进程感知不到（只能靠 tick 里的文件指纹兜底，慢半拍）。
    """
    global _win, _holder
    app = NSApplication.sharedApplication()
    if _win is not None:
        if _holder is not None:
            # 复用旧窗口时回调也要换成当前的（主进程可能重启过）
            if on_saved is not None:
                _holder.on_saved = on_saved
            if on_restart is not None:
                _holder.on_restart = on_restart
            if on_quit is not None:
                _holder.on_quit = on_quit
        _win.makeKeyAndOrderFront_(None)
        app.activateIgnoringOtherApps_(True)
        return _win

    holder = PrefsBridge.alloc().initWithCfg_(cfg)
    holder.on_saved = on_saved
    holder.on_restart = on_restart
    holder.on_quit = on_quit
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
    # ⚠️ 这行是救命的：NSWindow 默认 releasedWhenClosed=YES。
    # 不关掉它的话，用户把设置面板关了 → 窗口对象被系统释放，
    # 而模块里的 _win 还指向那块已释放的内存；下次点「打开设置」
    # 走 _win.makeKeyAndOrderFront_() → 野指针 → 整个 App SIGSEGV 崩掉。
    # 表现是「点了设置毫无反应」——其实是进程已经死了，岛也没了。
    win.setReleasedWhenClosed_(False)
    win.makeKeyAndOrderFront_(None)

    url = NSURL.fileURLWithPath_(HTML)
    web.loadFileURL_allowingReadAccessToURL_(
        url, url.URLByDeletingLastPathComponent())

    _win, _holder = win, holder
    app.activateIgnoringOtherApps_(True)
    return win
