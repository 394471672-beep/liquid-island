#!/usr/bin/env python3
"""
灵动岛 · 应用切换器 v3 —— 隐藏款：平时是一条细线，鼠标滑到屏幕顶部才展开。

交互（参考 GitHub 上的 boring.notch / Peninsula 的悬停展开范式）：
    · 收起态：屏幕顶部中央一条极细的半透明小条（几乎不占视线）
    · 鼠标滑到顶部中央（或碰到小条）→ 展开成完整胶囊，图标依次淡入
    · 鼠标移开 → 0.55 秒后自动收起
    · 悬停图标：图标放大（Dock 式距离驱动）+ 弹出该应用的**窗口缩略图**
    · 单击图标：切换到该应用
    · 右键图标：菜单「退出 XXX」
    · 点最右侧 ✕：关闭灵动岛

不占 Dock（Accessory 模式），不抢键盘焦点。
停止：点 ✕ / 终端 Ctrl+C / 建 STOP 文件。

首次使用窗口缩略图需要「屏幕录制」权限（系统设置 → 隐私与安全性 → 屏幕录制）；
没授权时自动降级为只显示应用名。
"""

import os
import re
import signal
import sys
import threading
import time

import objc
import Quartz
from ApplicationServices import (
    AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
    AXUIElementPerformAction, AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
)
from AppKit import (
    NSAnimationContext,
    NSApplication, NSApplicationActivationPolicyAccessory, NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered, NSBitmapImageRep, NSColor, NSEvent, NSImage, NSMenu, NSMenuItem,
    NSPanel, NSScreen, NSStatusBar, NSStatusWindowLevel, NSView,
    NSVisualEffectBlendingModeBehindWindow, NSVisualEffectMaterialHUDWindow,
    NSVisualEffectStateActive, NSVisualEffectView, NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorStationary, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel, NSWorkspace, NSWorkspaceOpenConfiguration,
)
from Foundation import NSMakeRect, NSMakePoint, NSSize, NSTimer, NSObject
from PyObjCTools import AppHelper

import config as cfgmod

BASE = os.path.dirname(os.path.abspath(__file__))
STOP_FILE = os.path.join(BASE, "STOP")

# ── 配置 ──────────────────────────────────────────────────
# 所有可调项都在 ~/.config/liquid-island/config.json（设置面板改的就是它）。
# 改完配置要重启本程序才生效。
CFG = cfgmod.load()
_ISL = CFG["island"]
_ICO = _ISL["icons"]
_WINCFG = CFG["windows"]

# ── 尺寸 ──────────────────────────────────────────────────
SCALE = float(_ISL.get("scale", 1.8))


def S(v):
    """尺寸缩放（统一取整，避免半像素发虚）。"""
    return int(round(v * SCALE))


ICON = S(_ICO.get("size", 38))       # 图标显示边长（点）
GAP = S(_ICO.get("gap", 10))         # 图标间距
PAD = S(_ICO.get("pad", 16))         # 胶囊左右内边距
CAP_H = S(58)           # 展开态胶囊高度
CLOSE_W = S(28)         # 右侧关闭按钮区宽
ICON_BOTTOM = S(13)     # 图标距胶囊底边（给下方小圆点留位置）
DOT_SIZE = S(5)         # 当前应用指示圆点直径

HIDDEN_W = S(132)       # 收起态横杠宽（参考 iOS 下拉横杠）
HIDDEN_H = max(8, S(6))         # 收起态横杠高

POPUP_NAME_H = S(32)    # 名字条弹出区高度
POPUP_THUMB_H = S(178)  # 缩略图卡片弹出区高度
THUMB_W = S(232)        # 缩略图卡片宽
THUMB_MAX_H = S(142)    # 缩略图最大高度

# 窗口列表（按窗口切换）
MAX_WINS = int(_WINCFG.get("max_cards", 4))   # 最多显示几张窗口卡片
WIN_CARD_W = S(196)     # 单个窗口卡片宽
WIN_CARD_IMG_H = S(116) # 卡片里缩略图区高
WIN_CARD_H = S(142)     # 卡片总高（图 + 标题）
WIN_GAP = S(8)          # 卡片间距
WIN_POPUP_H = WIN_CARD_H + S(14)

TOP_GAP = S(5)          # 距菜单栏下沿的间隙
TRIGGER_W = S(460)      # 顶部中央触发热区宽度（比小条宽，好碰到）
TRIGGER_H = S(16)       # 触发热区高度（贴屏幕顶部）

# ── 主题 ──────────────────────────────────────────────────
# glass 玻璃拟态（半透，透出桌面）| dark iOS 原味（近纯黑）
# light 浅色玻璃（浅底深字）| auto 跟随系统深浅色
def _detect_auto_theme():
    try:
        from AppKit import NSApplication, NSAppearanceNameDarkAqua
        nm = NSApplication.sharedApplication().effectiveAppearance() \
            .bestMatchFromAppearancesWithNames_(
                [NSAppearanceNameDarkAqua, "NSAppearanceNameAqua"])
        return "dark" if "DarkAqua" in str(nm) else "light"
    except Exception:
        return "glass"


THEME = str(_ISL.get("theme", "glass"))
if THEME == "auto":
    THEME = _detect_auto_theme()

#            底色浓度, 玻璃边缘亮度, 文字用白(1)/黑(0)
_THEME_SPEC = {
    "glass": (0.42, 0.30, 1),
    "dark": (0.92, 0.14, 1),
    "light": (0.34, 0.55, 0),
}
MASK_ALPHA, GLASS_EDGE, _TEXT_WHITE = _THEME_SPEC.get(THEME, _THEME_SPEC["glass"])
GLASS_SHADOW = 0.34   # 玻璃柔和外阴影不透明度
ACCENT_HEX = str(_ISL.get("accent", "#4DC1FF"))


def accent_rgba(alpha=1.0):
    h = ACCENT_HEX.lstrip("#")
    if len(h) != 6:
        h = "4DC1FF"
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return (r, g, b, alpha)


def text_color(alpha=1.0):
    """主题对应的文字色（浅色主题用深字）。"""
    return NSColor.colorWithCalibratedWhite_alpha_(
        1.0 if _TEXT_WHITE else 0.10, alpha)


def card_bg_rgba():
    """卡片底色：深色主题用深底，浅色主题用浅底。"""
    if _TEXT_WHITE:
        return (0.10, 0.10, 0.13, 0.70)
    return (1.0, 1.0, 1.0, 0.62)

# ── 动效 ──────────────────────────────────────────────────
MAG_MAX = float(_ICO.get("magnify", 1.45))     # 悬停图标放大倍数
MAG_LINK = 1.0 + (MAG_MAX - 1.0) * 0.36        # 邻近图标联动（跟着主倍率走）
MAG_DIST = S(_ICO.get("magnify_dist", 72))     # 放大影响半径（点）

EXPAND_DUR = 0.26    # 展开/收起动画时长
POP_DUR = 0.24       # 弹窗出现动画时长
POP_DRAG_DUR = 0.38  # 卡片「拖拽展开」时长（弹簧，稍长才有回弹感）
POP_DRAG_SX = 0.24   # 拖拽起始横向缩放（先窄）
POP_DRAG_SY = 0.05   # 拖拽起始纵向缩放（先扁成一条线）
HOVER_DWELL = float(_WINCFG.get("dwell", 0.13))   # 停稳多久才加载窗口卡片
POP_SCALE = 0.82     # 弹窗起始缩放（从多小弹出来）
NAME_FADE = 0.13
APP_FADE = 0.22
COLLAPSE_DELAY = float(_ISL.get("auto_collapse", 0.55))
THUMB_TTL = float(_WINCFG.get("cache_ttl", 30.0))

ICON_PX = int(ICON * MAG_MAX * 2)   # 图标位图渲染分辨率（放大后仍清晰）
THUMB_PX_W = THUMB_W * 2            # 缩略图渲染分辨率（Retina）

SKIP_BUNDLES = {
    "com.apple.dock", "com.apple.controlcenter", "com.apple.notificationcenterui",
    "com.apple.WindowManager", "com.apple.systemuiserver",
    "com.tencent.workbuddy.mac.island",
}


# ── Core Animation 小工具 ────────────────────────────────

def begin_anim(duration=0.24, ease="easeOut"):
    """带隐式动画的事务（用于布局变化、弹窗出现）。"""
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setAnimationDuration_(duration)
    Quartz.CATransaction.setAnimationTimingFunction_(
        Quartz.CAMediaTimingFunction.functionWithName_(ease))


def no_anim():
    Quartz.CATransaction.begin()
    Quartz.CATransaction.setDisableActions_(True)


def commit():
    Quartz.CATransaction.commit()


def scale_layer(layer, s):
    no_anim()
    layer.setTransform_(Quartz.CATransform3DMakeScale(s, s, 1.0))
    commit()


