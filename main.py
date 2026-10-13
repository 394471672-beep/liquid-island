# -*- coding: utf-8 -*-
"""液态岛（Liquid Island）· 入口

把「灵动岛」和「窗口切换器（AltTab 式）」组装成一个程序：
  · 灵动岛：屏幕顶部（或左上/右上）的玻璃胶囊，显示已打开的应用，悬停出窗口卡片
  · 切换器：按住 Option 连按 Tab 循环选窗口，松开即切换
  · 菜单栏图标：打开设置、重载配置、开关各模块、退出

事件循环只有这一个（AppHelper.runEventLoop），所以两个浮层共存在一个进程里。
"""
import fcntl
import os
import signal
import subprocess
import sys
import time

import objc
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSImage, NSLeftMouseDownMask,
    NSMenu, NSMenuItem, NSObject, NSRightMouseDownMask, NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSDistributedNotificationCenter, NSTimer
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
        self._cfg_stamp = None     # 配置文件指纹，变了就热更新
        self.menu_items = {}       # 菜单项引用（勾选状态要跟着配置同步）
        return self

    # ── 配置热更新 ────────────────────────────────────────
    @objc.python_method
    def cfg_stamp(self):
        """配置文件指纹（mtime + 大小）。

        为什么不只比 mtime：连续两次快速保存可能落在同一秒内，
        mtime 精度不够就会漏掉一次更新。
        """
        try:
            st = os.stat(cfgmod.CONFIG_PATH)
            return (st.st_mtime_ns, st.st_size)
        except Exception:
            return None

    @objc.python_method
    def apply_config(self, cfg=None):
        """把新配置应用到**正在运行**的各模块，不重启。

        两个触发来源：
          ① 设置面板保存后回调（最快，点完立刻生效）
          ② tick 里发现 config.json 指纹变了（兜底：手动改文件、
             或设置面板跑在别的进程里、回调没接上的情况）
        """
        cfg = cfg if cfg is not None else cfgmod.load()
        self.cfg = cfg

        island_on = bool(cfg["island"].get("enabled", True))
        sw_on = bool(cfg["switcher"].get("enabled", True))

        # 0) 先刷新 island 模块的全局变量（尺寸/主题/位置/它持有的 CFG）。
        #    必须放在最前面：下面若走到 build()（岛从关到开），
        #    IslandGroup 是读 island.CFG 决定建在哪个屏的，不刷就用了旧值。
        try:
            islandmod.reload_config(cfg)
        except Exception as e:
            print("[main] 刷新岛配置失败:", e, flush=True)

        # 1) 灵动岛：只有开关状态翻转时才建/销毁，其余一律原地重排
        #    （销毁重建会闪一下，还会丢掉展开状态和缩略图缓存）
        try:
            if island_on and self.island_ctrl is None:
                self.island_ctrl = islandmod.build()
            elif not island_on and self.island_ctrl is not None:
                self.island_ctrl.close()
                self.island_ctrl = None
            elif self.island_ctrl is not None:
                self.island_ctrl.apply_config(cfg)
            self.island_on = island_on
        except Exception as e:
            print("[main] 灵动岛应用新配置失败:", e, flush=True)

        # 2) 窗口切换器
        try:
            self.sw.apply_config(cfg)
        except Exception as e:
            print("[main] 切换器应用新配置失败:", e, flush=True)
        self.sw_on = sw_on

        # 3) 全局热键：按键组合和开关都可能改了
        try:
            sw_cfg = cfg["switcher"]
            self.mon.set_keys(sw_cfg.get("hold_key", "option"),
                              sw_cfg.get("cycle_key", "tab"))
            self.mon.set_options(sw_cfg.get("reverse_with_shift", True))
            if sw_on and not self.mon.running:
                self.mon.start()
            elif not sw_on and self.mon.running:
                self.mon.stop()
        except Exception as e:
            print("[main] 热键更新失败:", e, flush=True)

        # 4) 菜单栏勾选状态
        self.sync_menu_state()
        self._cfg_stamp = self.cfg_stamp()
        print("[main] 配置已生效（无需重启）", flush=True)

    @objc.python_method
    def sync_menu_state(self):
        it_island = self.menu_items.get("island")
        if it_island is not None:
            it_island.setState_(1 if self.island_on else 0)
        it_sw = self.menu_items.get("switcher")
        if it_sw is not None:
            it_sw.setState_(1 if self.sw_on else 0)

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

        # 4.5) 启动后让岛展开 3 秒：默认收起态只有 11px 透明条，
        # 用户打开后完全不知道有东西、以为「没反应」。给个直观反馈。
        self._intro_island()

        self._cfg_stamp = self.cfg_stamp()

        # 5) 主循环里的杂活：收切换器缩略图、看 STOP 文件
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.05, self, "tick:", None, True)

        # 5.5) 外部唤出入口。
        #   设置面板**必须**跑在本进程里：它要是另起一个进程，改的配置
        #   只写进了 json，主进程浑然不觉，「点完没反应」「重启才生效」
        #   就是这么来的。给一条分布式通知，外部也能让主进程自己开窗。
        try:
            NSDistributedNotificationCenter.defaultCenter() \
                .addObserver_selector_name_object_(
                    self, "onExternal:", "com.liquidisland.action", None)
        except Exception as e:
            print("[main] 外部通知注册失败:", e, flush=True)

        # 6) 首次运行 / 缺权限 → 弹引导（菜单栏常驻应用没有窗口，
        #    不弹的话用户只会觉得「装了没反应」）
        try:
            import onboarding
            if onboarding.should_show(self.cfg):
                # 主动申请一次：把液态岛登记进系统设置列表 + 触发系统授权弹窗。
                # 以前只有用户点引导窗「去授权」才登记——大量用户卡在
                # "系统设置列表里找不到这个 App"，表现就是「点击毫无反应」。
                # 权限按签名记账（ad-hoc 重签后会失效），失效时启动就重新申请。
                if not (core.has_ax_perm() and core.has_screen_perm()):
                    core.request_permissions()
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
        self.menu_items["island"] = it2

        it3 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "启用窗口切换器", "actToggleSwitcher_", "")
        it3.setTarget_(self)
        it3.setState_(1 if self.sw_on else 0)
        menu.addItem_(it3)
        self.menu_items["switcher"] = it3

        menu.addItem_(NSMenuItem.separatorItem())

        it4 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "重新载入配置", "actReload_", "r")
        it4.setTarget_(self)
        menu.addItem_(it4)

        it5 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出", "actQuit_", "q")
        it5.setTarget_(self)
        # 关掉「自动启用」：菜单栏应用没有标准 responder chain，
        # AppKit 有时会误判 action 不可用，把「退出」置灰点不动。
        # 退出必须永远可点，所以这里手动兜住。
        menu.setAutoenablesItems_(False)
        it5.setEnabled_(True)
        menu.addItem_(it5)

        menu.addItem_(NSMenuItem.separatorItem())

        it6 = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"关于 {APP_NAME}", "actAbout_", "")
        it6.setTarget_(self)
        menu.addItem_(it6)

        # 左键 / 右键 统一走按钮 action 弹同一个菜单（action-only 模式）。
        # ⚠️ 这里【不要】用 setMenu_(menu)：一旦菜单被 setMenu_ 挂到状态项上，
        # 再调 popUpStatusItemMenu_ 在部分 macOS 上会静默失效（右键点了没反应），
        # 正是之前反复出现的 bug。统一用 action 弹，最稳。
        # actStatusClick_ 必须是普通 def（见其注释）——@objc.python_method 会让
        # selector 不注册到 ObjC 运行时，导致按钮 action 找不到方法 → 右键毫无反应。
        self._status_menu = menu
        try:
            btn.setTarget_(self)
            btn.setAction_("actStatusClick_")
            btn.sendActionOn_(NSLeftMouseDownMask | NSRightMouseDownMask)
        except Exception as e:
            print("[main] 状态项点击适配失败:", e, flush=True)

    def actStatusClick_(self, sender):
        """右键点击菜单栏图标 → 弹出与左键同一个菜单。

        ⚠️ 这个方法【绝不能】加 @objc.python_method：那会让 selector 不注册到
        ObjC 运行时（respondsToSelector_ 返回 False），按钮的 action 找不到方法
        → 右键点了毫无反应（正是用户反复报的 bug）。必须是普通 def，PyObjC 才会
        把它暴露成 actStatusClick:。已用实验验证：@objc.python_method 装饰后
        respondsToSelector_("actStatusClick:") == False。
        """
        try:
            self.status_item.popUpStatusItemMenu_(self._status_menu)
        except Exception as e:
            print("[main] 弹出菜单失败:", e, flush=True)

    @objc.python_method
    def _intro_island(self):
        """启动后让灵动岛展开约 3 秒，给用户一个「它在工作」的直观反馈。

        默认收起态只有 11px 的透明小条，不看文档根本不知道有东西存在，
        用户打开后觉得「毫无反应」。展开一下，至少证明进程活着、岛能用。
        展开期间靠 island.Controller.intro_until 挡住自动收起。
        """
        import time as _t
        if not self.island_on or self.island_ctrl is None:
            return
        ctrls = getattr(self.island_ctrl, "ctrls", None)
        if not ctrls:
            return
        for c in ctrls:
            try:
                c.intro_until = _t.time() + 3.0
                c.expanded = True
                c.apply_geometry()
            except Exception:
                pass

    # ── 菜单动作 ──────────────────────────────────────────
    def actPrefs_(self, sender):
        try:
            import prefs
            # 传 on_saved：设置面板一保存就立刻热应用，
            # 不用等 tick 兜底，也不用重启（老大明确要求改掉「改完要重启」）。
            prefs.open_window(self.cfg, on_saved=self.on_prefs_saved,
                              on_restart=self.restart,
                              on_quit=lambda: self.actQuit_(None))
        except Exception as e:
            print("[main] 打开设置失败:", e, flush=True)

    @objc.python_method
    def on_prefs_saved(self, cfg):
        """设置面板保存后的回调（在 WKWebView 的回调线程上）。"""
        try:
            self.apply_config(cfg)
        except Exception as e:
            print("[main] 应用设置失败:", e, flush=True)

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
        """开关灵动岛：改配置后**立刻生效**，不重启。"""
        self.cfg["island"]["enabled"] = not self.island_on
        cfgmod.save(self.cfg)
        self.apply_config(self.cfg)

    def actToggleSwitcher_(self, sender):
        self.cfg["switcher"]["enabled"] = not self.sw_on
        cfgmod.save(self.cfg)
        self.apply_config(self.cfg)

    def actReload_(self, sender):
        """重新载入 config.json 并立刻生效。

        以前这个菜单是「重启进程」——配置文件被别的途径改了（手动编辑、
        设置面板跑在独立进程里）只能靠重启。现在直接热应用即可。
        """
        self.apply_config(cfgmod.load())

    def actQuit_(self, sender):
        """退出。

        ⚠️ 这里每一步都必须单独 try：以前 `self.mon.stop()` 抛一次异常
        （mon 还没建好 / 已经停过），后面的 terminate_ 就永远执行不到 ——
        用户点了「退出」却纹丝不动，看着就像菜单坏了。
        """
        try:
            open(STOP_FILE, "w").close()
        except Exception:
            pass
        try:
            if self.mon is not None:
                self.mon.stop()
        except Exception as e:
            print("[main] 停止监听失败:", e, flush=True)
        try:
            NSApplication.sharedApplication().terminate_(None)
        except Exception as e:
            print("[main] terminate 失败，强制退出:", e, flush=True)
        # 兜底：AppKit 不理我们的话，0.6 秒后自己了断，
        # 保证「点了退出一定退得掉」。
        import threading

        def _hard_exit():
            import time as _t
            _t.sleep(0.6)
            os._exit(0)

        threading.Thread(target=_hard_exit, daemon=True).start()

    @objc.python_method
    def restart(self):
        """重启自己。

        ⚠️ 单实例保护是 pgrep 查「还有没有别的 main.py 在跑」，而旧进程要等
        terminate 真正落地才消失。直接 Popen 新进程会被**自己的保护**拦掉，
        表现就是用户看到「点了重启，毫无反应」。
        所以交给一个脱离本进程的 shell：先睡 1.2 秒等旧的退干净，再拉新的。
        """
        import shlex
        cmd = ("sleep 1.2; cd %s; %s main.py"
               % (shlex.quote(BASE), shlex.quote(sys.executable)))
        try:
            subprocess.Popen(["/bin/sh", "-c", cmd], start_new_session=True)
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

    def onExternal_(self, note):
        """外部分布式通知 `com.liquidisland.action` → 在主进程里执行。

        object 取值：openprefs（开设置）/ reload（重载配置）/ quit（退出）。
        """
        try:
            act = str(note.object() or "")
        except Exception:
            act = ""
        try:
            if act == "openprefs":
                self.actPrefs_(None)
            elif act == "reload":
                self.actReload_(None)
            elif act == "quit":
                self.actQuit_(None)
        except Exception as e:
            print("[main] 外部指令处理失败:", e, flush=True)

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
            self._slow_tick += 1

            # 每 ≈0.5 秒：配置文件被动了就热应用。
            # 这是兜底路径——设置面板正常走 on_saved 回调（即时），
            # 但手动编辑 config.json、或设置面板跑在独立进程里时只能靠这里。
            if self._slow_tick % 10 == 0:
                try:
                    st = self.cfg_stamp()
                    if st is not None and st != self._cfg_stamp:
                        self.apply_config()
                except Exception as e:
                    print("[main] 配置变更检测失败:", e, flush=True)

            # 每 ≈2 秒：同步屏幕配置（插拔外接屏 / monitor=mouse 跟随）
            if self._slow_tick % 40 == 0 and self.island_ctrl is not None:
                try:
                    self.island_ctrl.sync_screens()
                except Exception:
                    pass

            # 每 ≈10 秒：把真实运行状态写到文件，方便排查「实时显示」问题
            # （权限到底生效没、窗口枚举到底看不看得见）。不走通知，避免
            # 分布式通知 selector 暴露问题导致崩溃。
            if self._slow_tick % 200 == 0:
                try:
                    import json as _json
                    info = {
                        "version": APP_VERSION,
                        "ax_perm": bool(core.has_ax_perm()),
                        "screen_perm": bool(core.has_screen_perm()),
                        "running_apps": len(core.running_apps()),
                        "windows_all": len(core.list_windows(scope="all-windows")),
                        "switcher_items": len(getattr(self.sw, "items", []) or []),
                        "island_apps_n": (
                            getattr(self.island_ctrl.ctrls[0], "n", None)
                            if (self.island_ctrl is not None
                                and getattr(self.island_ctrl, "ctrls", None))
                            else None),
                        "hotkey_running": bool(self.mon.running),
                    }
                    with open(os.path.expanduser(
                            "~/Library/Logs/liquid-island.state.json"), "w") as f:
                        _json.dump(info, f, ensure_ascii=False, indent=2)
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


