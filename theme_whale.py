#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""theme_whale —— 「蓝色大肥鱼」设计令牌（浅海 / 深海双主题）

来源（不是拍脑袋定的）
--------------------
① 配色表逐条移植自 DViridescent/dafy-whale-theme 的 ctx.theme.overrideTokens("dafy")：
     brand-primary / bg-base / bg-layer-1 / bg-layer-2 / bg-overlay
     border-l1 / border-l2 / label-primary / label-secondary
     state-success / state-warn / state-error + sidebar-fill
   浓度 30% / 26% / 14% 的三色海面光晕（#3B62F6 / #57C7E8 / #2FD0B5）也照抄。
② 圆角、动效时长、投影强度同源：hover .18s、pop .22s、吉祥物浮动 3.6s、
   浮起阴影 0 8px 24px rgba(20,60,140,.2)。
③ 间距阶梯与字号阶梯按 UI UX Pro Max（nextlevelbuilder）重排：
   4/8 节奏、正文不小于 12px、文字对比度 ≥ 4.5:1、非文字 ≥ 3:1。

为什么单独一层
--------------
旧 ui_kit.py 把 30 多个十六进制色值写死在控件里，导致：改一个色要改五个控件、
加不了暗色、还没法验对比度。这里把「原语 → 语义 → 组件」固化下来，
控件只认语义名（brand / label1 / line2 ...），换主题只换一张表。

用法
----
    from theme_whale import palette, SP, FS, RAD, MOTION, SHADOW
    t = palette("light")        # 或 palette("dark")
    t.brand, t.label2, t.overlay, t.on_brand ...

    py theme_whale.py --check   # 打印对比度自检表（不达标会列出）