def fade(layer, to_value, duration, key="fade", ease="easeOut"):
    anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
    anim.setFromValue_(layer.opacity())
    anim.setToValue_(to_value)
    anim.setDuration_(duration)
    anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(ease))
    layer.addAnimation_forKey_(anim, key)
    layer.setOpacity_(to_value)


# ── 应用列表 / 激活 / 图标 ────────────────────────────────

def running_apps():
    ws = NSWorkspace.sharedWorkspace()
    out = []
    for a in ws.runningApplications():
        try:
            if a.activationPolicy() != NSApplicationActivationPolicyRegular:
                continue
            if (a.bundleIdentifier() or "") in SKIP_BUNDLES:
                continue
            if not a.localizedName():
                continue
            out.append(a)
        except Exception:
            continue
    return out


def activate_app(a):
    ws = NSWorkspace.sharedWorkspace()
    url = a.bundleURL()
    if url is not None:
        try:
            cfg = NSWorkspaceOpenConfiguration.configuration()
            cfg.setActivates_(True)
            ws.openApplicationAtURL_configuration_completionHandler_(url, cfg, None)
            return True
        except Exception:
            pass
    try:
        a.unhide()
        a.activateWithOptions_(3)
    except Exception:
        pass
    try:
        import subprocess
        subprocess.Popen(["open", "-a", url.path() if url is not None
                          else (a.localizedName() or "")])
    except Exception:
        pass
    return False


def _draw_scaled(img, pw, ph):
    """把 NSImage 高质量重绘到指定像素尺寸，返回 CGImage。

    直接拿原始大图交给 CALayer 缩放会被快速滤波糊掉，
    预先按目标像素重绘最清晰。
    """
    if img is None or pw <= 0 or ph <= 0:
        return None
    target = NSImage.alloc().initWithSize_((pw, ph))
    if target is None:
        return None
    target.lockFocus()
    src = img.size()
    img.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, pw, ph),
        NSMakeRect(0, 0, src.width, src.height),
        2,      # NSCompositingOperationSourceOver
        1.0,
    )
    target.unlockFocus()
    rep = NSBitmapImageRep.imageRepWithData_(target.TIFFRepresentation())
    if rep is None:
        return None
    rep.setSize_(NSSize(pw, ph))
    return rep.CGImage()


def icon_cgimage(app):
    """应用图标 → 清晰 CGImage（按放大后的像素尺寸渲染）。"""
    try:
        return _draw_scaled(app.icon(), ICON_PX, ICON_PX)
    except Exception:
        return None


_SCREEN_PERM = None
_AX_PERM = None


def has_ax_perm():
    """是否已获得「辅助功能」权限（枚举/切换窗口需要）。

    **没有权限时必须跳过窗口枚举**：AX 调用会一直等目标应用响应，
    实测每次要卡 2 秒超时，鼠标一动就卡住整个界面。
    """
    global _AX_PERM
    if _AX_PERM is None:
        try:
            import ApplicationServices as AS
            _AX_PERM = bool(AS.AXIsProcessTrusted())
        except Exception:
            _AX_PERM = False
    return _AX_PERM


def has_screen_perm():
    """是否已获得「屏幕录制」权限。

    没有权限时调用 CGWindowListCreateImage 不会报错，
    而是**静默挂起进程**（实测卡死主循环），所以必须先问清楚。
    """
    global _SCREEN_PERM
    if _SCREEN_PERM is None:
        try:
            _SCREEN_PERM = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            _SCREEN_PERM = False
    return _SCREEN_PERM


def request_permissions():
    """缺权限时**主动申请一次**，让本程序出现在系统设置列表里。

    为什么必须主动申请：macOS 的 TCC 权限按「进程」记账。从终端跑时归到宿主
    （WorkBuddy），双击 .app 跑时归到「灵动岛」自己——两边权限互不通用。
    不主动申请的话，用户想去勾选都找不到「灵动岛」这一项。

    授权后需要**重启本程序**才生效。
    """
    global _SCREEN_PERM
    # 屏幕录制（窗口缩略图用）
    try:
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
    except Exception:
        pass
    # 辅助功能（枚举/切换窗口用）
    try:
        import ApplicationServices as AS
        if not AS.AXIsProcessTrusted():
            AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass
    _SCREEN_PERM = None      # 重置缓存，让后面的检查反映最新状态


def app_window_image(app):
    """兼容旧接口：取该应用最大可见窗口的缩略图。"""
    wins = app_windows(app)
    if not wins:
        return None
    r = window_thumb(wins[0])
    return r


# ── 按窗口切换（AX 枚举 + 精确提升） ─────────────────────
# 说明：CGWindowList 里混着大量「幽灵窗口」（多个 app 都有同位置同尺寸的
# 不可见窗口，且没有 AX 对应物），所以**以 AX 的窗口列表为准**，
# CG 只用来按位置匹配拿窗口 id（用于截图）。

_PT_RE = re.compile(r"x:([-\d.]+)\s*y:([-\d.]+)")
_SZ_RE = re.compile(r"w:([-\d.]+)\s*h:([-\d.]+)")


def _ax_attr(elem, attr):
    err, val = AXUIElementCopyAttributeValue(elem, attr, None)
    return val if err == 0 else None


def _ax_pos(elem):
    v = _ax_attr(elem, "AXPosition")
    if v is None:
        return None
    m = _PT_RE.search(str(v))
    return (float(m.group(1)), float(m.group(2))) if m else None


def _ax_size(elem):
    v = _ax_attr(elem, "AXSize")
    if v is None:
        return None
    m = _SZ_RE.search(str(v))
    return (float(m.group(1)), float(m.group(2))) if m else None


def _ax_title(elem):
    v = _ax_attr(elem, "AXTitle")
    return str(v) if v else ""


def _ax_minimized(elem):
    v = _ax_attr(elem, "AXMinimized")
    return bool(v) if v is not None else False


def _cg_windows(pid):
    """CGWindowList 侧：该 pid 的 layer-0 窗口（含 id，用于截图）。"""
    out = []
    try:
        for w in Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID):
            if w.get("kCGWindowOwnerPID") != pid:
                continue
            if int(w.get("kCGWindowLayer", 0)) != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            out.append({
                "id": w.get("kCGWindowNumber"),
                "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
                "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0)),
            })
    except Exception:
        pass
    return out


def app_windows(app, max_n=MAX_WINS, screen_w=None):
    """该应用的真实窗口列表，按面积从大到小。

    每项：{"ax": AX元素, "title", "pos", "size", "cgid", "min"}
    """
    try:
        pid = app.processIdentifier()
        elem = AXUIElementCreateApplication(pid)
        # 关键：AX 调用默认会一直等目标应用响应（后台线程里会卡死），
        # 设 0.4 秒超时，卡住就放弃这个应用。
        try:
            AXUIElementSetMessagingTimeout(elem, 2.0)
        except Exception:
            pass
        wins = _ax_attr(elem, "AXWindows")
        if not wins:
            return []
        cgs = _cg_windows(pid)
        if screen_w is None:
            screen_w = 2560.0

        out = []
        for w in list(wins):
            p, s = _ax_pos(w), _ax_size(w)
            if not p or not s:
                continue
            if s[0] < 200 or s[1] < 140:
                continue
            title = _ax_title(w)
            # 桌面窗口（无标题 + 铺满整屏）不是可切换的窗口
            if not title and s[0] >= screen_w * 0.9:
                continue
            cgid = None
            for c in cgs:
                if abs(c["x"] - p[0]) <= 4 and abs(c["y"] - p[1]) <= 4:
                    cgid = c["id"]
                    break
            out.append({"ax": w, "title": title, "pos": p, "size": s,
                        "cgid": cgid, "min": _ax_minimized(w)})
        out.sort(key=lambda x: -(x["size"][0] * x["size"][1]))
        return out[:max_n]
    except Exception:
        return []


