# -*- coding: utf-8 -*-
"""共享核心：权限、屏幕、应用图标、窗口枚举、缩略图、窗口提升。

灵动岛（island.py）和窗口切换器（switcher.py）都用这里的能力。
这里的每一条实现都是踩过坑验证过的，注释里标了原因，改之前先读注释。
"""
import re
import time

import Quartz
from AppKit import (
    NSApplicationActivationPolicyRegular, NSBitmapImageRep, NSImage,
    NSMakeRect, NSScreen, NSSize, NSWorkspace,
)
from ApplicationServices import (
    AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
    AXUIElementPerformAction, AXUIElementSetAttributeValue,
    AXUIElementSetMessagingTimeout,
)

# 我们自己的进程不能出现在列表里（否则会切到自己）
SKIP_BUNDLES = {
    "com.apple.WindowManager", "com.apple.systemuiserver",
    "com.tencent.workbuddy.mac.island",
}

_PT_RE = re.compile(r"x:([-\d.]+)\s*y:([-\d.]+)")
_SZ_RE = re.compile(r"w:([-\d.]+)\s*h:([-\d.]+)")


# ══════════ 权限 ══════════
# 两个权限都必须先问清楚再用对应 API，否则会静默挂起（不是报错）。

_SCREEN_PERM = None
_AX_PERM = None


def has_ax_perm():
    """辅助功能：枚举/切换窗口需要。

    **没有权限时 AX 调用会一直等目标应用响应**（实测每次 2 秒超时），
    所以必须跳过，不能硬调。
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
    """屏幕录制：截窗口缩略图需要。

    没权限时 `CGWindowListCreateImage` **不报错，而是静默挂起进程**。
    """
    global _SCREEN_PERM
    if _SCREEN_PERM is None:
        try:
            _SCREEN_PERM = bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            _SCREEN_PERM = False
    return _SCREEN_PERM


def request_permissions():
    """缺权限时主动申请（这样程序才会出现在系统设置列表里，用户才勾得到）。"""
    global _SCREEN_PERM, _AX_PERM
    try:
        if not Quartz.CGPreflightScreenCaptureAccess():
            Quartz.CGRequestScreenCaptureAccess()
    except Exception:
        pass
    try:
        import ApplicationServices as AS
        if not AS.AXIsProcessTrusted():
            AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    except Exception:
        pass
    _SCREEN_PERM = None
    _AX_PERM = None


def refresh_perm_cache():
    global _SCREEN_PERM, _AX_PERM
    _SCREEN_PERM = None
    _AX_PERM = None


# ══════════ 屏幕 ══════════

def screens():
    return list(NSScreen.screens())


def main_screen():
    """原点处的屏（= 主显示器）。

    注意：不能用 NSScreen.mainScreen()——那是「当前有键盘焦点的屏」，
    焦点跑到副屏时窗口会跟着乱跑。
    """
    for s in NSScreen.screens():
        f = s.frame()
        if f.origin.x == 0 and f.origin.y == 0:
            return s
    return NSScreen.mainScreen()


def screen_of_point(x, y):
    """某个屏幕坐标落在哪块屏上。"""
    for s in NSScreen.screens():
        f = s.frame()
        if (f.origin.x <= x <= f.origin.x + f.size.width
                and f.origin.y <= y <= f.origin.y + f.size.height):
            return s
    return main_screen()


# ══════════ 图像 ══════════

def _draw_scaled(img, pw, ph):
    """把 NSImage 高质量重绘到目标像素尺寸后取 CGImage。

    直接把 1024 原图交给图层缩到几十 pt 会被快速滤波糊掉，
    预先按目标像素重绘才清晰。
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
        2, 1.0)
    target.unlockFocus()
    rep = NSBitmapImageRep.imageRepWithData_(target.TIFFRepresentation())
    if rep is None:
        return None
    rep.setSize_(NSSize(pw, ph))
    return rep.CGImage()


def icon_cgimage(app, px=96):
    try:
        return _draw_scaled(app.icon(), px, px)
    except Exception:
        return None


# ══════════ 应用 ══════════

def running_apps():
    """当前打开的常规 GUI 应用（有 Dock 图标那一类）。"""
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


def frontmost_pid():
    try:
        return NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()
    except Exception:
        return -1


def activate_app(app):
    """把应用切到前台。

    PyObjC 的 `activateWithOptions_` 在新 macOS 上不生效，
    这里三管齐下（unhide + activate + open -a），哪条通就哪条起作用。
    """
    try:
        url = app.bundleURL()
    except Exception:
        url = None
    try:
        app.unhide()
        app.activateWithOptions_(3)
    except Exception:
        pass
    try:
        import subprocess
        subprocess.Popen(["open", "-a", url.path() if url is not None
                          else (app.localizedName() or "")])
    except Exception:
        pass


# ══════════ 窗口 ══════════

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


def _cg_id_map(pid):
    """CGWindowList 侧该 pid 的 layer-0 窗口 id + 位置（按当前 z 顺序）。"""
    out = []
    try:
        for i, w in enumerate(Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID)):
            if w.get("kCGWindowOwnerPID") != pid:
                continue
            if int(w.get("kCGWindowLayer", 0)) != 0:
                continue
            b = w.get("kCGWindowBounds", {})
            out.append({
                "id": w.get("kCGWindowNumber"), "order": i,
                "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
            })
    except Exception:
        pass
    return out


