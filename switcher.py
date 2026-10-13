# -*- coding: utf-8 -*-
"""窗口切换器浮层（AltTab 的交互，自绘实现）。

交互：按住 Option 不放 → 反复按 Tab 循环选 → 松开 Option 切过去。Esc 取消。
点某格也能直接切。空格/方向键同样可用。

性能上的两个关键约束（都是实测踩出来的）：
  1. 热键回调在主线程且必须**立即返回** → 浮层先用缓存/占位显示，
     枚举与截图都排到主循环的空闲时机做
  2. AX 调用**只能在主线程**（后台线程会静默卡死）；
     但截图必须在后台线程（最小化窗口会让调用挂起）
"""
import math
import time

import objc
import Quartz
from AppKit import (
    NSPanel, NSApplication, NSBackingStoreBuffered, NSColor, NSScreen,
    NSStatusWindowLevel, NSView, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel,
)
from Foundation import NSMakeRect, NSTimer, NSObject

import core

# 每格：缩略图区高度 = 宽度 * 比例
CELL_RATIO = 0.62
LABEL_H = 44
CELL_PAD = 10


def hex_to_rgb(h, alpha=1.0):
    h = (h or "").lstrip("#")
    if len(h) != 6:
        h = "4DC1FF"
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


THEMES = {
    "glass": {"bg": (0.10, 0.10, 0.13, 0.74), "cell": (1, 1, 1, 0.07),
              "edge": 0.24, "text": (0.94, 0.94, 0.96, 1.0)},
    "dark": {"bg": (0.04, 0.04, 0.06, 0.97), "cell": (1, 1, 1, 0.06),
             "edge": 0.16, "text": (0.90, 0.90, 0.92, 1.0)},
    "light": {"bg": (0.96, 0.96, 0.98, 0.86), "cell": (0.0, 0.0, 0.0, 0.05),
              "edge": 0.30, "text": (0.12, 0.12, 0.15, 1.0)},
}


def no_anim():
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)


def anim(dur=0.18, ease="easeOut"):
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setAnimationDuration_(dur)
    Quartz.CATransaction.setAnimationTimingFunction_(
        Quartz.CAMediaTimingFunction.functionWithName_(ease))


def commit():
    Quartz.CATransaction.commit()


# ══════════ 浮层视图 ══════════

class SwitchView(NSView):
    """只负责画：压暗层 + 网格 + 选中高亮。"""

    def drawRect_(self, rect):
        pass            # 全用 CALayer，这里不画


# ══════════ 切换器 ══════════