def window_thumb(wd):
    """截某个窗口的缩略图。

    实测：CGWindowList 与 AXWindows 的坐标体系对不上（同名窗口位置/尺寸
    都不一样），所以**不依赖 CGWindowID**，直接用 AX 给的位置截屏幕区域。
    返回 (cgimage, 宽高比) 或 None。
    """
    if not has_screen_perm():
        return None
    try:
        cg = None
        cgid = wd.get("cgid")
        pos, size = wd.get("pos"), wd.get("size")
        if cgid:
            # 匹配到了 CG 窗口 → 截该窗口本身（被遮挡也能拿到内容）
            cg = Quartz.CGWindowListCreateImage(
                Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
                cgid, Quartz.kCGWindowImageBoundsIgnoreFraming)
        if cg is None and pos and size and size[0] >= 60 and size[1] >= 60:
            # 退化方案：截屏幕上该窗口所在的矩形区域
            x, y, w, h = float(pos[0]), float(pos[1]), float(size[0]), float(size[1])
            sf = main_screen().frame()
            # 只在可见屏幕范围内截（跨屏/越界会失败）
            if x < sf.origin.x - 4000 or x > 20000:
                return None
            cg = Quartz.CGWindowListCreateImage(
                Quartz.CGRectMake(x, y, w, h), Quartz.kCGWindowListOptionOnScreenOnly,
                Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
        if cg is None:
            return None
        iw = Quartz.CGImageGetWidth(cg)
        ih = Quartz.CGImageGetHeight(cg)
        if not iw or not ih:
            return None
        nsimg = NSImage.alloc().initWithCGImage_size_(cg, NSSize(iw, ih))
        if nsimg is None:
            return None
        ratio = ih / float(iw)
        pw = int(WIN_CARD_W * 2)
        ph = int(pw * ratio)
        if ph > WIN_CARD_IMG_H * 2:
            ph = int(WIN_CARD_IMG_H * 2)
            pw = int(ph / ratio)
        return (_draw_scaled(nsimg, pw, ph), ratio)
    except Exception:
        return None


def raise_window(app, item):
    """精确把某个窗口提到最前（含从最小化恢复）。"""
    try:
        try:
            elem0 = AXUIElementCreateApplication(app.processIdentifier())
            AXUIElementSetMessagingTimeout(elem0, 0.8)
        except Exception:
            pass
        if item.get("min"):
            AXUIElementSetAttributeValue(item["ax"], "AXMinimized", False)
        activate_app(app)
        time.sleep(0.06)
        AXUIElementPerformAction(item["ax"], "AXRaise")
        try:
            elem = AXUIElementCreateApplication(app.processIdentifier())
            AXUIElementSetAttributeValue(elem, "AXFocusedWindow", item["ax"])
        except Exception:
            pass
        return True
    except Exception as e:
        print("提升窗口失败:", e, flush=True)
        activate_app(app)
        return False


def main_screen():
    for s in NSScreen.screens():
        f = s.frame()
        if f.origin.x == 0 and f.origin.y == 0:
            return s
    return NSScreen.mainScreen()


def mouse_screen():
    """鼠标当前所在的屏。

    monitor 设为 mouse 时用它：灵动岛跟着你正在操作的那块屏走，
    多屏用户不用为了够到岛而把鼠标甩到主屏去。
    """
    try:
        p = NSEvent.mouseLocation()
    except Exception:
        return main_screen()
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= p.x <= f.origin.x + f.size.width
                and f.origin.y <= p.y <= f.origin.y + f.size.height):
            return s
    return main_screen()


def target_screens(monitor=None):
    """按 monitor 配置给出要显示的屏列表。

      main   只主屏（原点屏）—— 原来的行为
      mouse  鼠标所在屏（单屏实例，位置动态跟随）
      all    每块屏各一个
    """
    m = monitor or CFG["island"].get("monitor", "main")
    try:
        screens = list(NSScreen.screens())
    except Exception:
        screens = [main_screen()]
    if not screens:
        return [main_screen()]
    if m == "mouse":
        return [mouse_screen()]
    if m == "all":
        return screens
    return [main_screen()]


def screen_signature():
    """屏配置的指纹（数量 + 每屏 frame）。变了就说明要重建。"""
    try:
        return tuple((round(f.origin.x), round(f.origin.y),
                      round(f.size.width), round(f.size.height))
                     for f in (s.frame() for s in NSScreen.screens()))
    except Exception:
        return ()


# ── 覆盖层图层（模块级函数，避免 PyObjC selector 冲突） ────

def ov_init_layers(ov):
    ov.icon_layers = []
    ov.dot_layers = []
    ov.icon_cxs = []
    ov.apps = []
    ov.hover = -1
    ov.popup = None           # None | "name" | "thumb"
    ov.close_hover = False
    ov.right_target = -1
    ov.quit_request = None
    ov.quit_confirm = False
    ov.win_click = -1
    ov.win_mouse = []
    ov.last_scales = None

    # 黑色蒙版：**必须最先 addSublayer** → 永远垫在最底层。
    # 它盖在毛玻璃（另一个 NSView）之上、图标/卡片之下，
    # 所以胶囊呈现黑色而不遮内容。
    ov.mask = Quartz.CALayer.alloc().init()
    ov.mask.setBackgroundColor_(NSColor.blackColor().CGColor())
    ov.mask.setCornerRadius_(CAP_H / 2)
    ov.mask.setOpacity_(0.0)
    # —— 玻璃拟态：亮边 + 柔和外阴影（模拟玻璃的厚度与折光）——
    ov.mask.setBorderWidth_(1.0)
    ov.mask.setBorderColor_(
        NSColor.colorWithCalibratedWhite_alpha_(1.0, GLASS_EDGE).CGColor())
    ov.mask.setShadowColor_(NSColor.blackColor().CGColor())
    ov.mask.setShadowOpacity_(GLASS_SHADOW)
    ov.mask.setShadowRadius_(S(14))
    ov.mask.setShadowOffset_((0, -S(5)))     # 向下投（CALayer 的 y 向上）
    ov.layer().addSublayer_(ov.mask)

    # —— 玻璃顶部高光（渐变，只在胶囊上半部，底部渐隐所以看不出直边）——
    ov.gloss = Quartz.CAGradientLayer.alloc().init()
    ov.gloss.setColors_([
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.22).CGColor(),
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.0).CGColor(),
    ])
    ov.gloss.setStartPoint_((0.5, 1.0))
    ov.gloss.setEndPoint_((0.5, 0.0))
    ov.gloss.setCornerRadius_(CAP_H / 2)
    ov.gloss.setMasksToBounds_(True)
    ov.gloss.setOpacity_(0.0)
    ov.layer().addSublayer_(ov.gloss)

    # 名字条
    ov.name_bg = Quartz.CALayer.alloc().init()
    ov.name_bg.setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.06, 0.06, 0.09, 0.97).CGColor())
    ov.name_bg.setCornerRadius_(S(13))
    ov.name_bg.setShadowColor_(NSColor.blackColor().CGColor())
    ov.name_bg.setShadowOpacity_(0.34)
    ov.name_bg.setShadowRadius_(S(12))
    ov.name_bg.setShadowOffset_((0, -S(5)))
    ov.name_bg.setBorderWidth_(1.0)
    ov.name_bg.setBorderColor_(NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.14).CGColor())
    ov.name_bg.setOpacity_(0.0)

    ov.name_text = Quartz.CATextLayer.alloc().init()
    ov.name_text.setFontSize_(S(12))
    ov.name_text.setForegroundColor_(text_color().CGColor())
    ov.name_text.setAlignmentMode_("center")
    ov.name_text.setContentsScale_(2.0)
    ov.name_text.setOpacity_(0.0)

    # 退出确认条（右键菜单的替代：nonactivating 窗口弹不出系统菜单）
    ov.quit_card = Quartz.CALayer.alloc().init()
    ov.quit_card.setBackgroundColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(0.62, 0.13, 0.15, 0.97).CGColor())
    ov.quit_card.setCornerRadius_(S(13))
    ov.quit_card.setBorderWidth_(1.0)
    ov.quit_card.setBorderColor_(
        NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.45, 0.45, 0.55).CGColor())
    ov.quit_card.setOpacity_(0.0)

    ov.quit_text = Quartz.CATextLayer.alloc().init()
    ov.quit_text.setFontSize_(S(12))
    ov.quit_text.setForegroundColor_(NSColor.whiteColor().CGColor())
    ov.quit_text.setAlignmentMode_("center")
    ov.quit_text.setContentsScale_(2.0)
    ov.quit_text.setOpacity_(0.0)

    # 窗口卡片组（一个窗口一张卡，横排）
    ov.win_cards = []
    ov.win_n = 0
    for _ in range(MAX_WINS):
        card = Quartz.CALayer.alloc().init()
        # 玻璃卡片：半透明深色底（透出桌面）+ 亮边 + 柔和阴影
        card.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                *card_bg_rgba()).CGColor())
        card.setCornerRadius_(S(11))
        card.setBorderWidth_(1.0)
        card.setBorderColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.26).CGColor())
        # 注意：不能开 masksToBounds，否则自身的阴影会被裁掉。
        # 圆角由 cornerRadius 保证；图内容由 img 图层自己裁。
        card.setMasksToBounds_(False)
        card.setShadowColor_(NSColor.blackColor().CGColor())
        card.setShadowOpacity_(0.42)
        card.setShadowRadius_(S(13))
        card.setShadowOffset_((0, -S(6)))
        # 锚点在**顶部中心**：拖拽展开时从胶囊下沿往外长（拟物感来源）
        card.setAnchorPoint_((0.5, 1.0))
        card.setOpacity_(0.0)

        img = Quartz.CALayer.alloc().init()
        img.setContentsGravity_("resizeAspectFill")
        img.setContentsScale_(2.0)
        img.setMasksToBounds_(True)
        img.setCornerRadius_(S(11))
        img.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(0.06, 1.0).CGColor())

        label = Quartz.CATextLayer.alloc().init()
        label.setFontSize_(S(11))
        label.setForegroundColor_(text_color(0.92).CGColor())
        label.setAlignmentMode_("center")
        label.setTruncationMode_("middle")
        label.setContentsScale_(2.0)

        card.addSublayer_(img)
        card.addSublayer_(label)
        ov.layer().addSublayer_(card)
        ov.win_cards.append({"card": card, "img": img, "label": label})

    # iOS 式收起横杠
    ov.bar = Quartz.CALayer.alloc().init()
    ov.bar.setBackgroundColor_(
        NSColor.colorWithCalibratedWhite_alpha_(
            1.0 if _TEXT_WHITE else 0.15, 0.66).CGColor())
    ov.bar.setCornerRadius_(HIDDEN_H / 2)
    ov.bar.setOpacity_(1.0)

    # 关闭按钮
    ov.close_text = Quartz.CATextLayer.alloc().init()
    ov.close_text.setString_("✕")
    ov.close_text.setFontSize_(S(13))
    ov.close_text.setForegroundColor_(text_color(0.50).CGColor())
    ov.close_text.setAlignmentMode_("center")
    ov.close_text.setContentsScale_(2.0)

    for l in (ov.name_bg, ov.name_text, ov.quit_card, ov.quit_text,
              ov.close_text, ov.bar):
        ov.layer().addSublayer_(l)