自检结论（--check 已跑通）
------------------------
旧色板的次要文字 #8A8F98 on #F4F5F7 = 2.96:1、#6B7280 on #F4F5F7 = 4.40:1，
都低于 WCAG AA 正文要求的 4.5:1 —— 这就是界面「发灰、看不清」的真实原因。
新色板 label2 = 5.53:1（浅）/ 7.39:1（深）。
"""

from __future__ import annotations

import sys
from functools import lru_cache

# ── 颜色工具 ────────────────────────────────────────────────────────────────
def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _hex(r: float, g: float, b: float) -> str:
    return "#%02X%02X%02X" % (
        max(0, min(255, round(r))),
        max(0, min(255, round(g))),
        max(0, min(255, round(b))),
    )


def mix(a: str, b: str, t: float) -> str:
    """把 a 向 b 混合 t（0=a，1=b）。"""
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(h: str) -> float:
    r, g, b = (v / 255.0 for v in _rgb(h))
    return 0.2126 * _lin(r * 255) + 0.7152 * _lin(g * 255) + 0.0722 * _lin(b * 255)


def contrast(a: str, b: str) -> float:
    """WCAG 对比度（1.0 ~ 21.0）。正文要 ≥4.5，大字/非文字要 ≥3.0。"""
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ── 原语：两套海（亮/暗） ─────────────────────────────────────────────────────
# 数值全部来自 dafy-whale-theme，未改动；on_brand 是按对比度算出来的。
_RAW = {
    "light": {
        "brand":     "#3B62F6",
        "bg_base":   "#E9F3FC",
        "layer1":    "#F3F9FE",
        "layer2":    "#E0EDF9",
        "overlay":   "#FFFFFF",
        "line1":     "#C6DCF2",
        "line2":     "#9FC3E8",
        "label1":    "#0E2A5C",
        "label2":    "#45678F",
        "success":   "#1F9D72",
        "warn":      "#B97A1E",
        "error":     "#D6456D",
        "sidebar":   "#DCE9F8",
        "on_brand":  "#FFFFFF",
        "shadow":    "#0F3C7A",
    },
    "dark": {
        "brand":     "#6C8CFF",
        "bg_base":   "#0A1428",
        "layer1":    "#0F1E38",
        "layer2":    "#0B1830",
        "overlay":   "#13233F",
        "line1":     "#1D3252",
        "line2":     "#2B4A78",
        "label1":    "#DCEBFF",
        "label2":    "#8FB0DC",
        "success":   "#4BC49B",
        "warn":      "#E8B45A",
        "error":     "#FF7A93",
        "sidebar":   "#0C1830",
        # 深海下品牌色变亮，白字只有 3.07:1（只够大字），所以压深色字 = 6.0:1
        "on_brand":  "#0A1428",
        "shadow":    "#000000",
    },
}

# 海面光晕三色（同源 dafy 的 .dafy-wash）
GLOW = ("#3B62F6", "#57C7E8", "#2FD0B5")


class Palette:
    """一套主题。属性名即语义名，另有若干派生色（*_hover / *_bg / *_line）。"""

    def __init__(self, name: str) -> None:
        raw = dict(_RAW[name])
        self.name = name
        self._raw = raw
        towards = "#000000" if name == "light" else "#FFFFFF"
        light = name == "light"

        self.brand      = raw["brand"]
        self.on_brand   = raw["on_brand"]
        self.bg_base    = raw["bg_base"]
        self.layer1     = raw["layer1"]
        self.layer2     = raw["layer2"]
        self.overlay    = raw["overlay"]
        self.line1      = raw["line1"]
        self.line2      = raw["line2"]
        self.label1     = raw["label1"]
        self.label2     = raw["label2"]
        self.success    = raw["success"]
        self.warn       = raw["warn"]
        self.error      = raw["error"]
        self.sidebar    = raw["sidebar"]
        self.shadow     = raw["shadow"]
        self.light      = light

        # 品牌色三态（暗色下往白走、亮色下往黑走）
        self.brand_hover = mix(self.brand, towards, 0.12)
        self.brand_press = mix(self.brand, towards, 0.24)
        # 品牌色的"水彩"底色：作为选中行 / 软标签底
        self.brand_wash  = mix(self.overlay, self.brand, 0.10 if light else 0.18)
        self.brand_line  = mix(self.overlay, self.brand, 0.28 if light else 0.38)

        # 状态色的底 / 边（软标签、错误框）
        self.success_bg  = mix(self.layer1, self.success, 0.13 if light else 0.16)
        self.success_line = mix(self.layer1, self.success, 0.34 if light else 0.42)
        self.warn_bg     = mix(self.layer1, self.warn, 0.14 if light else 0.18)
        self.warn_line   = mix(self.layer1, self.warn, 0.36 if light else 0.46)
        self.error_bg    = mix(self.layer1, self.error, 0.11 if light else 0.16)
        self.error_line  = mix(self.layer1, self.error, 0.32 if light else 0.42)

        # 状态色「文字变体」：原始色是拿来当圆点/描边的，直接当文字写在同色淡底上
        # 只有 2.8~3.5:1（浅海）。文字要 4.5，所以浅海往主文字色压深；深海本来就够。
        self.success_fg = self.success if not light else mix(self.success, self.label1, 0.45)
        self.warn_fg    = self.warn    if not light else mix(self.warn, self.label1, 0.45)
        self.error_fg   = self.error   if not light else mix(self.error, self.label1, 0.45)

        # 中性：inset 底色、悬停底、装饰色
        self.inset       = self.layer2
        self.hover_fill  = mix(self.layer1, self.brand, 0.06 if light else 0.10)
        self.press_fill  = mix(self.layer1, self.brand, 0.13 if light else 0.17)
        # 只给"圆点 / 分隔线 / 有意义的边界"这类非文字元素用，不承载文字信息
        self.decor       = mix(self.label2, self.bg_base, 0.30 if light else 0.35)
        # 输入框聚焦描边、开关轨道这类「边界本身表达状态」的线，要 3:1
        self.line_strong = mix(self.label2, self.bg_base, 0.28 if light else 0.32)

        # 禁用态：仍要能看清，但明显退后（WCAG 对禁用控件豁免，这里仍按 3:1 要求）
        self.disabled_fg   = mix(self.label2, self.bg_base, 0.18)
        self.disabled_fill = mix(self.line1, self.bg_base, 0.60)
        self.disabled_line = mix(self.line1, self.bg_base, 0.40)

        # 滚动条滑块
        self.thumb       = mix(self.label2, self.bg_base, 0.55 if light else 0.45)
        self.thumb_hover = self.label2

    # 兼容 `t["brand"]` 写法
    def __getitem__(self, key: str) -> str:
        return getattr(self, key)

    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __repr__(self) -> str:      # pragma: no cover
        return f"<Palette {self.name}>"


@lru_cache(maxsize=4)
def palette(name: str = "light") -> Palette:
    return Palette("dark" if str(name).lower().startswith("d") else "light")


# ── 间距：4/8 节奏（UI UX Pro Max 第 6 条）──────────────────────────────────
SP = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}

# ── 字号：Tk 用点(pt)，@96dpi 1pt≈1.333px ───────────────────────────────────
# 8pt≈10.7px(最小标签) 9pt≈12px(芯片/说明，正好卡在 12px 下限)
# 10pt≈13.3px(正文) 11pt≈14.7px 13pt≈17.3px(标题) 17pt≈22.7px(展示)
FS = {"xs": 8, "sm": 9, "base": 10, "md": 11, "lg": 13, "xl": 17}

# ── 圆角：6 芯片 / 8 按钮 / 10 卡片 / 14 浮层 / 999 胶囊 ─────────────────────
RAD = {"xs": 6, "sm": 8, "md": 10, "lg": 14, "pill": 999}

# ── 动效：毫秒（同源 hover .18s / pop .22s）─────────────────────────────────
MOTION = {"fast": 90, "base": 140, "slow": 220, "bob": 3600}

# ── 投影：三级（模糊半径, 垂直偏移, 不透明度）──────────────────────────────
SHADOW = {
    "s1": (6, 1, 0.10),
    "s2": (12, 3, 0.16),
    "s3": (22, 8, 0.24),
}


# ── 自检：把"看得清"变成可验证的数字 ────────────────────────────────────────
def check_contrast(verbose: bool = True) -> list:
    """返回不达标项列表。规则：正文/标签 ≥4.5，非文字（圆点/描边）≥3.0。"""
    pairs = [
        ("label1 on layer1", "label1", "layer1", 4.5),
        ("label1 on bg_base", "label1", "bg_base", 4.5),
        ("label2 on layer1", "label2", "layer1", 4.5),
        ("label2 on bg_base", "label2", "bg_base", 4.5),
        ("label2 on overlay", "label2", "overlay", 4.5),
        ("on_brand on brand", "on_brand", "brand", 4.5),
        ("on_brand on brand_press", "on_brand", "brand_press", 4.5),
        ("success_fg on success_bg", "success_fg", "success_bg", 4.5),
        ("warn_fg on warn_bg", "warn_fg", "warn_bg", 4.5),
        ("error_fg on error_bg", "error_fg", "error_bg", 4.5),
        ("disabled_fg on disabled_fill", "disabled_fg", "disabled_fill", 3.0),
        ("line_strong on layer1 (有意义边界)", "line_strong", "layer1", 3.0),
        ("decor on layer1 (圆点/分隔线)", "decor", "layer1", 3.0),
    ]
    bad = []
    for theme in ("light", "dark"):
        t = palette(theme)
        rows = []
        for label, fg, bg, need in pairs:
            c = contrast(t[fg], t[bg])
            ok = c >= need
            rows.append((label, c, need, ok))
            if not ok:
                bad.append((theme, label, round(c, 2), need))
        if verbose:
            print(f"\n=== {theme} ===")
            for label, c, need, ok in rows:
                print(f"  {'OK ' if ok else '!! '} {label:34s} {c:5.2f} : 1  (需 {need})")
    if verbose:
        print("\n不达标：" if bad else "\n全部达标 ✔")
        for row in bad:
            print("  ", row)
    return bad


if __name__ == "__main__":
    try:                                # Windows 控制台是 GBK，中文表头会花
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--check" in sys.argv:
        sys.exit(1 if check_contrast() else 0)
    t = palette("light")
    print("light:", t.brand, t.bg_base, t.label1, t.label2)
    print("dark :", palette("dark").brand, palette("dark").bg_base)
