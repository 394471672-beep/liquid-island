# -*- coding: utf-8 -*-
"""全局热键：按住某个修饰键 + 反复按 Tab 循环选择窗口（AltTab 的交互）。

实现方式：CGEventTap 挂在会话级事件流上，**拦截**这些按键（返回 None 吞掉），
所以系统自带的 Cmd+Tab 不会同时弹出来抢戏。

⚠️ 回调里绝对不能做慢操作（AX 枚举、截图）——事件回调是同步的，
卡住会让整个键盘变卡。业务逻辑靠回调通知外部，外部自己异步处理。
"""
import Quartz

# 修饰键 → (flag 掩码, 左右键码)
HOLD_KEYS = {
    "option": (0x80000, {58, 61}),
    "cmd": (0x100000, {55, 54}),
    "ctrl": (0x40000, {59, 62}),
}
# 循环键 → 键码
CYCLE_KEYS = {"tab": 48, "grave": 50, "space": 49, "arrow-right": 124}

SHIFT_MASK = 0x20000
ESC_CODE = 53


class HotkeyMonitor(object):
    """监听「按住 hold + 按 cycle 循环」这个手势。

    回调：
      on_begin()           hold 键按下 → 准备切换器
      on_cycle(delta)      cycle 键按下（delta = +1 / -1，按 Shift 反向）
      on_commit()          hold 键松开 → 执行切换
      on_cancel()          Esc → 取消
    """

    def __init__(self, on_begin, on_cycle, on_commit, on_cancel):
        self.on_begin = on_begin
        self.on_cycle = on_cycle
        self.on_commit = on_commit
        self.on_cancel = on_cancel
        self.hold_key = "option"
        self.cycle_key = "tab"
        self.tap = None
        self.active = False
        self.swallow_cycle_up = False
        self.enabled = True
        self.reverse_shift = True   # Shift+循环键反向（switcher.reverse_with_shift）

    @property
    def running(self):
        """热键是否真的挂上了（授权后重试时用它判断）。"""
        return bool(self.tap is not None and self.active)

    # ── 配置 ──────────────────────────────────────────────
    def set_keys(self, hold_key, cycle_key):
        if hold_key in HOLD_KEYS:
            self.hold_key = hold_key
        if cycle_key in CYCLE_KEYS:
            self.cycle_key = cycle_key

    def set_options(self, reverse_with_shift=True):
        """Shift 是否反向循环。以前这个值写死在回调里，配置项没人读。"""
        self.reverse_shift = bool(reverse_with_shift)

    def _hold_mask(self):
        return HOLD_KEYS.get(self.hold_key, HOLD_KEYS["option"])[0]

    def _cycle_code(self):
        return CYCLE_KEYS.get(self.cycle_key, CYCLE_KEYS["tab"])

    # ── 生命周期 ──────────────────────────────────────────
    def start(self):
        if self.tap is not None:
            return True
        mask = (Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
                | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
                | Quartz.CGEventMaskBit(Quartz.kCGEventFlagsChanged))
        # 用 HID 层（kCGHIDEventTap）而不是 Session 层：
        # AltTab 这类同类工具挂在更底层，若我们用 Session 层，
        # 事件会先被它们吞掉，我们就永远收不到（实测踩到）。
        self.tap = Quartz.CGEventTapCreate(
            Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault, mask, self._callback, None)
        if self.tap is None:
            print("[hotkey] 创建事件监听失败（缺辅助功能权限）", flush=True)
            return False
        src = Quartz.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), src,
                                  Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self.tap, True)
        self.active = True
        return True

    def stop(self):
        if self.tap is not None:
            try:
                Quartz.CGEventTapEnable(self.tap, False)
            except Exception:
                pass
            self.tap = None
        self.active = False

    def reset(self):
        self.active = False
        self.swallow_cycle_up = False

    # ── 事件回调（必须极快） ───────────────────────────────
    def _callback(self, proxy, etype, event, refcon):
        try:
            if not self.enabled:
                return event
            hold_mask = self._hold_mask()
            cyc = self._cycle_code()

            if etype == Quartz.kCGEventFlagsChanged:
                flags = Quartz.CGEventGetFlags(event)
                down = bool(flags & hold_mask)
                if down and not self.active:
                    self.active = True
                    self.swallow_cycle_up = False
                    self.on_begin()
                elif not down and self.active:
                    self.active = False
                    self.on_commit()
                # 修饰键本身不吞（否则 Option 的其他组合都会失灵）
                return event

            kc = Quartz.CGEventGetIntegerValueField(
                event, Quartz.kCGKeyboardEventKeycode)

            if etype == Quartz.kCGEventKeyDown:
                if self.active and kc == cyc:
                    flags = Quartz.CGEventGetFlags(event)
                    self.swallow_cycle_up = True
                    rev = self.reverse_shift and bool(flags & SHIFT_MASK)
                    self.on_cycle(-1 if rev else 1)
                    return None         # 吞掉，避免系统也处理
                if self.active and kc == ESC_CODE:
                    self.active = False
                    self.swallow_cycle_up = False
                    self.on_cancel()
                    return None

            if etype == Quartz.kCGEventKeyUp:
                if kc == cyc and self.swallow_cycle_up:
                    if not self.active:
                        self.swallow_cycle_up = False
                    return None         # 循环键的抬起也吞掉（否则可能触发系统切换）
        except Exception as e:
            print("[hotkey] 回调异常:", e, flush=True)
        return event