def pop_scale(layer, from_scale=None, duration=None):
    """弹窗出现：从中心缩放弹出（iOS 灵动岛那种弹法）。"""
    fs = POP_SCALE if from_scale is None else from_scale
    d = POP_DUR if duration is None else duration
    anim = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
    anim.setFromValue_(fs)
    anim.setToValue_(1.0)
    anim.setDuration_(d)
    anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_("easeOut"))
    layer.addAnimation_forKey_(anim, "popscale")


def ov_set_apps(ov, apps):
    for lay in ov.icon_layers:
        lay.removeFromSuperlayer()
    for dot in ov.dot_layers:
        dot.removeFromSuperlayer()
    ov.icon_layers = []
    ov.dot_layers = []
    ov.icon_cxs = []
    ov.apps = list(apps)
    ov.hover = -1
    ov.last_scales = None

    for i, a in enumerate(ov.apps):
        lay = Quartz.CALayer.alloc().init()
        lay.setBounds_(NSMakeRect(0, 0, ICON, ICON))
        lay.setAnchorPoint_((0.5, 0.5))
        lay.setCornerRadius_(ICON * 0.23)
        lay.setMasksToBounds_(True)
        lay.setContentsScale_(2.0)
        lay.setContentsGravity_("resizeAspect")
        # 高分辨率位图 + 三线性滤波 → 放大缩小都锐利
        lay.setMinificationFilter_("trilinear")
        lay.setMagnificationFilter_("trilinear")
        cg = icon_cgimage(a)
        if cg is not None:
            lay.setContents_(cg)

        anim = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        anim.setFromValue_(0.0)
        anim.setToValue_(1.0)
        anim.setDuration_(APP_FADE)
        anim.setBeginTime_(Quartz.CACurrentMediaTime() + i * 0.02)
        anim.setFillMode_("backwards")
        anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_("easeOut"))
        lay.addAnimation_forKey_(anim, "appear")
        lay.setOpacity_(1.0)

        dot = Quartz.CALayer.alloc().init()
        dot.setCornerRadius_(DOT_SIZE / 2)
        dot.setBackgroundColor_(
            NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).CGColor())

        ov.layer().addSublayer_(lay)
        ov.layer().addSublayer_(dot)
        ov.icon_layers.append(lay)
        ov.dot_layers.append(dot)
        # 坐标先占位：真正的值由 ov_relayout 按「图标组相对窗口居中」算好填入
        ov.icon_cxs.append(0.0)

    ov_relayout(ov)
    ov_update_dots(ov)


def ov_update_dots(ov):
    """图标下方小圆点：当前前台应用画青色，其余暗白。"""
    try:
        front_pid = NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()
    except Exception:
        front_pid = -1
    no_anim()
    for i, dot in enumerate(ov.dot_layers):
        try:
            is_front = ov.apps[i].processIdentifier() == front_pid
        except Exception:
            is_front = False
        if is_front:
            dot.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    *accent_rgba()).CGColor())
        else:
            dot.setBackgroundColor_(
                NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).CGColor())
    commit()


def ov_relayout(ov):
    """按窗口尺寸重排：图标在顶部胶囊内，弹出内容在胶囊下方。"""
    W = ov.bounds().size.width
    H = ov.bounds().size.height
    expanded = H > HIDDEN_H * 2
    # 收起态：只显示 iOS 式横杠
    no_anim()
    if not expanded:
        ov.bar.setBounds_(NSMakeRect(0, 0, HIDDEN_W, HIDDEN_H))
        ov.bar.setPosition_((W / 2, HIDDEN_H / 2))
        ov.bar.setOpacity_(1.0)
        for lay in ov.icon_layers + ov.dot_layers:
            lay.setOpacity_(0.0)
        ov.close_text.setOpacity_(0.0)
        ov.mask.setOpacity_(0.0)
        ov.gloss.setOpacity_(0.0)
        commit()
        return
    ov.bar.setOpacity_(0.0)
    # 展开态：黑色蒙版只铺「胶囊」那一段，且在窗口里居中。
    # 窗口比胶囊宽时（为了横向放下窗口卡片），多出来的部分是透明的。
    n_icons = len(ov.icon_layers)
    cap_w = min(W, capsule_w(n_icons))
    cap_x = (W - cap_w) / 2
    ov.mask.setBounds_(NSMakeRect(0, 0, cap_w, CAP_H))
    ov.mask.setPosition_((cap_x + cap_w / 2, H - CAP_H / 2))
    ov.mask.setCornerRadius_(CAP_H / 2)
    ov.mask.setOpacity_(MASK_ALPHA)
    # 顶部高光：只覆盖胶囊上半部（下半部渐变已透明，看不出直边）
    gh = CAP_H * 0.52
    ov.gloss.setBounds_(NSMakeRect(0, 0, cap_w, gh))
    ov.gloss.setPosition_((cap_x + cap_w / 2, H - gh / 2))
    ov.gloss.setOpacity_(0.9)
    for lay in ov.icon_layers:
        if lay.opacity() < 1.0:
            fade(lay, 1.0, APP_FADE, key="appear", ease="easeOut")
    for lay in ov.dot_layers:
        lay.setOpacity_(1.0)
    commit()
    popup_h = max(0, H - CAP_H)
    # 图标组相对**窗口**居中：这样窗口怎么变宽，图标在屏幕上的位置都不动。
    # （之前按「左内边距」固定排，窗口一变宽图标组就整体左移 → 闪烁）
    icon_total = n_icons * ICON + max(0, n_icons - 1) * GAP
    icon_x0 = (W - icon_total) / 2
    # 一个事务改完所有图标。原先循环里每次 begin/commit 一个 CATransaction，
    # 等于每改一个图标强制重绘一次（弹出的「卡一下」有它一份）。
    no_anim()
    for i, lay in enumerate(ov.icon_layers):
        cx = icon_x0 + i * (ICON + GAP) + ICON / 2
        ov.icon_cxs[i] = cx
        lay.setPosition_((cx, popup_h + ICON_BOTTOM + ICON / 2))
    commit()
    no_anim()
    ov.close_text.setBounds_(NSMakeRect(0, 0, CLOSE_W, S(18)))
    ov.close_text.setPosition_((cap_x + cap_w - PAD - CLOSE_W / 2, popup_h + CAP_H / 2))
    for i, dot in enumerate(ov.dot_layers):
        dot.setBounds_(NSMakeRect(0, 0, DOT_SIZE, DOT_SIZE))
        dot.setPosition_((ov.icon_cxs[i], popup_h + ICON_BOTTOM - DOT_SIZE / 2 - 2))
    commit()
    if ov.popup == "name" and ov.hover >= 0:
        ov_place_name(ov, ov.hover)
    elif ov.popup == "thumb":
        ov_place_windows(ov, ov.win_n)


def ov_place_name(ov, i):
    if not (0 <= i < len(ov.icon_cxs)):
        return
    text = ov.apps[i].localizedName() or "?"
    w = max(S(52), min(S(220), len(text) * S(14) + S(22)))
    W = ov.bounds().size.width
    cx = min(max(ov.icon_cxs[i], w / 2 + 4), max(w / 2 + 4, W - w / 2 - 4))
    y = POPUP_NAME_H / 2 + 1
    begin_anim(POP_DUR, "easeOut")
    ov.name_bg.setBounds_(NSMakeRect(0, 0, w, S(26)))
    ov.name_bg.setPosition_((cx, y))
    ov.name_text.setString_(text)
    ov.name_text.setBounds_(NSMakeRect(0, 0, w - S(12), S(17)))
    ov.name_text.setPosition_((cx, y))
    commit()