LOCK_PATH = os.path.join(cfgmod.CONFIG_DIR, "instance.lock")
_LOCK_FH = None

# 命令行里只要出现任一条，就认定是「另一个液态岛」
_INSTANCE_PATTERNS = (
    "liquid_island/main.py",          # 源码直跑
    "Contents/MacOS/Liquid Island",   # 打包版（/Applications、dist、dmg 都命中）
    "液态岛.app/Contents/MacOS",
)


def _other_instances():
    """找出别的液态岛进程 —— 不管它是从源码、/Applications 还是 dmg 里起的。

    为什么原来会漏：老代码只 pgrep `liquid_island/main.py`，而打包后进程的
    命令行是 `/Applications/液态岛.app/Contents/MacOS/Liquid Island`，
    跟源码路径八竿子打不着；从 dmg 里直接双击又是 `/Volumes/液态岛/...`
    这第三条路径。于是三条路各起一个互不认识的实例：菜单栏三个图标、
    三个岛叠在一起、还各抢一份系统权限 —— 用户看到的就是
    「右上角怎么有三个液态岛」。
    """
    me = os.getpid()
    hits = []
    try:
        out = subprocess.run(["ps", "-ax", "-o", "pid=,command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return hits
    for line in out.splitlines():
        pid_str, _, cmd = line.strip().partition(" ")
        if not pid_str.isdigit():
            continue
        pid = int(pid_str)
        if pid == me:
            continue
        if any(p in cmd for p in _INSTANCE_PATTERNS):
            hits.append((pid, cmd.strip()[:110]))
    return hits


def _acquire_lock():
    """文件锁：比 pgrep 可靠（进程被 kill 时内核自动释放，不会留幽灵锁）。"""
    global _LOCK_FH
    try:
        os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
        _LOCK_FH = open(LOCK_PATH, "w")
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FH.write(str(os.getpid()))
        _LOCK_FH.flush()
        return True
    except OSError:
        return False


def _notify_existing():
    """已经在跑了：让那个实例把设置面板打开。

    否则用户再点一次 App 就是彻头彻尾的「毫无反应」——这正是之前最让人
    抓狂的地方。菜单栏应用没有主窗口，得给个看得见的反馈。
    """
    try:
        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_(
            "com.liquidisland.action", "openprefs")
    except Exception as e:
        print("[main] 通知已有实例失败:", e, flush=True)


def main():
    others = _other_instances()
    if others:
        print("[main] 已有实例在跑，本次启动放弃：", others, flush=True)
        _notify_existing()
        return
    if not _acquire_lock():
        print("[main] 单实例锁被占用，本次启动放弃", flush=True)
        _notify_existing()
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
