# -*- coding: utf-8 -*-
"""配置读写（JSON）。

配置放在 ~/.config/liquid-island/config.json，缺失的键会自动用默认值补齐，
所以以后加新配置项不会让老配置文件失效。
"""
import copy
import json
import os

CONFIG_DIR = os.path.expanduser("~/.config/liquid-island")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# ── 默认配置 ──────────────────────────────────────────────
# 说明：所有可调项都在这儿，设置面板改的就是这些键。
DEFAULTS = {
    "island": {
        "enabled": True,
        # 形态：bar（iOS 横杠）| pill（小胶囊）| dot（小圆点）| invisible（完全隐藏，靠热区唤出）
        "form": "bar",
        # 位置：top-center | top-left | top-right | left | right
        "position": "top-center",
        # 显示在哪块屏：main（原点屏）| all（所有屏）
        "monitor": "main",
        # 展开方式：hover（鼠标滑过）| click（点击）| hotkey（快捷键）
        "expand_on": "hover",
        # 触发热区（贴屏幕顶部/边缘）
        "hotzone": {"width": 460, "height": 16},
        # 鼠标离开后多少秒收起
        "auto_collapse": 0.55,
        # 主题：glass（玻璃拟态）| dark（iOS 纯黑）| light（浅色玻璃）| auto（跟随系统）
        "theme": "glass",
        # 强调色（当前应用指示点、选中高亮）
        "accent": "#4DC1FF",
        # 整体缩放（1.0 = 原始尺寸）
        "scale": 1.8,
        "icons": {
            "size": 38,          # 图标基准边长（乘 scale）
            "gap": 10,           # 图标间距
            "pad": 16,           # 内边距
            "magnify": 1.45,     # 悬停放大倍率
            "magnify_dist": 72,  # 放大影响半径
        },
        "show_names": True,      # 悬停显示应用名
        "show_active_dot": True, # 当前应用指示点
        # 点图标后的行为：switch（切到该应用）| switcher（打开窗口切换器选窗口）
        "click_action": "switch",
    },
    "windows": {
        "enabled": True,         # 悬停显示窗口卡片
        "max_cards": 4,          # 最多几张
        "thumbs": True,          # 是否截缩略图（关掉更快、更省电）
        "dwell": 0.13,           # 悬停停稳多久才加载
        "cache_ttl": 30.0,       # 窗口列表缓存秒数
        "click_to_raise": True,  # 点卡片精确切到该窗口
    },
    "switcher": {
        "enabled": True,
        # 按住哪个键开始（option | cmd | ctrl），配合循环键
        "hold_key": "option",
        "cycle_key": "tab",
        "reverse_with_shift": True,
        # 范围：all-windows（所有窗口）| same-app（仅当前应用窗口）| current-screen（仅当前屏）
        "scope": "all-windows",
        # 排序：recent（最近使用）| alpha（字母）| zorder（前后层叠顺序）
        "sort": "recent",
        # 展示：thumbnails（缩略图网格）| icons（纯图标条）| list（列表）
        "style": "thumbnails",
        "columns": 4,
        "thumb_size": 300,       # 缩略图宽度（像素）
        "show_title": True,
        "show_icons": True,
        "theme": "glass",
        "background_dim": 0.18,  # 切换器背后压暗程度
        "corner_radius": 22,
        "gap": 18,
    },
    "general": {
        "launch_at_login": False,
        "show_menubar_icon": True,
        "hide_island_while_switching": True,   # 开切换器时临时收起灵动岛
        # 首次运行引导是否看过。看过之后只有手动点「权限与引导…」才会再弹
        "onboarded": False,
        # 「稍后再说」的静默截止时间戳（Unix 秒），过了才重新提醒
        "onboard_snooze_until": 0,
    },
}


def _merge(base, override):
    """递归合并：override 里有的用 override，其余保留 base（默认值）。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load():
    """读配置；文件不存在或损坏就用默认值（并落盘一份，方便用户手改）。"""
    data = {}
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception as e:
        print(f"[config] 读取失败，改用默认值: {e}", flush=True)
        data = {}
    cfg = _merge(DEFAULTS, data)
    if not os.path.exists(CONFIG_PATH):
        try:
            save(cfg)
        except Exception:
            pass
    return cfg


def save(cfg):
    """原子写：先写临时文件再替换，避免写一半被中断导致配置损坏。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def reset():
    """恢复默认。"""
    save(copy.deepcopy(DEFAULTS))
    return copy.deepcopy(DEFAULTS)