def ov_place_windows(ov, n):
    """把 n 张窗口卡片横排居中。

    幂等：卡片数和窗口宽度都没变 → 直接返回，不重排。
    （每次切换图标都重排一次会重启隐式动画，来回选图标时看着就是闪）
    """
    W = ov.bounds().size.width
    n = max(1, min(n, MAX_WINS))
    if getattr(ov, "last_win_layout", None) == (n, W):
        return
    ov.last_win_layout = (n, W)
    total = n * WIN_CARD_W + (n - 1) * WIN_GAP
    x0 = (W - total) / 2
    # 锚点在顶部中心，所以 position 要落在卡片**顶边**中心
    y = WIN_POPUP_H / 2 + WIN_CARD_H / 2
    begin_anim(POP_DUR, "easeOut")
    for i, wc in enumerate(ov.win_cards):
        if i >= n:
            continue          # 可见性由 ov_sync_cards 负责，这里只管位置
        cx = x0 + i * (WIN_CARD_W + WIN_GAP) + WIN_CARD_W / 2
        wc["card"].setBounds_(NSMakeRect(0, 0, WIN_CARD_W, WIN_CARD_H))
        wc["card"].setPosition_((cx, y))
        wc["img"].setBounds_(NSMakeRect(0, WIN_CARD_H - WIN_CARD_IMG_H - 3,
                                        WIN_CARD_W - 8, WIN_CARD_IMG_H))
        wc["img"].setPosition_((WIN_CARD_W / 2, WIN_CARD_H - WIN_CARD_IMG_H / 2 - 3))
        wc["label"].setBounds_(NSMakeRect(0, 0, WIN_CARD_W - S(14), S(14)))
        wc["label"].setPosition_((WIN_CARD_W / 2, S(15)))
    commit()


def capsule_w(n):
    """胶囊（可见的黑底）宽度：图标组 + 左右各留一份 CLOSE_W 的留白。

    左右对称是刻意的——只留右侧给 ✕ 的话，图标区会偏左，
    看起来就是「右边多出一块」。
    """
    return PAD * 2 + n * ICON + max(0, n - 1) * GAP + CLOSE_W * 2


def ov_window_needed_w(ov, n):
    """容纳 n 张卡片需要的窗口宽度。"""
    total = n * WIN_CARD_W + max(0, n - 1) * WIN_GAP
    return total + PAD * 2


def ov_place_quit(ov, i):
    """退出确认条的位置。"""
    if not (0 <= i < len(ov.icon_cxs)):
        return
    text = f"退出「{ov.apps[i].localizedName()}」"
    w = max(S(112), len(text) * S(13) + S(30))
    W = ov.bounds().size.width
    cx = min(max(ov.icon_cxs[i], w / 2 + 4), max(w / 2 + 4, W - w / 2 - 4))
    y = POPUP_NAME_H / 2 + 1
    begin_anim(POP_DUR, "easeOut")
    ov.quit_card.setBounds_(NSMakeRect(0, 0, w, S(26)))
    ov.quit_card.setPosition_((cx, y))
    ov.quit_text.setString_(text)
    ov.quit_text.setBounds_(NSMakeRect(0, 0, w - S(12), S(17)))
    ov.quit_text.setPosition_((cx, y))
    commit()


def ov_quit_hit(ov, p):
    """是否点在退出确认条上。"""
    if ov.popup != "quit":
        return False
    b = ov.quit_card.bounds()
    pos = ov.quit_card.position()
    return (abs(p.x - pos.x) <= b.size.width / 2
            and abs(p.y - pos.y) <= b.size.height / 2)


def ov_win_hit(ov, p):
    """点在哪个窗口卡片上（-1 表示没有）。"""
    if ov.popup != "thumb":
        return -1
    for i, wc in enumerate(ov.win_cards[:max(0, ov.win_n)]):
        pos = wc["card"].position()          # 锚点在顶部中心 → 卡片占 [pos.y-H, pos.y]
        if (abs(p.x - pos.x) <= WIN_CARD_W / 2
                and pos.y - WIN_CARD_H <= p.y <= pos.y):
            return i
    return -1


def pop_drag(layer, duration=None):
    """把卡片「从指针处拖拽开」——锚点在顶部中心，从一条窄线展开。

    为什么锚点在顶部中心：卡片长在胶囊下沿，锚点放顶部 = 从指针所在的
    那一侧往外长，观感像被拉出来（而不是凭空缩放）。

    为什么用 keyframe 而不是 CASpringAnimation / CATransform3D：
    CATransform3D 是 C 结构体，直接作为 CAAnimation 的 fromValue 会让
    Core Animation 在原生层崩溃（进程直接 SIGTRAP，Python 层看不到异常）。
    改成 keyPath "transform.scale"（标量）+ 多关键帧，同样有回弹，且绝对安全。
    """
    d = POP_DRAG_DUR if duration is None else duration
    try:
        a = Quartz.CAKeyframeAnimation.animationWithKeyPath_("transform.scale")
        # 0.12 → 微过冲 1.06 → 回落到 0.985 → 停在 1.0（拟物回弹）
        a.setValues_([POP_DRAG_SX, 1.06, 0.985, 1.0])
        a.setKeyTimes_([0.0, 0.58, 0.80, 1.0])
        a.setDuration_(d)
        a.setTimingFunction_(
            Quartz.CAMediaTimingFunction.functionWithName_("easeOut"))
        layer.addAnimation_forKey_(a, "popdrag")
    except Exception as e:
        print("[pop_drag] 降级:", e, flush=True)
        fade(layer, 1.0, POP_DUR, key="popdrag")


def ov_sync_cards(ov, n):
    """让前 n 张窗口卡片可见，其余隐藏。

    必须独立成一个函数：popup 一直是 "thumb" 时 ov_show_popup 会被短路，
    这时从「1 个窗口的应用」切到「3 个窗口的应用」，新增卡片 opacity 还是 0
    → 卡片"缺一块"，切回来又出现，看着就是闪。
    """
    for i, wc in enumerate(ov.win_cards):
        card = wc["card"]
        want = 1.0 if i < n else 0.0
        if want > 0 and card.opacity() != 1.0:
            fade(card, 1.0, POP_DRAG_DUR, key="pop")
            pop_drag(card)               # 拟物：从顶部向两侧+向下拖开
        elif want == 0 and card.opacity() != 0.0:
            fade(card, 0.0, NAME_FADE, key="pop")


def ov_show_popup(ov, kind):
    """弹出名字条 / 缩略图卡片 / 退出确认条。"""
    if ov.popup == kind:
        return
    ov.popup = kind
    simple = {
        "name": (ov.name_bg, ov.name_text),
        "quit": (ov.quit_card, ov.quit_text),
    }
    for l in (ov.name_bg, ov.name_text, ov.quit_card, ov.quit_text):
        target = 1.0 if l in simple.get(kind or "", ()) else 0.0
        if target > 0 and l.opacity() != 1.0:
            fade(l, 1.0, POP_DUR, key="pop")
            pop_scale(l, from_scale=0.9)
        elif target == 0 and l.opacity() != 0.0:
            fade(l, 0.0, NAME_FADE, key="pop")
    # 窗口卡片组
    ov_sync_cards(ov, ov.win_n if kind == "thumb" else 0)


def ov_set_windows(ov, items):
    """填充窗口卡片内容。items: [{"cg": cgimage|None, "title": str}]

    **只有内容真的变了才写图层**：每次切换图标都把 4 张卡片（含空槽）
    重写一遍，会触发无谓的重绘/解码，来回选图标时就是一片闪。
    """
    no_anim()
    for i, wc in enumerate(ov.win_cards):
        cg = items[i].get("cg") if i < len(items) else None
        title = (items[i].get("title") or "") if i < len(items) else ""
        if wc.get("cid") != id(cg):
            wc["cid"] = id(cg)
            wc["img"].setContents_(cg)
            if cg is not None:
                # 新图淡入：直接换 contents 会「啪」地跳一下（用户感觉是卡）
                a = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
                a.setFromValue_(0.0)
                a.setToValue_(1.0)
                a.setDuration_(0.22)
                a.setTimingFunction_(
                    Quartz.CAMediaTimingFunction.functionWithName_("easeOut"))
                wc["img"].addAnimation_forKey_(a, "thumbin")
                wc["img"].setOpacity_(1.0)
        if wc.get("clab") != title:
            wc["clab"] = title
            wc["label"].setString_(title)
    commit()
    ov.win_n = len(items)


