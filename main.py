# -*- coding: utf-8 -*-
"""液态岛（Liquid Island）· 入口

把「灵动岛」和「窗口切换器（AltTab 式）」组装成一个程序：
  · 灵动岛：屏幕顶部（或左上/右上）的玻璃胶囊，显示已打开的应用，悬停出窗口卡片
  · 切换器：按住 Option 连按 Tab 循环选窗口，松开即切换
  · 菜单栏图标：打开设置、重载配置、开关各模块、退出

事件循环只有这一个（AppHelper.runEventLoop），所以两个浮层共存在一个进程里。
"""
import os
import signal
import subprocess
import sys
import time

import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSMenu,
    NSMenuItem, NSObject, NSStatusBar, NSVariableStatusItemLength,
)
from Foundation import NSTimer
from PyObjCTools import AppHelper

import config as cfgmod
import core
import hotkey as hkmod
import island as islandmod
import switcher as swmod
from version import APP_NAME, APP_VERSION

BASE = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(BASE, "STOP")


class AppAgent(NSObject):
    """持有各个模块，负责把它们串起来。"""

    def init(self):
        self = objc.super(AppAgent, self).init()
        if self is None:
            return None
        self.cfg = cfgmod.load()
        self.island_ctrl = None
        self.sw = None
        self.mon = None
        self.status_item = None
        self.sw_on = bool(self.cfg["switcher"].get("enabled", True))
        self.island_on = bool(self.cfg["island"].get("enabled", True))
        self._slow_tick = 0        # tick 计数，用来把慢活摊到每 N 拍做一次
        return self

    # ── 装配 ─────────────────────────────────────────────
    @objc.python_method
    def setup(self):
        # 1) 灵动岛
        if self.island_on:
            self.island_ctrl = islandmod.build()

        # 2) 窗口切换器（面板预先建好，避免第一次按热键时现建导致卡顿）
        self.sw = swmod.Switcher(self.cfg)
        self.sw._ensure_window()

        # 3) 全局热键
        self.mon = hkmod.HotkeyMonitor(
            on_begin=self.on_begin, on_cycle=self.on_cycle,
            on_commit=self.on_commit, on_cancel=self.on_cancel)
        sw_cfg = self.cfg["switcher"]
        self.mon.set_keys(sw_cfg.get("hold_key", "option"),
                          sw_cfg.get("cycle_key", "tab"))
        if self.sw_on:
            if not self.mon.start():
                print("[main] 全局热键启动失败（缺辅助功能权限）", flush=True)

        # 4) 菜单栏
        if self.cfg["general"].get("show_menubar_icon", True):
            self.build_menubar()

        # 5) 主循环里的杂活：收切换器缩略图、看 STOP 文件
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "tick:", None, True)

        # 6) 首次运行 / 缺权限 → 弹引导（菜单栏常驻应用没有窗口，
        #    不弹的话用户只会觉得「装了没反应」）
        try:
            import onboarding
            if onboarding.should_show(self.cfg):
                onboarding.open_window(self.cfg, on_finish=self.on_perm_changed)
        except Exception as e:
            print("[main] 引导窗口失败:", e, flush=True)

        print("─" * 52, flush=True)
        print(f"{APP_NAME} {APP_VERSION} 已启动", flush=True)
        print(f"  灵动岛：{'开' if self.island_on else '关'}"
              f"  位置={self.cfg['island'].get('position')}"
              f"  主题={islandmod.THEME}", flush=True)
        print(f"  切换器：{'开' if self.sw_on else '关'}"
              f"  热键={self.cfg['switcher'].get('hold_key')}"
              f"+{self.cfg['switcher'].get('cycle_key')}"
              f"  范围={self.cfg['switcher'].get('scope')}", flush=True)
        print("  停止：菜单栏 → 退出，或建 STOP 文件", flush=True)
        if not core.has_ax_perm():
            print("  ⚠️ 缺「辅助功能」权限：窗口列表/切换不可用", flush=True)
        if not core.has_screen_perm():
            print("  ⚠️ 缺「屏幕录制」权限：缩略图不可用（不影响切换）", flush=True)

    @objc.python_method
    def build_menubar(self):
        bar = NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(NSVariableStatusItemLength)
        btn = self.status_item.button()
        try:
            img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                "capsule.portrait", "液态岛")
            if img is not None:
                btn.setImage_(img)
            else:
                btn.setTitle_("岛")
        except Exception:
            btn.setTitle_("岛")

        menu = NSMenu.alloc().init()

        it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "打开设置…", "actPrefs_", ",")
        it.setTarget_(self)
        menu.addItem_(it)

        it0 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "权限与引导…", "actOnboard_", "")
        it0.setTarget_(self)
        menu.addItem_(it0)

        it0b = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "诊断与自检…", "actDiag_", "")
        it0b.setTarget_(self)
        menu.addItem_(it0b)

        menu.addItem_(NSMenuItem.separatorItem())

        it2 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "启用灵动岛", "actToggleIsland_", "")
        it2.setTarget_(self)
        it2.setState_(1 if self.island_on else 0)
        menu.addItem_(it2)

        it3 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "启用窗口切换器", "actToggleSwitcher_", "")
        it3.setTarget_(self)
        it3.setState_(1 if self.sw_on else 0)
        menu.addItem_(it3)

        menu.addItem_(NSMenuItem.separatorItem())

        it4 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "重载配置（重启）", "actReload_", "r")
        it4.setTarget_(self)
        menu.addItem_(it4)

        it5 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出", "actQuit_", "q")
        it5.setTarget_(self)
        menu.addItem_(it5)

        menu.addItem_(NSMenuItem.separatorItem())

        it6 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"关于 {APP_NAME}", "actAbout_", "")
        it6.setTarget_(self)
        menu.addItem_(it6)

        self.status_item.setMenu_(menu)

    # ── 菜单动作 ──────────────────────────────────────────
    def actPrefs_(self, sender):
        try:
            import prefs
            prefs.open_window(self.cfg)
        except Exception as e:
            print("[main] 打开设置失败:", e, flush=True)

    def actOnboard_(self, sender):
        try:
            import onboarding
            onboarding.open_window(self.cfg, on_finish=self.on_perm_changed)
        except Exception as e:
            print("[main] 打开引导失败:", e, flush=True)

    def actDiag_(self, sender):
        try:
            import panels
            panels.open_diagnostics()
        except Exception as e:
            print("[main] 打开诊断失败:", e, flush=True)

    def actAbout_(self, sender):
        try:
            import panels
            panels.open_about()
        except Exception as e:
            print("[main] 打开关于失败:", e, flush=True)

    def actToggleIsland_(self, sender):
        """开关灵动岛：改配置 + 重启进程（最稳，避免半开半关状态）。"""
        self.cfg["island"]["enabled"] = not self.island_on
        cfgmod.save(self.cfg)
        self.restart()

    def actToggleSwitcher_(self, sender):
        self.cfg["switcher"]["enabled"] = not self.sw_on
        cfgmod.save(self.cfg)
        self.restart()

    def actReload_(self, sender):
        self.restart()

    def actQuit_(self, sender):
        try:
            open(STOP_FILE, "w").close()
        except Exception:
            pass
        self.mon.stop()
        NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def restart(self):
        """重启自己（改配置后最省事、最不容易出脏状态）。"""
        try:
            subprocess.Popen([sys.executable, os.path.join(BASE, "main.py")],
                             cwd=BASE)
        except Exception as e:
            print("[main] 重启失败:", e, flush=True)
        NSApplication.sharedApplication().terminate_(None)

    # ── 热键回调（在主线程，必须立刻返回） ─────────────────
    @objc.python_method
    def on_begin(self):
        if not self.sw_on:
            return
        print("[switcher] 热键触发", flush=True)
        if (self.island_ctrl is not None
                and self.cfg["general"].get("hide_island_while_switching", True)):
            self.island_ctrl.set_suspended(True)
        try:
            self.sw._begin()
        except Exception as e:
            print("[main] 切换器打开失败:", e, flush=True)

    @objc.python_method
    def on_cycle(self, delta):
        if self.sw_on and self.sw.visible:
            self.sw._cycle(delta)

    @objc.python_method
    def on_commit(self):
        if not self.sw.visible:
            self.resume_island()
            return
        self.sw._hide()
        # raise_window 里有 AX 调用 + 短暂 sleep，会阻塞主线程，
        # 所以排到下一拍执行，避免卡住键盘事件回调。
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.01, self, "doCommit:", None, False)

    def doCommit_(self, timer):
        try:
            i = self.sw.index
            items = self.sw.items
            if 0 <= i < len(items):
                core.raise_window(items[i])
        except Exception as e:
            print("[main] 切换失败:", e, flush=True)
        self.resume_island()

    @objc.python_method
    def on_cancel(self):
        self.sw._cancel()
        self.resume_island()

    @objc.python_method
    def resume_island(self):
        if self.island_ctrl is not None:
            self.island_ctrl.set_suspended(False)

    @objc.python_method
    def on_perm_changed(self):
        """引导里授权完成后回调：权限缓存清掉，并重试此前失败的热键。

        热键在无「辅助功能」权限时 start() 会失败，授权完必须再试一次，
        否则用户得手动重启应用才生效。
        """
        core.refresh_perm_cache()
        try:
            if self.sw_on and self.mon is not None and not self.mon.running:
                if self.mon.start():
                    print("[main] 权限已到位，热键已启用", flush=True)
        except Exception as e:
            print("[main] 重试热键失败:", e, flush=True)

    # ── 主循环杂活 ────────────────────────────────────────
    def tick_(self, timer):
        try:
            if self.sw is not None and self.sw.visible:
                self.sw._drain_thumbs()
                if self.sw.pending_reload:
                    self.sw.pending_reload = False
                    self.sw._reload()
            # 每 ≈2 秒：同步屏幕配置（插拔外接屏 / monitor=mouse 跟随）
            self._slow_tick += 1
            if self._slow_tick % 40 == 0 and self.island_ctrl is not None:
                try:
                    self.island_ctrl.sync_screens()
                except Exception:
                    pass

            if os.path.exists(STOP_FILE):
                try:
                    os.remove(STOP_FILE)
                except Exception:
                    pass
                self.mon.stop()
                NSApplication.sharedApplication().terminate_(None)
        except Exception as e:
            print("[main] tick 异常:", e, flush=True)


def _already_running():
    """单实例保护。

    开机自启的 LaunchAgent 直接跑 `python main.py`，绕过了
    /Applications/液态岛.app 启动器里的 pgrep 去重；这里补一道，
    否则登录时可能同时存在「上次没退干净的」和「新拉起的」两个进程，
    两个浮层两套热键互相打架。
    """
    me = os.getpid()
    try:
        out = subprocess.run(["pgrep", "-f", "liquid_island/main.py"],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    for tok in out.split():
        if tok.isdigit() and int(tok) != me:
            return True
    return False


def main():
    if _already_running():
        print("[main] 已有实例在跑，本次启动放弃", flush=True)
        return
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    try:
        os.remove(STOP_FILE)
    except Exception:
        pass

    core.request_permissions()

    agent = AppAgent.alloc().init()
    agent.setup()

    signal.signal(signal.SIGINT, lambda *a: app.terminate_(None))
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