class Switcher(object):
    """窗口切换器（**普通 Python 类**）。

    ⚠️ 刻意不继承 NSObject：一旦继承，PyObjC 会把每个方法当 selector 导出，
    撞上系统私有方法就 BadPrototypeError 直接崩（实测 `cycle`、`_cycle` 都撞过）。
    刷新任务因此不用 NSTimer，改成主循环轮询驱动（pending_reload，见 main.py 的 tick）。
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.pending_reload = False     # 主循环看到它就去枚举窗口
        self.panel = None
        self.view = None
        self.visible = False
        self.items = []            # 窗前列表（core.list_windows 的产物）
        self.cells = []            # 每格的图层组
        self.index = 0
        self._thumb_token = 0      # 防止过期的截图结果覆盖新列表
        self._thumb_pending = []
        self._busy_thumbs = False
        self.on_commit = None      # 切换完成后的回调（灵动岛用来恢复显示）
        self.on_show = None
        self._thumb_result = None
        self._cell_size = (300, 230)
        self._grid_origin = (0, 0)
        self._grid_gap = 18
        self._grid_cols = 4

    # ── 窗口 ─────────────────────────────────────────────
    def _ensure_window(self):
        if self.panel is not None:
            return
        sc = core.main_screen()
        f = sc.frame()
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(f.origin.x, f.origin.y, f.size.width, f.size.height),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        panel.setLevel_(NSStatusWindowLevel + 1)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(False)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary)
        view = SwitchView.alloc().initWithFrame_(
            NSMakeRect(0, 0, f.size.width, f.size.height))
        view.setWantsLayer_(True)
        panel.setContentView_(view)
        self.panel = panel
        self.view = view
        self._build_layers()

    def _build_layers(self):
        """建好图层骨架（压暗层 + 网格底 + 最大格数）。"""
        ov = self.view
        sw = self.cfg["switcher"]
        th = THEMES.get(sw.get("theme", "glass"), THEMES["glass"])

        self.dim = Quartz.CALayer.alloc().init()
        self.dim.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(
                0.0, float(sw.get("background_dim", 0.18))).CGColor())
        ov.layer().addSublayer_(self.dim)

        self.board = Quartz.CALayer.alloc().init()
        self.board.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
            *th["bg"]).CGColor())
        self.board.setCornerRadius_(float(sw.get("corner_radius", 22)))
        self.board.setBorderWidth_(1.0)
        self.board.setBorderColor_(NSColor.colorWithCalibratedWhite_alpha_(
            1.0, th["edge"]).CGColor())
        self.board.setShadowColor_(NSColor.blackColor().CGColor())
        self.board.setShadowOpacity_(0.45)
        self.board.setShadowRadius_(28)
        self.board.setShadowOffset_((0, -10))
        ov.layer().addSublayer_(self.board)

        # 预建最多 12 格
        max_cells = max(4, int(sw.get("columns", 4)) * 3)
        self.cells = []
        for _ in range(max_cells):
            cb = Quartz.CALayer.alloc().init()
            cb.setCornerRadius_(14)
            cb.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
                *th["cell"]).CGColor())
            cb.setBorderWidth_(1.0)
            cb.setBorderColor_(NSColor.colorWithCalibratedWhite_alpha_(
                1.0, 0.10).CGColor())
            cb.setMasksToBounds_(True)

            ci = Quartz.CALayer.alloc().init()          # 缩略图
            ci.setContentsGravity_("resizeAspectFill")
            ci.setContentsScale_(2.0)
            ci.setMasksToBounds_(True)
            ci.setCornerRadius_(10)
            ci.setBackgroundColor_(NSColor.colorWithCalibratedWhite_alpha_(
                0.0, 1.0).CGColor())

            ic = Quartz.CALayer.alloc().init()          # 小应用图标
            ic.setContentsGravity_("resizeAspect")
            ic.setContentsScale_(2.0)
            ic.setCornerRadius_(6)
            ic.setMasksToBounds_(True)

            lb = Quartz.CATextLayer.alloc().init()      # 标题
            lb.setFontSize_(13)
            lb.setAlignmentMode_("center")
            lb.setTruncationMode_("middle")
            lb.setContentsScale_(2.0)
            lb.setForegroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(
                *th["text"]).CGColor())

            for l in (ci, ic, lb):
                cb.addSublayer_(l)
            self.board.addSublayer_(cb)
            self.cells.append({"bg": cb, "img": ci, "icon": ic, "label": lb})

    def _rebuild_layers(self):
        """按新配置重建图层骨架（列数 / 主题变了才需要）。"""
        try:
            self.view.layer().setSublayers_(None)
        except Exception:
            pass
        self.cells = []
        self._build_layers()

    def apply_config(self, cfg):
        """配置热更新。

        切换器只在按住热键时才可见，所以分两种情况处理：
        · 没打开 —— 换配置引用就行，下次 _begin() 自然用上
          （_layout_board / _reload 都是现读 self.cfg 的）
        · 正打开 —— 重排 + 重画，让你立刻看到效果

        只有「图层骨架级」的配置（列数、主题）才需要重建图层；
        缩略图大小、间距这类每次 _layout_board() 都会重算，不必重建。
        """
        old = self.cfg.get("switcher", {}) if isinstance(self.cfg, dict) else {}
        skel_changed = (
            old.get("columns") != cfg["switcher"].get("columns")
            or old.get("theme") != cfg["switcher"].get("theme")
            or old.get("background_dim") != cfg["switcher"].get("background_dim")
            or old.get("corner_radius") != cfg["switcher"].get("corner_radius"))
        self.cfg = cfg

        if self.panel is None:
            return                       # 窗口还没建，下次 _ensure_window 会用新值
        if skel_changed:
            self._rebuild_layers()
        if self.visible:
            try:
                self._layout_board()
                self._render()
                self._start_thumbs()
            except Exception as e:
                print("[switcher] 应用新配置失败:", e, flush=True)

    # ── 开始 / 结束 ──────────────────────────────────────
    def _begin(self):
        """热键按下时调用：立即显示（用缓存），并排一次刷新。"""
        self._ensure_window()
        self.index = 0
        self.visible = True
        self._layout_board()
        self.panel.orderFrontRegardless()
        if self.on_show:
            self.on_show()
        # 先用已有列表撑场面，再异步刷新（AX 必须在主线程 → 用定时器排到下一拍）
        self._render()
        # 窗口枚举必须在主线程（AX 的限制），但热键回调必须立刻返回，
        # 所以只置个标志，由 main 的 tick 去跑（≈0.05s 后）。
        self.pending_reload = True

    def _hide(self):
        self.visible = False
        if self.panel is not None:
            self.panel.orderOut_(None)
        self._thumb_token += 1

    # ── 布局 ─────────────────────────────────────────────
    def _layout_board(self):
        f = self.view.bounds()
        no_anim()
        self.dim.setFrame_(f)
        commit()

        cols = max(1, int(self.cfg["switcher"].get("columns", 4)))
        n = max(1, min(len(self.items) or 1, len(self.cells)))
        rows = max(1, math.ceil(n / cols))
        tw = float(self.cfg["switcher"].get("thumb_size", 300))
        gap = float(self.cfg["switcher"].get("gap", 18))
        sw = tw
        sh = tw * CELL_RATIO + LABEL_H
        board_w = cols * sw + (cols + 1) * gap
        board_h = rows * sh + (rows + 1) * gap
        fw, fh = f.size.width, f.size.height
        board_w = min(board_w, fw - 80)
        board_h = min(board_h, fh - 120)
        bx = (fw - board_w) / 2
        by = (fh - board_h) / 2 + 20

        anim(0.16)
        self.board.setFrame_(NSMakeRect(bx, by, board_w, board_h))
        commit()

        self._cell_size = (sw, sh)
        self._grid_origin = (bx, by)
        self._grid_gap = gap
        self._grid_cols = cols

    # ── 数据 ─────────────────────────────────────────────
    def _reload(self):
        sw = self.cfg["switcher"]
        self.items = core.list_windows(scope=sw.get("scope", "all-windows"),
                                       sort=sw.get("sort", "recent"))
        if self.index >= len(self.items):
            self.index = 0
        self._layout_board()
        self._render()
        self._start_thumbs()

    def _start_thumbs(self):
        """后台线程补缩略图（最小化窗口直接跳过——截它会挂起）。"""
        if not self.cfg["windows"].get("thumbs", True):
            return
        if not core.has_screen_perm():
            return
        if self._busy_thumbs or not self.items:
            return
        self._busy_thumbs = True
        self._thumb_token += 1
        token = self._thumb_token
        todo = list(self.items)
        max_w = int(float(self.cfg["switcher"].get("thumb_size", 300)) * 2)
        max_h = int(max_w * CELL_RATIO)

        def work():
            out = []
            try:
                with objc.autorelease_pool():
                    for wd in todo:
                        if wd.get("min"):
                            out.append(None)
                            continue
                        try:
                            r = core.window_thumb(wd, max_w, max_h)
                            out.append(r[0] if r else None)
                        except Exception:
                            out.append(None)
            except Exception:
                pass
            self._thumb_result = (token, out)
            self._busy_thumbs = False

        import threading
        self._thumb_result = None
        threading.Thread(target=work, daemon=True).start()

    def _drain_thumbs(self):
        """主循环里收截图结果。"""
        res = getattr(self, "_thumb_result", None)
        if res is None:
            return
        self._thumb_result = None
        token, imgs = res
        if token != self._thumb_token or not self.visible:
            return
        no_anim()
        for i, cg in enumerate(imgs):
            if i < len(self.cells) and cg is not None:
                self.cells[i]["img"].setContents_(cg)
        commit()

    # ── 渲染 ─────────────────────────────────────────────
    def _render(self):
        sw = self.cfg["switcher"]
        th = THEMES.get(sw.get("theme", "glass"), THEMES["glass"])
        accent = hex_to_rgb(self.cfg["island"].get("accent", "#4DC1FF"))
        sw_cfg = self.cfg["switcher"]
        cols = self._grid_cols
        cw, ch = self._cell_size
        bx, by = self._grid_origin
        gap = self._grid_gap
        n = len(self.items)

        no_anim()
        for i, cell in enumerate(self.cells):
            if i >= n:
                cell["bg"].setOpacity_(0.0)
                continue
            col = i % cols
            row = i // cols
            # 从上往下排（CALayer y 向上，所以要反过来）
            rows_total = math.ceil(n / cols)
            # 坐标要相对 **board**（cells 是 board 的子层）。
            # 这里如果再加 board 原点 (bx,by) 就会偏出去——实测卡片会跑到屏幕顶部。
            cx = gap + col * (cw + gap) + cw / 2
            cy = (rows_total - 1 - row) * (ch + gap) + gap + ch / 2

            cell["bg"].setBounds_(NSMakeRect(0, 0, cw, ch))
            cell["bg"].setPosition_((cx, cy))
            cell["bg"].setOpacity_(1.0)

            it = self._item_at(i)
            thumb_h = cw * CELL_RATIO
            cell["img"].setBounds_(NSMakeRect(0, 0, cw - CELL_PAD * 2, thumb_h))
            cell["img"].setPosition_((cw / 2, ch - thumb_h / 2 - CELL_PAD))

            if sw.get("show_icons", True) and it is not None:
                cell["icon"].setBounds_(NSMakeRect(0, 0, 34, 34))
                cell["icon"].setPosition_((CELL_PAD + 17, LABEL_H / 2 + 4))
                cell["icon"].setOpacity_(1.0)
                # 图标图片按 pid 缓存：render 会被频繁调用，不能每次都重新渲染图标
                pid = it["pid"]
                if cell.get("icon_pid") != pid:
                    cell["icon_pid"] = pid
                    cell["icon"].setContents_(core.icon_cgimage(it["app"], 68))
            else:
                cell["icon"].setOpacity_(0.0)

            if sw.get("show_title", True) and it is not None:
                label = it["title"] or it["app_name"]
                cell["label"].setString_(label[:38])
                cell["label"].setBounds_(
                    NSMakeRect(0, 0, cw - (56 if sw.get("show_icons", True) else 20), 18))
                cell["label"].setPosition_(
                    (cw / 2 + (22 if sw.get("show_icons", True) else 0),
                     LABEL_H / 2 + 4))
            else:
                cell["label"].setString_("")

            selected = (i == self.index)
            if selected:
                cell["bg"].setBorderWidth_(2.5)
                cell["bg"].setBorderColor_(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(*accent).CGColor())
                cell["bg"].setShadowColor_(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(*accent).CGColor())
                cell["bg"].setShadowOpacity_(0.55)
                cell["bg"].setShadowRadius_(18)
                cell["bg"].setShadowOffset_((0, 0))
                cell["bg"].setBackgroundColor_(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(
                        min(1.0, th["cell"][0] + accent[0] * 0.18),
                        min(1.0, th["cell"][1] + accent[1] * 0.18),
                        min(1.0, th["cell"][2] + accent[2] * 0.18),
                        min(1.0, th["cell"][3] + 0.05)).CGColor())
            else:
                cell["bg"].setBorderWidth_(1.0)
                cell["bg"].setBorderColor_(NSColor.colorWithCalibratedWhite_alpha_(
                    1.0, 0.10).CGColor())
                cell["bg"].setShadowOpacity_(0.0)
                cell["bg"].setBackgroundColor_(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(*th["cell"]).CGColor())
        commit()

    def _item_at(self, i):
        return self.items[i] if 0 <= i < len(self.items) else None

    # ── 交互 ─────────────────────────────────────────────
    def _cycle(self, delta):
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)
        self._render()

    def _commit_selection(self):
        it = self._item_at(self.index)
        self._hide()
        if it is not None:
            core.raise_window(it)
        if self.on_commit:
            self.on_commit()

    def _cancel(self):
        self._hide()
        if self.on_commit:
            self.on_commit()