def ov_update_hover(ov, mouse_x):
    """距离驱动的放大 + 名字条。mouse_x=None 表示鼠标不在窗口内。

    优化：算出目标缩放后先和上次比对，完全一样就不动图层（省 CPU）。
    """
    n = len(ov.icon_layers)
    if n == 0:
        return None
    if mouse_x is None:
        if ov.hover != -1:
            ov.hover = -1
            ov_show_popup(ov, None)
        if ov.last_scales != [1.0] * n:
            no_anim()
            for lay in ov.icon_layers:
                lay.setTransform_(Quartz.CATransform3DMakeScale(1.0, 1.0, 1.0))
            commit()
            ov.last_scales = [1.0] * n
        return None

    # 命中 = 离鼠标最近的图标（搜索半径覆盖到间隙中线）。
    # 若只认 ICON/2，鼠标在图标之间的 18pt 空隙里就是「无命中」，
    # 来回选图标时必然经过空隙 → 弹窗反复收/放 → 闪烁。
    hit = -1
    best = 1e9
    reach = (ICON + GAP) / 2
    for i, cx in enumerate(ov.icon_cxs):
        d = abs(mouse_x - cx)
        if d < best and d <= reach:
            best = d
            hit = i

    targets = []
    for i in range(n):
        d = abs(mouse_x - ov.icon_cxs[i])
        if i == hit:
            s = MAG_MAX
        elif d < MAG_DIST:
            s = 1.0 + (MAG_LINK - 1.0) * ((1.0 - d / MAG_DIST) ** 1.5)
        else:
            s = 1.0
        targets.append(s)

    same = (ov.last_scales is not None and len(ov.last_scales) == n
            and all(abs(a - b) < 0.008 for a, b in zip(ov.last_scales, targets)))
    if same:
        if hit != ov.hover:
            ov.hover = hit
            return hit
        return None

    ov.last_scales = targets
    no_anim()
    for lay, s in zip(ov.icon_layers, targets):
        lay.setTransform_(Quartz.CATransform3DMakeScale(s, s, 1.0))
    commit()

    if hit != ov.hover:
        ov.hover = hit
        return hit
    return None


def ov_set_close_hover(ov, on):
    if on == ov.close_hover:
        return
    ov.close_hover = on
    no_anim()
    ov.close_text.setForegroundColor_(
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.95 if on else 0.45).CGColor())
    commit()


def ov_hit_index(ov, p):
    """命中哪个图标（考虑放大后的命中范围）。"""
    H = ov.bounds().size.height
    popup_h = max(0, H - CAP_H)
    cy = popup_h + ICON_BOTTOM + ICON / 2
    if not (cy - ICON / 2 <= p.y <= cy + ICON / 2):
        return -1
    for i, cx in enumerate(ov.icon_cxs):
        if abs(p.x - cx) <= ICON / 2:
            return i
    return -1


def ov_close_hit(ov, p):
    H = ov.bounds().size.height
    # 收起态只有一根 11pt 高的横杠，没有 ✕ 按钮。
    # 不做这个判断的话，点横杠右半边会落进 ✕ 判定区（最右 PAD+CLOSE_W 宽），
    # 表现就是「点一下灵动岛直接退出」。
    if H < CAP_H + 8:
        return False
    return (p.x >= ov.bounds().size.width - PAD - CLOSE_W
            and H - CAP_H <= p.y <= H)


# ── 视图类（只保留 ObjC 回调） ────────────────────────────