def app_windows(app, max_n=12, screen_w=None):
    """某个应用的真实窗口列表（按面积从大到小）。

    为什么以 AX 为准：CGWindowList 里混着大量「幽灵窗口」（多个 app 都有
    同位置同尺寸的不可见窗口，且没有 AX 对应物），拿它当列表会切到空窗口。
    CG 只用来按位置匹配拿 id（截图时用）。
    """
    if not has_ax_perm():
        return []
    try:
        pid = app.processIdentifier()
        elem = AXUIElementCreateApplication(pid)
        # AX 调用默认会一直等目标应用响应（后台线程里会卡死），
        # 设 2 秒超时：太快拿不到窗口，太慢会拖住主循环。
        try:
            AXUIElementSetMessagingTimeout(elem, 2.0)
        except Exception:
            pass
        wins = _ax_attr(elem, "AXWindows")
        if not wins:
            return []
        cgs = _cg_id_map(pid)
        if screen_w is None:
            screen_w = main_screen().frame().size.width

        out = []
        for w in list(wins):
            p, s = _ax_pos(w), _ax_size(w)
            if not p or not s:
                continue
            if s[0] < 200 or s[1] < 140:
                continue
            title = _ax_title(w)
            if not title and s[0] >= screen_w * 0.9:
                continue        # 桌面窗口，不是可切换的窗口
            cgid, cgorder = None, 10 ** 6
            for c in cgs:
                if abs(c["x"] - p[0]) <= 4 and abs(c["y"] - p[1]) <= 4:
                    cgid, cgorder = c["id"], c["order"]
                    break
            out.append({
                "app": app, "pid": pid,
                "app_name": app.localizedName() or "?",
                "title": title, "pos": p, "size": s,
                "cgid": cgid, "cgorder": cgorder,
                "min": _ax_minimized(w), "ax": w,
            })
        out.sort(key=lambda x: -(x["size"][0] * x["size"][1]))
        return out[:max_n]
    except Exception:
        return []


def list_windows(scope="all-windows", sort="recent"):
    """切换器用的窗口列表（跨应用）。

    scope:
      all-windows     所有常规应用的窗口
      same-app        只当前前台应用的窗口
      current-screen  只当前鼠标所在屏的窗口
    sort:
      recent   前台应用排最前，其余按应用顺序（近似「最近使用」）
      alpha    按应用名 + 标题
      zorder   按窗口的层叠顺序（最前的排第一）
    """
    fpid = frontmost_pid()
    apps = running_apps()
    if scope == "same-app":
        apps = [a for a in apps if a.processIdentifier() == fpid]

    items = []
    for a in apps:
        wins = app_windows(a)
        if not wins:
            continue
        for w in wins:
            items.append(w)

    if scope == "current-screen":
        c = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        sc = screen_of_point(c.x, c.y)
        sf = sc.frame()
        keep = []
        for w in items:
            cx = w["pos"][0] + w["size"][0] / 2
            cy = w["pos"][1] + w["size"][1] / 2
            if (sf.origin.x <= cx <= sf.origin.x + sf.size.width
                    and sf.origin.y <= cy <= sf.origin.y + sf.size.height):
                keep.append(w)
        items = keep

    if sort == "alpha":
        items.sort(key=lambda x: (x["app_name"].lower(), x["title"].lower()))
    elif sort == "zorder":
        items.sort(key=lambda x: x.get("cgorder", 10 ** 6))
    else:   # recent：前台应用优先，其余保持 running_apps 顺序
        items.sort(key=lambda x: (0 if x["pid"] == fpid else 1,
                                  x.get("cgorder", 10 ** 6)))
    return items


def window_thumb(wd, max_w=400, max_h=260):
    """截窗口缩略图 → (CGImage, 宽高比) 或 None。

    注意两个已验证的事实：
      1. 最小化的窗口截图会让进程**静默挂起** → 调用方必须先过滤 min
      2. CGWindowList 与 AXWindows 的坐标体系对不上 → 优先用 CGWindowID 截，
         退化时按 AX 给的位置截屏幕区域
    """
    if not has_screen_perm():
        return None
    try:
        cg = None
        cgid, pos, size = wd.get("cgid"), wd.get("pos"), wd.get("size")
        if cgid:
            cg = Quartz.CGWindowListCreateImage(
                Quartz.CGRectNull, Quartz.kCGWindowListOptionIncludingWindow,
                cgid, Quartz.kCGWindowImageBoundsIgnoreFraming)
        if cg is None and pos and size and size[0] >= 60 and size[1] >= 60:
            x, y, w, h = float(pos[0]), float(pos[1]), float(size[0]), float(size[1])
            if -8000 < x < 20000 and -8000 < y < 20000:
                cg = Quartz.CGWindowListCreateImage(
                    Quartz.CGRectMake(x, y, w, h),
                    Quartz.kCGWindowListOptionOnScreenOnly,
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
        pw = int(max_w)
        ph = int(pw * ratio)
        if ph > max_h:
            ph = int(max_h)
            pw = max(1, int(ph / ratio))
        return (_draw_scaled(nsimg, pw, ph), ratio)
    except Exception:
        return None


def raise_window(wd):
    """把某个窗口精确提到最前（含从最小化恢复）。"""
    app = wd.get("app")
    try:
        try:
            elem0 = AXUIElementCreateApplication(app.processIdentifier())
            AXUIElementSetMessagingTimeout(elem0, 0.8)
        except Exception:
            pass
        if wd.get("min"):
            try:
                AXUIElementSetAttributeValue(wd["ax"], "AXMinimized", False)
            except Exception:
                pass
        activate_app(app)
        time.sleep(0.06)
        AXUIElementPerformAction(wd["ax"], "AXRaise")
        try:
            elem = AXUIElementCreateApplication(app.processIdentifier())
            AXUIElementSetAttributeValue(elem, "AXFocusedWindow", wd["ax"])
        except Exception:
            pass
        return True
    except Exception as e:
        print("[core] 提升窗口失败:", e, flush=True)
        activate_app(app)
        return False