class IslandOverlay(NSView):
    """透明覆盖层：图标、名字条、缩略图卡片都是 CALayer。"""

    def initWithFrame_(self, frame):
        self = objc.super(IslandOverlay, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        self.layer().setBackgroundColor_(NSColor.clearColor().CGColor())
        self.layer().setContentsScale_(2.0)
        ov_init_layers(self)
        return self

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        # 窗口卡片：点哪张切到哪个窗口
        wi = ov_win_hit(self, p)
        if wi >= 0:
            self.win_click = wi
            return
        # 退出确认条：点它才真的退出
        if ov_quit_hit(self, p):
            self.quit_confirm = True
            return
        if ov_close_hit(self, p):
            print("bye", flush=True)
            # 先落 STOP 标记：启动器靠它区分「用户主动关」和「崩溃」，
            # 否则会被自动重启逻辑拉起来（表现为「点 ✕ 关不掉」）。
            try:
                open(STOP_FILE, "w").close()
            except Exception:
                pass
            try:
                AppHelper.stopEventLoop()
                NSApplication.sharedApplication().terminate_(None)
            except Exception:
                pass
            os._exit(0)          # 兜底：确保进程真的退出
            return
        i = ov_hit_index(self, p)
        if i < 0:
            return
        a = self.apps[i]
        print(f"切换到 {a.localizedName()}", flush=True)
        activate_app(a)

    def rightMouseDown_(self, event):
        """右键：请求弹出「退出 XXX」确认条（系统菜单在这个窗口弹不出来）。"""
        p = self.convertPoint_fromView_(event.locationInWindow(), None)
        i = ov_hit_index(self, p)
        if i < 0:
            return
        self.quit_request = i


# ── 控制器 ────────────────────────────────────────────────

class Controller(NSObject):
    def initWithPanel_overlay_effect_(self, panel, overlay, effect):
        self = objc.super(Controller, self).init()
        self.panel = panel
        self.overlay = overlay
        self.effect = effect
        self.last_sig = None
        self.n = 0
        self.tick = 0
        self.expanded = False
        self.popup = None
        self.leave_at = None
        self.win_cache = {}        # pid -> (时间, 窗口列表, 卡片数据)
        self.win_objs = []         # 当前展示的窗口对象（供点击切换）
        self.shown_for = -1        # 卡片当前为哪个图标显示
        self.hover_app = -1        # 上次悬停的图标（判断是否切换，避免重复布局）
        self.last_layout_t = 0.0   # 上次卡片布局时间（节流用）
        self.last_mouse = None     # 上次鼠标位置（没动就跳过重算）
        self.suspended = False     # 切换器打开时临时挂起
        self.pend_i = -1           # 待加载的图标（等悬停停稳）
        self.pend_t = 0.0
        self.thumb_busy = False
        self.thumb_result = None
        return self

    # 应用列表刷新
    def refresh_(self, timer):
        if os.path.exists(STOP_FILE):
            AppHelper.stopEventLoop()
            NSApplication.sharedApplication().terminate_(None)
            return
        apps = running_apps()
        sig = tuple(a.bundleIdentifier() for a in apps)
        if sig != self.last_sig:
            self.last_sig = sig
            self.n = len(apps)
            ov_set_apps(self.overlay, apps)
            self.apply_geometry()

    # 鼠标轮询（33fps）
    def pollMouse_(self, timer):
        try:
            if getattr(self, "suspended", False):
                return          # 切换器打开期间不动灵动岛
            self.tick = getattr(self, "tick", 0) + 1
            self.drain_quit()
            if self.tick % 3 == 0:
                self.drain_thumb_result()
            if self.expanded and self.tick % 15 == 0:      # ≈0.45s 刷一次当前应用圆点
                ov_update_dots(self.overlay)
            p = NSEvent.mouseLocation()
            # 鼠标没动 → 跳过最重的 hover 计算（图标缩放遍历）
            moved = True
            if self.last_mouse is not None:
                if (abs(p.x - self.last_mouse[0]) < 0.6
                        and abs(p.y - self.last_mouse[1]) < 0.6):
                    moved = False
            self.last_mouse = (p.x, p.y)
            in_trigger = self.in_hotzone(p)
            in_panel = self.inside_panel(p)

            # 悬停停稳 DWELL 秒后才拉窗口数据。
            # 快速划过图标时不加载 → 不换缩略图、不重排 → 不闪也不卡。
            pi = self.pend_i
            if pi >= 0 and time.time() - self.pend_t >= HOVER_DWELL:
                self.pend_i = -1
                self.attach_windows(pi)

            if in_trigger or in_panel:
                self.expand()
                if in_panel and moved:
                    self.handle_hover(p)
            else:
                self.maybe_collapse()

            if not in_panel:
                ov_update_hover(self.overlay, None)
                ov_set_close_hover(self.overlay, False)
                self.hover_app = -1
                self.pend_i = -1
            else:
                f = self.panel.frame()
                lx = p.x - f.origin.x
                ly = p.y - f.origin.y
                ov_set_close_hover(self.overlay, ov_close_hit(self.overlay,
                                                              NSMakePoint(lx, ly)))
        except Exception as e:
            print("[poll] 异常:", e, flush=True)

    # ── 内部辅助（下划线前缀 = 不会被 PyObjC 当 selector） ──
    def in_hotzone(self, p):
        """触发热区：贴着灵动岛所在位置的一条边缘带（比面板本身大，好碰到）。

        位置可配（顶部居中/左右、侧边），所以热区必须跟着面板走，
        不能再写死「屏幕顶部中央」。
        """
        f = self.panel.frame()
        pos = CFG["island"].get("position", "top-center")
        if pos in ("left", "right"):
            x0, x1 = (f.origin.x - TRIGGER_H, f.origin.x + f.size.width + TRIGGER_H)
            pad = max(TRIGGER_W, f.size.height) / 2
            return (x0 <= p.x <= x1
                    and f.origin.y - pad <= p.y <= f.origin.y + f.size.height + pad)
        sf = (getattr(self, "screen", None) or main_screen()).frame()
        top_y = (sf.origin.y + sf.size.height
                 - NSStatusBar.systemStatusBar().thickness())
        cx = f.origin.x + f.size.width / 2
        half = max(TRIGGER_W, f.size.width) / 2
        return (p.y >= top_y - TRIGGER_H and abs(p.x - cx) <= half)

    def set_suspended(self, on):
        """切换器打开时临时收起（避免两个浮层打架、也免得抢鼠标）。"""
        self.suspended = bool(on)
        if on:
            self.expanded = False
            self.popup = None
            self.shown_for = -1
            self.hover_app = -1
            self.pend_i = -1
            self.overlay.last_win_layout = None
            ov_show_popup(self.overlay, None)
            self.apply_geometry()

    def inside_panel(self, p):
        f = self.panel.frame()
        return (f.origin.x <= p.x <= f.origin.x + f.size.width
                and f.origin.y <= p.y <= f.origin.y + f.size.height)

    def handle_hover(self, p):
        if self.popup == "quit":
            return              # 退出确认态期间不切换弹窗
        f = self.panel.frame()
        lx = p.x - f.origin.x
        changed = ov_update_hover(self.overlay, lx)
        if changed is None:
            return
        i = changed
        if i < 0:
            # 鼠标在图标间空隙 / 面板边缘留白：**保持当前弹窗不动**。
            # 之前这里 set_popup(None) → 卡片淡出，进下一个图标又淡入+缩放弹出，
            # 来回选择时就是一片闪。真正离开面板由 pollMouse 的
            # not in_panel 分支统一收起，不需要在这里抢着收。
            return
        # 只在「切到另一个图标」时才重置成名字态。
        # 若同一图标上每次轮询都重置，会把已弹出的窗口卡片打回名字条，
        # 造成每帧反复重排（实测的卡顿主因）。
        if i != self.hover_app:
            self.hover_app = i
            # 只有当前没显示卡片时才用名字条过渡。
            # 若已有卡片还每次重置成 name，会形成「name ↔ thumb」来回切换，
            # 导致每帧重排（实测日志里同一图标刷 30+ 次「展示卡片」）。
            if self.popup != "thumb":
                self.set_popup("name")
                ov_place_name(self.overlay, i)
            # 先排队，等鼠标停稳（见 pollMouse）再拉窗口数据
            self.pend_i = i
            self.pend_t = time.time()
            return
        if self.pend_i == i:
            return                      # 已排队，别再重复触发
        self.attach_windows(i)

    def attach_windows(self, i):
        """扫描该应用窗口 → 先出卡片（无图）→ 后台补缩略图。

        分工原因（都是实测踩出来的）：
          · AX 枚举必须在主线程且限时（后台线程里 AX 会卡死；超时太短又拿不到窗口）
          · CGWindowListCreateImage 对**最小化窗口**会静默挂起 → 只能丢后台线程，
            且最小化窗口直接跳过不截
        """
        if not (0 <= i < len(self.overlay.apps)):
            return
        if not has_ax_perm():
            return          # 没辅助功能权限 → 不做枚举（会卡 2 秒），只留名字条
        app = self.overlay.apps[i]
        try:
            pid = app.processIdentifier()
        except Exception:
            return
        now = time.time()
        cached = self.win_cache.get(pid)
        if cached and now - cached[0] < THUMB_TTL:
            self.show_windows(i, cached[1], cached[2])
            return
        try:
            wins = app_windows(app, screen_w=main_screen().frame().size.width)
        except Exception as e:
            print("[win] 枚举失败:", e, flush=True)
            wins = []
        items = [{"cg": None, "title": wd.get("title") or ""} for wd in wins]
        self.win_cache[pid] = (now, wins, items)
        self.show_windows(i, wins, items)
        if not wins:
            return
        # 后台补缩略图（最小化的窗口跳过，截它会挂起）
        if not self.thumb_busy:
            self.thumb_busy = True
            todo = list(wins)

            def work():
                out = []
                try:
                    with objc.autorelease_pool():
                        for wd in todo:
                            if wd.get("min"):
                                out.append(None)
                                continue
                            try:
                                r = window_thumb(wd)
                                out.append(r[0] if r else None)
                            except Exception:
                                out.append(None)
                except Exception:
                    pass
                self.thumb_result = (pid, out)

            threading.Thread(target=work, daemon=True).start()

    def show_windows(self, i, wins, items):
        """把窗口卡片呈现出来（只在鼠标仍在该图标上时弹）。

        幂等：同一应用且卡片已在显示 → 只更新图片，不重复布局。
        """
        self.win_objs = wins
        # 节流：同一图标 + 同样卡片数，0.8 秒内不重复布局。
        # 原因：展开动画期间鼠标会在面板边缘反复进出，hover 状态被反复重置，
        # 若不节流会每帧重排卡片（实测的卡顿主因）。
        # 切换图标时 shown_for 不同 → 立即布局，不影响手感。
        now = time.time()
        # 同一图标 + 同样窗口数 + 已经是卡片态 → 只更新内容，永远不重排。
        # （之前写成「超过 0.8 秒再排一次」，结果每 0.8 秒闪一下。）
        same = (self.shown_for == i and self.overlay.win_n == len(items)
                and self.popup == "thumb")
        if same:
            return                      # 状态完全一致 → 一个字都不动
        ov_set_windows(self.overlay, items)
        if self.overlay.hover == i:
            if items:
                self.shown_for = i
                self.last_layout_t = now
                if self.popup != "thumb":
                    self.set_popup("thumb")          # 首次弹出（带缩放动画）
                else:
                    ov_sync_cards(self.overlay, len(items))   # 换应用，同步可见性
                ov_place_windows(self.overlay, len(items))
                print(f"[win] 展示 {len(items)} 张卡片", flush=True)
            else:
                # 该应用没有窗口 → 回落到名字条（否则会留一张空卡片）
                self.shown_for = i
                self.set_popup("name")
                ov_place_name(self.overlay, i)

    def drain_thumb_result(self):
        """主循环收后台截图结果，填进卡片。"""
        res = self.thumb_result
        if res is None:
            return
        self.thumb_result = None
        self.thumb_busy = False
        pid, cgs = res
        cached = self.win_cache.get(pid)
        if not cached:
            return
        _, wins, items = cached
        for k, cg in enumerate(cgs):
            if k < len(items) and cg is not None:
                items[k]["cg"] = cg
        self.win_cache[pid] = (time.time(), wins, items)
        # 如果当前正停在这个应用的图标上，直接刷新卡片
        cur = self.overlay.hover
        if 0 <= cur < len(self.overlay.apps):
            try:
                if self.overlay.apps[cur].processIdentifier() == pid:
                    ov_set_windows(self.overlay, items)
            except Exception:
                pass

    def drain_quit(self):
        """处理窗口卡片点击、右键请求、退出确认。"""
        ov = self.overlay
        # 窗口卡片点击 → 精确切到那个窗口
        wi = getattr(ov, "win_click", -1)
        if wi >= 0:
            ov.win_click = -1
            i = ov.hover
            if 0 <= wi < len(self.win_objs) and 0 <= i < len(ov.apps):
                wd = self.win_objs[wi]
                title = (wd.get("title") or "")[:26]
                print(f"切到窗口：{title}", flush=True)
                raise_window(ov.apps[i], wd)
            self.set_popup(None)
        req = getattr(ov, "quit_request", None)
        if req is not None:
            ov.quit_request = None
            if 0 <= req < len(ov.apps):
                self.quit_index = req
                ov_place_quit(ov, req)
                self.set_popup("quit")
        if getattr(ov, "quit_confirm", False):
            ov.quit_confirm = False
            i = getattr(self, "quit_index", -1)
            if 0 <= i < len(ov.apps):
                app = ov.apps[i]
                print(f"退出 {app.localizedName()}", flush=True)
                try:
                    app.terminate()
                except Exception:
                    try:
                        app.forceTerminate()
                    except Exception:
                        pass
            self.set_popup(None)

    def set_popup(self, kind):
        if self.popup == kind:
            return
        if kind != "thumb":
            self.shown_for = -1
        self.overlay.last_win_layout = None      # 弹窗变了 → 卡片需要重排
        self.popup = kind
        ov_show_popup(self.overlay, kind)
        self.apply_geometry()

    def expand(self):
        self.leave_at = None
        if not self.expanded:
            self.expanded = True
            self.apply_geometry()

    def maybe_collapse(self):
        if not self.expanded:
            return
        now = time.time()
        if self.leave_at is None:
            self.leave_at = now
            return
        if now - self.leave_at >= COLLAPSE_DELAY:
            self.leave_at = None
            self.expanded = False
            self.popup = None
            self.shown_for = -1
            self.hover_app = -1
            self.overlay.last_win_layout = None
            for lay in self.overlay.icon_layers:
                scale_layer(lay, 1.0)
            ov_show_popup(self.overlay, None)
            self.apply_geometry()

    def apply_geometry(self):
        """按展开状态 + 弹出类型调整窗口尺寸（顶边固定，向下生长）。"""
        if not self.expanded:
            w, h = HIDDEN_W, HIDDEN_H
            cap_w = HIDDEN_W
        else:
            popup_h = {"name": POPUP_NAME_H, "thumb": WIN_POPUP_H,
                       "quit": POPUP_NAME_H}.get(self.popup, 0)
            # 窗口宽度**恒定**：永远预留「放得下 MAX_WINS 张卡片」的宽度。
            # 为什么不按弹窗类型动态变宽 —— 宽度一变，窗口重新居中，
            # 图标组和卡片跟着整体跳动，来回选图标时就是闪烁。
            # 窗口本身是透明的，多出来的宽度看不见（只有居中的胶囊/卡片可见）。
            cap_w = capsule_w(self.n)
            w = max(cap_w, ov_window_needed_w(self.overlay, MAX_WINS))
            h = CAP_H + popup_h
        panel_place(self.panel, self.overlay, self.effect, w, h,
                    self.expanded, cap_w, getattr(self, "screen", None))


def panel_place(panel, overlay, effect, w, h, expanded, cap_w=None, screen=None):
    """窗口顶边固定在菜单栏下方，向下延伸 h；毛玻璃只覆盖居中的胶囊段。

    screen：要贴哪块屏（多屏时用）。不传就退回主屏。
    """
    sf = (screen or main_screen()).frame()
    menu_h = NSStatusBar.systemStatusBar().thickness()
    margin = S(24)
    pos = CFG["island"].get("position", "top-center")
    if pos in ("left", "right"):
        # 侧边：贴屏幕左/右缘、垂直居中
        if pos == "left":
            x = sf.origin.x + S(10)
        else:
            x = sf.origin.x + sf.size.width - w - S(10)
        y = sf.origin.y + (sf.size.height - h) / 2
    else:
        if pos == "top-left":
            x = sf.origin.x + margin
        elif pos == "top-right":
            x = sf.origin.x + sf.size.width - w - margin
        else:
            x = sf.origin.x + (sf.size.width - w) / 2
        top = sf.origin.y + sf.size.height - menu_h - TOP_GAP
        y = top - h

    # 窗口本身瞬时改（窗口透明，看不见尺寸跳变）
    no_anim()
    # display=False：尺寸变化交给系统下一帧重绘。
    # display=True 会**同步**重绘整个窗口，弹卡片时窗口从 104 高变成 385 高，
    # 那一下同步重绘就是肉眼看到的「卡」。
    panel.setFrame_display_(NSMakeRect(x, y, w, h), False)
    overlay.setFrame_(NSMakeRect(0, 0, w, h))
    commit()

    # 毛玻璃：收起态高度归零（只留横杠），展开态完整显示。
    # 注意 setFrame 必须直接调，不能用 animator()——PyObjC 里 animator 不生效，
    # 会导致 effect 卡在初始尺寸上（表现就是「毛玻璃没生效」）。
    cw = min(w, cap_w) if cap_w else w
    cx0 = (w - cw) / 2
    no_anim()
    if expanded:
        effect.setFrame_(NSMakeRect(cx0, max(0, h - CAP_H), cw, CAP_H))
        effect.layer().setCornerRadius_(CAP_H / 2)
    else:
        effect.setFrame_(NSMakeRect(cx0, 0, cw, 0))
    commit()

    ov_relayout(overlay)


def make_controller(screen=None):
    """在指定屏上建一个灵动岛实例（原来的 build() 主体抽出来）。

    screen=None 表示主屏。定时器挂在 ctrl.timers 上，是为了多屏重建时
    能干净地 invalidate —— 不然插拔显示器几次就会攒下一堆跑着的定时器。
    """
    w0, h0 = HIDDEN_W, HIDDEN_H
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(0, 0, w0, h0),
        NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, False)
    panel.setLevel_(NSStatusWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(False)
    panel.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, w0, h0))

    effect = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, w0, HIDDEN_H))
    effect.setMaterial_(NSVisualEffectMaterialHUDWindow)
    effect.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    effect.setState_(NSVisualEffectStateActive)
    effect.setWantsLayer_(True)
    effect.layer().setCornerRadius_(HIDDEN_H / 2)
    effect.layer().setMasksToBounds_(True)
    effect.layer().setBorderWidth_(1.0)
    effect.layer().setBorderColor_(
        NSColor.colorWithCalibratedWhite_alpha_(1.0, 0.18).CGColor())
    content.addSubview_(effect)

    overlay = IslandOverlay.alloc().initWithFrame_(NSMakeRect(0, 0, w0, h0))
    content.addSubview_(overlay)

    panel.setContentView_(content)
    panel.orderFrontRegardless()

    ctrl = Controller.alloc().initWithPanel_overlay_effect_(panel, overlay, effect)
    ctrl.screen = screen or main_screen()
    ctrl.refresh_(None)

    t1 = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        1.5, ctrl, "refresh:", None, True)
    t2 = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        0.04, ctrl, "pollMouse:", None, True)
    ctrl.timers = [t1, t2]
    return ctrl


class IslandGroup(object):
    """一组灵动岛实例（monitor=all 时每屏一个）。

    刻意用普通 Python 类而不是 NSObject 子类：不继承就不会把方法导出成
    selector，也就不会撞系统私有方法（本项目踩过好几次 BadPrototypeError）。
    对外只暴露和单个 Controller 一样的接口（set_suspended），
    这样 main.py 不用关心到底有几个实例。
    """

    def __init__(self, monitor=None):
        self.monitor = monitor or CFG["island"].get("monitor", "main")
        self.ctrls = []
        self.sig = screen_signature()
        self.rebuild()

    def set_suspended(self, on):
        for c in self.ctrls:
            try:
                c.set_suspended(on)
            except Exception:
                pass

    def rebuild(self):
        self.close()
        self.ctrls = [make_controller(s) for s in target_screens(self.monitor)]
        self.sig = screen_signature()

    def close(self):
        for c in self.ctrls:
            for t in (getattr(c, "timers", None) or []):
                try:
                    t.invalidate()
                except Exception:
                    pass
            try:
                c.panel.orderOut_(None)
                c.panel.close()
            except Exception:
                pass
        self.ctrls = []

    def sync_screens(self):
        """让实例数量和屏幕配置保持一致。

        插拔外接屏、改分辨率都会变；monitor=mouse 还要把岛挪到鼠标所在屏。
        由 main 的主循环低频调用（≈2 秒一次），不用单独起定时器。
        """
        try:
            new_sig = screen_signature()
            if self.monitor == "mouse":
                # 鼠标模式永远只有一个实例，屏变了只需要挪过去，不必重建
                if not self.ctrls:
                    self.rebuild()
                    return
                cur = mouse_screen()
                for c in self.ctrls:
                    if c.screen is not cur:
                        c.screen = cur
                        try:
                            c.apply_geometry()
                        except Exception:
                            pass
                self.sig = new_sig
                return
            if new_sig != self.sig:
                self.rebuild()
        except Exception as e:
            print("[island] 同步屏幕失败:", e, flush=True)


def build():
    """创建灵动岛，返回 IslandGroup（**不**启动事件循环）。

    **不**启动事件循环——这样它能和窗口切换器共用一个进程（合并版的关键）。
    """
    return IslandGroup()


def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    ctrl = build()

    signal.signal(signal.SIGINT, lambda *a: app.terminate_(None))

    # 缺权限 → 主动申请（已授权则静默跳过，不会打扰）
    request_permissions()

    print(f"灵动岛已启动（{THEME} 主题 / {CFG['island'].get('position')}）。", flush=True)
    print("停止：点 ✕ / Ctrl+C / 建 STOP 文件", flush=True)
    if has_ax_perm():
        print("辅助功能权限：已授权 → 悬停图标可显示窗口卡片", flush=True)
    else:
        print("辅助功能权限：未授权 → 悬停只显示应用名（窗口卡片需要它）", flush=True)
    if has_screen_perm():
        print("屏幕录制权限：已授权 → 悬停图标可显示窗口缩略图", flush=True)
    else:
        print("屏幕录制权限：未授权 → 只显示应用名", flush=True)
        print("  已为你弹出授权请求，请到 系统设置 → 隐私与安全性 → 屏幕录制", flush=True)
        print("  勾选「灵动岛」后重启本程序（关闭再双击 /Applications/灵动岛.app）", flush=True)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
