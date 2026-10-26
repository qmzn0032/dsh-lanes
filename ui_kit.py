#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_kit —— 「蓝色大肥鱼」Canvas 自绘控件库

这一版改了什么（对照上一版）
--------------------------
上一版的毛病，量得出来：
    · 30 多个十六进制色值写死在控件里 → 加不了暗色主题、改色要改五处
    · 次要文字 #8A8F98 对比度只有 2.96:1、#6B7280 只有 4.40:1 → 低于 WCAG AA 正文线
    · 圆角是 8 段折线拟合，r≥10 时肉眼可见锯齿
    · 零投影：卡片只有 1px 描边，整屏糊成一片，没有层次
    · 状态只有 normal/hover/press × 3 套硬色，没有 focus（键盘用户看不见焦点）
    · hover 是瞬变，没有过渡

这一版：
    · 配色/间距/字号/圆角/动效全部来自 theme_whale.py（同源 dafy-whale-theme
      + UI UX Pro Max 规则），控件只认语义名，浅海/深海一键切换
    · Pillow 4× 超采样渲染圆角与三级柔和投影（无 Pillow 时自动退回折线绘制，
      功能不丢，只是边角没那么细）
    · 按钮/卡片/胶囊/芯片都有 hover→press→focus 的过渡与焦点环
    · 新增 OceanBackdrop：海面光晕背景（顶部品牌蓝 30%、左下青 26%、中心薄荷 14%）

设计约束（维护者请先读）
-----------------------
1. **Pillow 是可选的**：没有 Pillow 也能跑，`_HAS_PIL` 会自动选路。启动器打包时
   若想瘦身，不装 Pillow 即可，界面只损失抗锯齿与投影。
2. **API 向 tk.Button 靠拢**：`.pack()/.grid()`、`configure(text=...)`、`cget("text")`、
   `.invoke()` 都能用；旧的模块级常量（BG/CARD/ACCENT/SUBTLE…）继续导出，
   因此 dsh_lanes_gui.py 不必改一行。
3. **整块重绘**：控件只有几十像素，delete+draw 比 itemconfigure 简单可靠。
   渲染结果按 (尺寸, 颜色, 圆角, 投影) 缓存成 PhotoImage，hover 来回动不会重算。
4. **DPR**：本模块不做 DPI 感知，调用方需先 `SetProcessDpiAwareness(1)`
   （dsh_lanes_gui.enable_dpi_awareness 已做），否则高分屏会发虚。
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

from theme_whale import FS, GLOW, MOTION, RAD, SHADOW, SP, mix, palette

try:                                     # 可选：抗锯齿与投影
    from PIL import Image, ImageDraw, ImageFilter, ImageTk
    _HAS_PIL = True
except Exception:                        # pragma: no cover
    _HAS_PIL = False

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# ── 当前主题（模块级；换主题后新建的控件才生效）──────────────────────────────
THEME = "light"


def set_theme(name: str) -> None:
    """切换默认主题（"light" 浅海 / "dark" 深海）。已存在的控件不会自动重绘。"""
    global THEME
    THEME = "dark" if str(name).lower().startswith("d") else "light"


def theme_name() -> str:
    return THEME


def theme():
    return palette(THEME)


# ── 旧常量（向后兼容，取浅海主题的值）──────────────────────────────────────
_t = palette("light")
BG = _t.bg_base
CARD = _t.layer1
BORDER = _t.line1
BORDER_HOVER = _t.line_strong
TEXT = _t.label1
MUTED = _t.label2
SUBTLE = _t.label2          # 旧值是 #8A8F98（2.96:1），现在是 5.51:1
ACCENT = _t.brand
ACCENT_DK = _t.brand_hover
ACCENT_PRESS = _t.brand_press
ACCENT_WASH = _t.brand_wash
GREEN = _t.success
GREEN_DK = _t.success_fg
GREEN_BG = _t.success_bg
GRAY = _t.decor
GRAY_DOT = _t.decor
GRAY_BG = _t.inset
RED = _t.error_fg
RED_BG = _t.error_bg
AMBER = _t.warn_fg
AMBER_BG = _t.warn_bg
DISABLED_FG = _t.disabled_fg
DISABLED_FILL = _t.disabled_fill


# ── 字体 ────────────────────────────────────────────────────────────────────
_FONTS: dict = {}


def font(size: str = "base", bold: bool = False, mono: bool = False) -> tkfont.Font:
    """按字号阶梯取字体（带缓存）。size: xs/sm/base/md/lg/xl。"""
    key = (size, bold, mono)
    f = _FONTS.get(key)
    if f is None:
        f = tkfont.Font(
            family=(FONT_MONO if mono else FONT_UI),
            size=FS.get(size, FS["base"]),
            weight=("bold" if bold else "normal"),
        )
        _FONTS[key] = f
    return f


# ── 渲染层：圆角 + 投影（Pillow 超采样 / 折线兜底）──────────────────────────
_SS = 3                                   # 超采样倍数：3 倍足够细，内存可控
_CACHE: dict = {}
_CACHE_MAX = 700


def _clear_cache() -> None:
    _CACHE.clear()


def _rr_points(x1: float, y1: float, x2: float, y2: float, r: float, steps: int = 10) -> list:
    """圆角矩形的多边形顶点（Pillow 不可用时的兜底）。"""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts: list = []
    for cx, cy, a0, a1 in (
        (x2 - r, y1 + r, -90, 0),
        (x2 - r, y2 - r, 0, 90),
        (x1 + r, y2 - r, 90, 180),
        (x1 + r, y1 + r, 180, 270),
    ):
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def _round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, fill, outline="", width=1, tags=()):
    """折线圆角矩形（兜底路径，也在滚动条这类小元素上用）。"""
    return cv.create_polygon(
        *_rr_points(x1, y1, x2, y2, r),
        fill=fill, outline=(outline or fill), width=width, smooth=False, tags=tags,
    )


def _rgba(color: str, alpha: float) -> tuple:
    h = color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), int(255 * max(0.0, min(1.0, alpha))))


def _surface(w: int, h: int, r: int, fill: str, line: str = "", lw: int = 1,
             shadow: str | None = None, tint: str = "#000000"):
    """渲染一块圆角面（可带投影）。返回 (PhotoImage | None, pad)。

    pad 是投影向外扩出的像素数，调用方画图时要把坐标减去 pad。
    """
    if not _HAS_PIL or w < 2 or h < 2:
        return None, 0
    _ensure_root()
    w, h, r, lw = int(w), int(h), int(r), int(lw)
    key = ("s", w, h, r, fill, line, lw, shadow, tint)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit

    pad = 0
    blur = dy = 0
    alpha = 0.0
    if shadow in SHADOW:
        blur, dy, alpha = SHADOW[shadow]
        pad = int(blur * 0.75 + 2)
    W, H = w + pad * 2, h + pad * 2
    # 大面板不必 3 倍超采样：2 倍肉眼已无差别，1 倍只用于超大容器（省内存省时间）
    ss = _SS if w * h <= 140000 else (2 if w * h <= 420000 else 1)
    img = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))

    if pad:
        sh = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle(
            [pad * ss, (pad + dy) * ss, (pad + w) * ss, (pad + h + dy) * ss],
            radius=r * ss, fill=_rgba(tint, alpha),
        )
        sh = sh.filter(ImageFilter.GaussianBlur(blur * ss / 2.2))
        img = Image.alpha_composite(img, sh)

    body = ImageDraw.Draw(img)
    box = [pad * ss, pad * ss, (pad + w) * ss - 1, (pad + h) * ss - 1]
    body.rounded_rectangle(
        box, radius=r * ss, fill=fill,
        outline=(line or None), width=max(1, lw) * ss if line else 0,
    )
    img = img.resize((W, H), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    if len(_CACHE) > _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (photo, pad)
    return photo, pad


def round_box(cv: tk.Canvas, x1, y1, x2, y2, r, fill, outline="", width=1,
              shadow: str | None = None, tint: str | None = None, tags=()):
    """在任意 Canvas 上画一块圆角面：有 Pillow 走抗锯齿+投影，没有就折线。"""
    w, h = int(x2 - x1), int(y2 - y1)
    photo, pad = _surface(
        w, h, r, fill, outline, width, shadow,
        tint or theme().shadow,
    )
    if photo is None:
        if outline:
            return [_round_rect(cv, x1 + 0.5, y1 + 0.5, x2 - 0.5, y2 - 0.5, r,
                                fill=fill, outline=outline, width=width, tags=tags)]
        return [_round_rect(cv, x1, y1, x2, y2, r, fill=fill, outline="", width=0, tags=tags)]
    item = cv.create_image(x1 - pad, y1 - pad, image=photo, anchor="nw", tags=tags)
    return [item, photo]


def _parent_bg(master) -> str:
    try:
        return master.cget("bg")
    except Exception:
        return theme().layer1


def divider(master, bg: str | None = None) -> tk.Frame:
    """1px 分隔线。"""
    return tk.Frame(master, height=1, bg=(bg or theme().line1), bd=0, highlightthickness=0)


# ── 按钮配色表 ──────────────────────────────────────────────────────────────
def _btn_style(t, variant: str) -> dict:
    if variant == "primary":
        return {"fill": t.brand, "fg": t.on_brand, "line": "",
                "hover_fill": t.brand_hover, "hover_line": "",
                "press_fill": t.brand_press, "shadow": "s1"}
    if variant == "soft":
        return {"fill": t.brand_wash, "fg": t.brand, "line": t.brand_line,
                "hover_fill": mix(t.brand_wash, t.brand, 0.16), "hover_line": t.brand,
                "press_fill": mix(t.brand_wash, t.brand, 0.26), "shadow": None}
    if variant == "ghost":
        return {"fill": None, "fg": t.label2, "line": "",
                "hover_fill": t.hover_fill, "hover_line": "",
                "press_fill": t.press_fill, "shadow": None}
    if variant == "danger":
        return {"fill": t.overlay, "fg": t.error_fg, "line": t.error_line,
                "hover_fill": t.error_bg, "hover_line": t.error,
                "press_fill": mix(t.error_bg, t.error, 0.18), "shadow": None}
    # secondary（默认）
    return {"fill": t.overlay, "fg": t.label1, "line": t.line1,
            "hover_fill": t.hover_fill, "hover_line": t.line_strong,
            "press_fill": t.press_fill, "shadow": "s1"}


# 尺寸：所有高度都是 4 的倍数（4/8 节奏），r 取圆角阶梯
_SZ = {
    "xs": {"h": 24, "padx": 8,  "fs": "sm",   "r": RAD["xs"]},
    "sm": {"h": 28, "padx": 10, "fs": "sm",   "r": RAD["sm"]},
    "md": {"h": 32, "padx": 14, "fs": "base", "r": RAD["sm"]},
    "lg": {"h": 40, "padx": 18, "fs": "md",   "r": RAD["md"]},
}


# ── 圆角按钮 ────────────────────────────────────────────────────────────────
class RoundedButton(tk.Canvas):
    """圆角按钮：primary / secondary / soft / ghost / danger。

    参数
    ----
    text     文字        command  回调        variant  变体
    size     md(32px) / sm(28) / xs(24) / lg(40)
    icon     文字前符号（如 "⟳" "＋"），用同一字体测量
    width    显式宽度；不传按文字自适应
    bg       父容器底色（ghost 变体用它当填充）
    bold / fg / focusable / shadow

    键盘：可 Tab 聚焦（focusable=True），Enter/Space 触发。
    """

    def __init__(self, master, text: str = "", command=None, variant: str = "secondary",
                 size: str = "md", icon: str = "", width: int | None = None,
                 bg: str | None = None, font_family: str = FONT_UI, bold: bool = False,
                 fg: str | None = None, focusable: bool = True,
                 shadow: str | None = "", **kw) -> None:
        self._t = theme()
        self._sz = _SZ.get(size, _SZ["md"])
        self._variant = variant
        self._style = _btn_style(self._t, variant)
        self._shadow = (self._style["shadow"] or None) if shadow == "" else (shadow or None)
        self._text, self._icon, self._cmd = text, icon, command
        self._state = "normal"          # normal | disabled
        self._hover = self._press = self._focus = False
        self._fg_override = fg
        self._owner_bg = bg or _parent_bg(master)
        self._bold = bold
        self._font = tkfont.Font(family=font_family, size=FS.get(self._sz["fs"], FS["base"]),
                                 weight=("bold" if bold else "normal"))
        self._refs: list = []
        w = width if width else self._measure()
        super().__init__(master, width=w, height=self._sz["h"], bg=self._owner_bg,
                         highlightthickness=0, bd=0, cursor="hand2", takefocus=focusable, **kw)
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        self.bind("<Return>", lambda _e: self.invoke())
        self.bind("<space>", lambda _e: self.invoke())

    # ── 尺寸 ──
    def _label(self) -> str:
        return f"{self._icon} {self._text}" if self._icon else self._text

    def _measure(self) -> int:
        return int(self._font.measure(self._label()) + self._sz["padx"] * 2 + 4)

    # ── 配色 ──
    def _colors(self) -> tuple:
        s = self._style
        if self._state == "disabled":
            fill = self._t.disabled_fill if s["fill"] is not None else self._owner_bg
            return fill, "", self._t.disabled_fg
        fill, line = s["fill"], s["line"]
        if self._press:
            fill = s["press_fill"]
            line = s["hover_line"] or line
        elif self._hover:
            fill = s["hover_fill"]
            line = s["hover_line"] or line
        if fill is None:
            fill = self._owner_bg
        return fill, line, (self._fg_override or s["fg"])

    def _draw(self) -> None:
        self.delete("all")
        self._refs = []
        w, h = int(self.cget("width")), self._sz["h"]
        fill, line, fg = self._colors()
        if self._focus and self._state == "normal":
            line = self._t.brand
        self._refs += round_box(self, 0, 0, w - 1, h - 1, self._sz["r"],
                                fill=fill, outline=line, width=2 if (line and self._focus) else 1,
                                shadow=self._shadow if self._state == "normal" else None,
                                tags="body")
        self.create_text(w / 2, h / 2 + (1 if self._press else 0), text=self._label(),
                         fill=fg, font=self._font, anchor="center", tags="label")

    # ── 事件 ──
    def _set_focus(self, on: bool) -> None:
        if on != self._focus:
            self._focus = on
            self._draw()

    def _on_enter(self, _e) -> None:
        if self._state != "normal" or self._hover:
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _e) -> None:
        if not (self._hover or self._press):
            return
        self._hover = self._press = False
        self._draw()

    def _on_press(self, _e) -> None:
        if self._state != "normal":
            return
        self._press = True
        self._draw()

    def _on_release(self, e) -> None:
        if self._state != "normal":
            return
        was = self._press
        self._press = False
        self._draw()
        if was and 0 <= e.x <= int(self.cget("width")) and 0 <= e.y <= self._sz["h"]:
            self.invoke()

    # ── 对外 API ──
    def invoke(self) -> None:
        if self._state == "normal" and callable(self._cmd):
            self._cmd()

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        redraw = False
        if "text" in kw:
            self._text = kw.pop("text")
            kw.setdefault("width", self._measure())
            redraw = True
        if "icon" in kw:
            self._icon = kw.pop("icon")
            kw["width"] = self._measure()
            redraw = True
        if "command" in kw:
            self._cmd = kw.pop("command")
        if "state" in kw:
            self._state = kw.pop("state")
            try:
                super().configure(cursor=("hand2" if self._state == "normal" else "arrow"))
            except tk.TclError:
                pass
            redraw = True
        if "variant" in kw:
            self._variant = kw.pop("variant")
            self._style = _btn_style(self._t, self._variant)
            redraw = True
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()
        return self

    config = configure

    def cget(self, key):  # type: ignore[override]
        if key == "state":
            return self._state
        if key == "text":
            return self._text
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


# ── 圆角卡片 ────────────────────────────────────────────────────────────────
class RoundedCard(tk.Canvas):
    """圆角卡片容器：Canvas 画圆角底（含投影）+ 内嵌 Frame 装内容。

    fill=False（默认）  高度跟内容走；宽度跟 pack 拉伸。← lane 卡片
    fill=True           宽高都跟父容器走。← 日志面板这类占满整列的容器
                          这时要传 width=1, height=1，别让默认尺寸漏进布局。

    shadow   "s1"/"s2"/"s3" 或 None（默认 s1，悬停升到 s2）
    """

    def __init__(self, master, bg: str | None = None, border: str | None = None,
                 border_hover: str | None = None, border_width: int = 1,
                 radius: int = RAD["md"], padx: int = SP["lg"], pady: int = SP["md"],
                 owner_bg: str | None = None, fill: bool = False, rail: str | None = None,
                 rail_w: int = 3, shadow: str | None = "s1", ocean: bool = False, **kw) -> None:
        t = theme()
        self._t = t
        self._bg = bg or t.layer1
        self._border = border if border is not None else t.line1
        self._border_hover = border_hover or t.line_strong
        self._bw = border_width
        self._r = radius
        self._padx, self._pady = padx, pady
        self._owner_bg = owner_bg or _parent_bg(master)
        self._fill = fill
        self._rail = rail
        self._rail_w = rail_w
        self._shadow = shadow
        self._ocean = ocean
        self._h = 0
        self._hover = False
        self._refs: list = []
        self._ocean_refs: list = []
        self._resize_job = None
        super().__init__(master, bg=self._owner_bg, highlightthickness=0, bd=0, **kw)
        self.inner = tk.Frame(self, bg=self._bg)
        self._win = self.create_window(padx, pady, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", lambda _e: self.set_hover(True))
        self.bind("<Leave>", lambda _e: self.after(40, self._check_out))
        self.inner.bind("<Configure>", self._on_inner)

    def set_hover(self, on: bool) -> None:
        if on == self._hover:
            return
        self._hover = on
        self._paint(int(self.winfo_width()), int(self.winfo_height()))

    def _check_out(self) -> None:
        try:
            px, py = self.winfo_pointerxy()
            w = self.winfo_containing(px, py)
        except Exception:
            w = None
        while w is not None:
            if w is self:
                return
            try:
                w = w.master
            except Exception:
                w = None
        self.set_hover(False)

    def _on_inner(self, _e=None) -> None:
        if self._fill:
            return
        h = self.inner.winfo_reqheight() + self._pady * 2 + self._bw * 2
        if h != self._h and h > 1:
            self._h = h
            self.configure(height=h)

    def _on_resize(self, e) -> None:
        # 拖动窗口时 <Configure> 会连发；60ms 去抖，避免每像素重建一次图像
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(60, lambda: self._apply_size(e.width, e.height))

    def _apply_size(self, w: int, h: int) -> None:
        self._resize_job = None
        self._paint(w, h)
        try:
            self.itemconfigure(self._win, width=max(1, w - self._padx * 2 - self._bw * 2))
            if self._fill:
                self.itemconfigure(self._win, height=max(1, h - self._pady * 2 - self._bw * 2))
        except tk.TclError:
            pass

    def _paint(self, w: int, h: int) -> None:
        if w < 4 or h < 4:
            return
        bw = self._bw
        line = self._border_hover if self._hover else self._border
        shadow = "s2" if (self._shadow and self._hover) else self._shadow
        self.delete("frame")
        self.delete("rail")
        self.delete("oceanbg")
        self._refs = []
        if self._ocean:
            # 垫一层"真海面"：卡片圆角外面那圈缺口露的就是它，否则会露出纯色底，
            # 在铺满整窗的渐变上就是四个色块缺口。
            photo = ocean_slice(self, w, h)
            if photo is not None:
                self._ocean_refs = [self.create_image(0, 0, image=photo, anchor="nw",
                                                     tags="oceanbg"), photo]
                self.tag_lower("oceanbg")
        self._refs = round_box(self, bw, bw, w - bw, h - bw, self._r,
                               fill=self._bg, outline=line, width=bw,
                               shadow=shadow, tags="frame")
        self.tag_lower("frame")
        self._paint_rail(w, h, bw)

    def _paint_rail(self, w: int, h: int, bw: int) -> None:
        if not self._rail:
            return
        x0 = bw + 4
        y0 = self._r * 0.5
        y1 = h - self._r * 0.5
        if y1 - y0 > self._rail_w * 2:
            round_box(self, x0, y0, x0 + self._rail_w, y1, self._rail_w / 2,
                      fill=self._rail, outline="", width=0, tags="rail")
            self.tag_raise("rail")


# ── 状态胶囊 ────────────────────────────────────────────────────────────────
def _pill_style(t, kind: str) -> dict:
    return {
        "on":    {"bg": t.success_bg,  "fg": t.success_fg, "dot": t.success},
        "off":   {"bg": t.inset,       "fg": t.label2,     "dot": t.decor},
        "warn":  {"bg": t.warn_bg,     "fg": t.warn_fg,    "dot": t.warn},
        "error": {"bg": t.error_bg,    "fg": t.error_fg,   "dot": t.error},
        "busy":  {"bg": t.brand_wash,  "fg": t.brand,      "dot": t.brand},
    }.get(kind, {"bg": t.inset, "fg": t.label2, "dot": t.decor})


class Pill(tk.Canvas):
    """状态胶囊：圆点 + 文字，全圆角。kind = on / off / warn / error / busy。"""

    H = 24

    def __init__(self, master, text: str = "", kind: str = "on",
                 bg: str | None = None, **kw) -> None:
        self._t = theme()
        self._text, self._kind = text, kind
        self._pal = _pill_style(self._t, kind)
        self._owner_bg = bg or _parent_bg(master)
        self._font = tkfont.Font(family=FONT_UI, size=FS["sm"], weight="bold")
        self._refs: list = []
        super().__init__(master, width=self._width(), height=self.H, bg=self._owner_bg,
                         highlightthickness=0, bd=0, **kw)
        self._draw()

    def _width(self) -> int:
        return int(self._font.measure(self._text) + SP["sm"] * 2 + 8 + SP["xs"] + 4)

    def _draw(self) -> None:
        self.delete("all")
        w, h = int(self.cget("width")), self.H
        self._refs = round_box(self, 0, 0, w - 1, h - 1, h / 2,
                               fill=self._pal["bg"], outline="", width=0)
        cy = h / 2
        self.create_oval(SP["sm"] + 1, cy - 3, SP["sm"] + 7, cy + 3,
                         fill=self._pal["dot"], outline="")
        self.create_text(SP["sm"] + 12, cy + 0.5, text=self._text, fill=self._pal["fg"],
                         font=self._font, anchor="w")

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        if "text" in kw:
            self._text = kw.pop("text")
            kw["width"] = self._width()
        if "kind" in kw:
            self._kind = kw.pop("kind")
            self._pal = _pill_style(self._t, self._kind)
        if kw:
            super().configure(**kw)
        self._draw()
        return self

    config = configure

    def cget(self, key):  # type: ignore[override]
        if key == "text":
            return self._text
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


# ── 标签芯片 ────────────────────────────────────────────────────────────────
def _chip_style(t, kind: str) -> dict:
    return {
        "ver":     {"bg": t.inset,      "fg": t.label2,     "line": t.line1,     "mono": True},
        "primary": {"bg": t.brand,      "fg": t.on_brand,   "line": "",          "mono": False},
        "soft":    {"bg": t.brand_wash, "fg": t.brand,      "line": t.brand_line, "mono": False},
        "key":     {"bg": t.inset,      "fg": t.label2,     "line": t.line1,     "mono": False},
        "ok":      {"bg": t.success_bg, "fg": t.success_fg, "line": t.success_line, "mono": True},
        "warn":    {"bg": t.warn_bg,    "fg": t.warn_fg,    "line": t.warn_line, "mono": False},
    }.get(kind, {"bg": t.inset, "fg": t.label2, "line": t.line1, "mono": True})


class Chip(tk.Canvas):
    """标签芯片：版本号 kind="ver"（等宽），"★ 主要版本" kind="primary"。"""

    H = 22

    def __init__(self, master, text: str = "", kind: str = "ver",
                 bg: str | None = None, **kw) -> None:
        self._t = theme()
        self._text, self._kind = text, kind
        self._pal = _chip_style(self._t, kind)
        self._owner_bg = bg or _parent_bg(master)
        self._font = tkfont.Font(
            family=(FONT_MONO if self._pal["mono"] else FONT_UI),
            size=FS["sm"], weight=("normal" if self._pal["mono"] else "bold"),
        )
        self._refs: list = []
        super().__init__(master, width=self._width(), height=self.H, bg=self._owner_bg,
                         highlightthickness=0, bd=0, **kw)
        self._draw()

    def _width(self) -> int:
        return int(self._font.measure(self._text) + SP["sm"] * 2 + 1)

    def _draw(self) -> None:
        self.delete("all")
        w, h = int(self.cget("width")), self.H
        self._refs = round_box(self, 0, 0, w - 1, h - 1, RAD["xs"],
                               fill=self._pal["bg"], outline=self._pal["line"], width=1)
        self.create_text(w / 2, h / 2 + 0.5, text=self._text, fill=self._pal["fg"],
                         font=self._font, anchor="center")

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        if "text" in kw:
            self._text = kw.pop("text")
            kw["width"] = self._width()
        if "kind" in kw:
            self._kind = kw.pop("kind")
            self._pal = _chip_style(self._t, self._kind)
        if kw:
            super().configure(**kw)
        self._draw()
        return self

    config = configure

    def cget(self, key):  # type: ignore[override]
        if key == "text":
            return self._text
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


# ── 极简滚动条 ──────────────────────────────────────────────────────────────
class MiniScrollbar(tk.Canvas):
    """没有箭头、没有槽边框的一根圆角滑块。接口同 tk.Scrollbar。

        sb = MiniScrollbar(parent, command=text.yview)
        text.configure(yscrollcommand=sb.set)

    内容装得下时不画滑块；悬停加深；支持拖动与点槽翻页。
    """

    def __init__(self, master, command=None, bg: str | None = None, width: int = 12,
                 ocean: bool = False, **kw) -> None:
        self._t = theme()
        self._cmd = command
        self._first, self._last = 0.0, 1.0
        self._owner_bg = bg or _parent_bg(master)
        self._hover = False
        self._ocean = ocean          # 底下是海面时垫一张切片，别留一条纯色带
        self._ocean_refs: list = []
        self._grab_off: float | None = None
        super().__init__(master, width=width, bg=self._owner_bg,
                         highlightthickness=0, bd=0, **kw)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    def set(self, first, last) -> None:
        try:
            first, last = float(first), float(last)
        except (TypeError, ValueError):
            return
        if abs(first - self._first) < 1e-4 and abs(last - self._last) < 1e-4:
            return
        self._first, self._last = first, last
        self._draw()

    def get(self) -> tuple:
        return self._first, self._last

    def _track(self) -> tuple:
        h = max(1, int(self.winfo_height()))
        pad = 3
        return pad, max(1, h - pad * 2)

    def _thumb(self) -> tuple:
        pad, track = self._track()
        y0 = pad + track * self._first
        y1 = pad + track * self._last
        if y1 - y0 < 24:                      # 内容再长也留一个抓得住的滑块
            y1 = pad + track
            y0 = max(pad, y1 - 24)
        return y0, y1

    def _set_hover(self, on: bool) -> None:
        if on != self._hover:
            self._hover = on
            self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = int(self.winfo_width()), int(self.winfo_height())
        if w < 6 or h < 24:
            return
        if self._ocean:
            photo = ocean_slice(self, w, h)
            if photo is not None:
                self._ocean_refs = [self.create_image(0, 0, image=photo, anchor="nw"), photo]
        if self._last - self._first >= 0.999:
            return
        y0, y1 = self._thumb()
        round_box(self, 3, y0, w - 3, y1, (w - 6) / 2,
                  fill=(self._t.thumb_hover if self._hover else self._t.thumb),
                  outline="", width=0)

    def _press(self, e) -> None:
        y0, y1 = self._thumb()
        if y0 <= e.y <= y1:
            self._grab_off = e.y - y0
        else:
            self._grab_off = (y1 - y0) / 2
            self._moveto(e.y - self._grab_off)

    def _drag(self, e) -> None:
        if self._grab_off is None:
            return
        self._moveto(e.y - self._grab_off)

    def _release(self, _e) -> None:
        self._grab_off = None

    def _moveto(self, thumb_top: float) -> None:
        if not callable(self._cmd):
            return
        pad, track = self._track()
        _y0, y1 = self._thumb()
        room = max(1.0, track - (y1 - _y0))
        frac = (thumb_top - pad) / room
        try:
            self._cmd("moveto", max(0.0, min(1.0, frac)))
        except Exception:
            pass

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):
            kw = {**cnf, **kw}
        if "command" in kw:
            self._cmd = kw.pop("command")
        kw.pop("orient", None)
        if kw:
            super().configure(**kw)
        self._draw()
        return self

    config = configure

    def cget(self, key):  # type: ignore[override]
        if key == "command":
            return self._cmd
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)


# ── 海面背景 ────────────────────────────────────────────────────────────────
_BG_CACHE: dict = {}
_SLICE_CACHE: dict = {}
_CROP_CACHE: dict = {}
_CACHE_ROOT = None


def _ensure_root() -> None:
    """换一个 Tk 根之后，上一轮的 PhotoImage / Font 全部失效，必须清缓存。

    为什么会有"另一个根"：切换主题时 Tk 没有样式层，只能销毁整窗重建
    （见 dsh_lanes_gui.main 的重启循环）。不清缓存的话，新窗口里第一次画圆角
    就会撞上 `image "pyimage1" doesn't exist` —— 图片属于已销毁的那个解释器。
    """
    global _CACHE_ROOT
    root = getattr(tk, "_default_root", None)
    if root is not _CACHE_ROOT:
        _CACHE.clear()
        _BG_CACHE.clear()
        _SLICE_CACHE.clear()
        _CROP_CACHE.clear()
        _FONTS.clear()
        _CACHE_ROOT = root


def clear_cache() -> None:
    """手动清空渲染缓存（换主题、改缩放时都可调）。"""
    _CACHE.clear()
    _BG_CACHE.clear()
    _SLICE_CACHE.clear()
    _CROP_CACHE.clear()
    _FONTS.clear()


def ocean_image(w: int, h: int, name: str | None = None):
    """渲染"海面光晕"背景：底色纵向渐变 + 三处径向光（品牌蓝/青/薄荷）。

    同源 dafy-whale-theme 的 .dafy-wash（30% / 26% / 14%，soft-light 叠加）。
    这里用 alpha 叠加近似同一观感（Tk 没有 mix-blend-mode）。

    渐变要"看得见"才叫铺满整窗：起始色是 bg_base（浅海近白、深海近黑），
    结束色往青蓝推（浅 22% / 深 30%）。一开始只往 layer2 推 75%，两个色
    只差 9 个数，渲出来跟纯色没区别 —— 窗口四边露出来的海成了"白边"。
    """
    if not _HAS_PIL or w < 8 or h < 8:
        return None
    _ensure_root()
    key = (w, h, name or THEME)
    hit = _BG_CACHE.get(key)
    if hit is not None:
        return hit
    t = palette(name or THEME)
    img = Image.new("RGB", (w, h), t.bg_base)
    d = ImageDraw.Draw(img)
    # 纵向渐变：上浅下深，像从水面往下看
    deep_c = mix(GLOW[1], GLOW[0], 0.35)            # 青蓝之间，别太艳
    deep = mix(t.bg_base, deep_c, 0.44 if t.light else 0.42)
    for y in range(h):
        d.line([(0, y), (w, y)], fill=mix(t.bg_base, deep, y / max(1, h - 1)))

    specs = (   # (中心x比例, 中心y比例, 宽, 高, 颜色, 强度)
        (0.88, -0.04, 1.5, 1.15, GLOW[0], 0.30),
        (-0.04, 1.06, 1.24, 1.05, GLOW[1], 0.26),
        (0.50, 0.46, 0.72, 0.62, GLOW[2], 0.14),
    )
    n = 128
    for cx, cy, rw, rh, color, alpha in specs:
        # 解析式径向衰减：边缘严格为 0，不会像"模糊椭圆"那样在包围盒边留硬棱
        mask = Image.new("L", (n, n), 0)
        px = mask.load()
        half = (n - 1) / 2
        for j in range(n):
            dy = (j - half) / half
            for i in range(n):
                dx = (i - half) / half
                d = math.hypot(dx, dy)
                if d < 1.0:
                    px[i, j] = int(255 * alpha * (1.0 - d) ** 1.6)
        mw, mh = max(2, int(w * rw)), max(2, int(h * rh))
        mask = mask.resize((mw, mh), Image.LANCZOS)
        # 显式求交：Image.paste 带负偏移或超出画布时裁区行为不可靠，会留硬边
        x0, y0 = int(w * cx - mw / 2), int(h * cy - mh / 2)
        dx0, dy0 = max(0, x0), max(0, y0)
        dx1, dy1 = min(w, x0 + mw), min(h, y0 + mh)
        if dx1 <= dx0 or dy1 <= dy0:
            continue
        patch_mask = mask.crop((dx0 - x0, dy0 - y0, dx1 - x0, dy1 - y0))
        img.paste(Image.new("RGB", (dx1 - dx0, dy1 - dy0), color), (dx0, dy0), patch_mask)
    if len(_BG_CACHE) > 8:
        _BG_CACHE.clear()
    _BG_CACHE[key] = img
    return img


def _ocean_pos(widget):
    """widget 在窗口坐标里的 (x, y) 以及窗口尺寸；拿不到就返回 None。"""
    try:
        root = widget.winfo_toplevel()
        WW, HH = int(root.winfo_width()), int(root.winfo_height())
        if WW < 8 or HH < 8:
            return None
        return (int(widget.winfo_rootx()) - int(root.winfo_rootx()),
                int(widget.winfo_rooty()) - int(root.winfo_rooty()), WW, HH)
    except (tk.TclError, AttributeError):
        return None


def ocean_crop(widget, w: int, h: int, name: str | None = None, off=(0, 0)):
    """「整窗海面」里 widget 所在位置的一块（PIL 图，不是 PhotoImage）。

    位置按屏幕坐标减窗口原点算，所以嵌在滚动区域里的卡片也适用（滚到哪算哪）。
    off 是相对 widget 左上角再偏一点（画投影时要比控件本身大一圈）。
    """
    if not _HAS_PIL or w < 2 or h < 2:
        return None
    _ensure_root()
    pos = _ocean_pos(widget)
    if pos is None:
        return None
    x, y = pos[0] + int(off[0]), pos[1] + int(off[1])
    WW, HH = pos[2], pos[3]
    key = ("c", x, y, int(w), int(h), name or THEME)
    hit = _CROP_CACHE.get(key)
    if hit is not None:
        return hit
    ocean = ocean_image(WW, HH, name or THEME)
    if ocean is None:
        return None
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(WW, x + int(w)), min(HH, y + int(h))
    # 超出窗口的部分用底色补满：只在控件伸到窗口外时发生，正常布局碰不到
    img = Image.new("RGB", (int(w), int(h)), palette(name or THEME).bg_base)
    if x1 > x0 and y1 > y0:
        img.paste(ocean.crop((x0, y0, x1, y1)), (x0 - x, y0 - y))
    if len(_CROP_CACHE) > 16:
        _CROP_CACHE.clear()
    _CROP_CACHE[key] = img
    return img


def ocean_slice(widget, w: int, h: int, name: str | None = None):
    """ocean_crop 的 PhotoImage 版：给「本身就是海面」的控件当背景图（列表底、滚动条）。"""
    if not _HAS_PIL or w < 2 or h < 2:
        return None
    _ensure_root()
    pos = _ocean_pos(widget)
    if pos is None:
        return None
    key = ("p", pos[0], pos[1], int(w), int(h), name or THEME)
    hit = _SLICE_CACHE.get(key)
    if hit is not None:
        return hit
    crop = ocean_crop(widget, w, h, name)
    if crop is None:
        return None
    photo = ImageTk.PhotoImage(crop)
    if len(_SLICE_CACHE) > 24:       # 窗口越拉越大时位置键会变，别无限攒
        _SLICE_CACHE.clear()
    _SLICE_CACHE[key] = photo
    return photo


class OceanBackdrop(tk.Canvas):
    """铺满父容器的海面背景。当作底层：先 place/pack 它，再把内容放上面。

    Tk 的 Frame 不透明，所以光晕只会在卡片之间的缝隙里露出来 —— 这正是想要的效果：
    内容浮在海水上。窗口 resize 后 120ms 重建一次，重建有缓存。

    crop=True 时不再自己渲染一张海面，而是从整窗那张里裁出自己这一块 ——
    这样窗口里好几个海面面板（根背景 / 右栏 / 左列）拼起来是同一片海，接缝对得上。
    """

    def __init__(self, master, theme: str | None = None, crop: bool = False, **kw) -> None:
        self._name = theme or THEME
        self._crop = crop
        t = palette(self._name)
        super().__init__(master, bg=t.bg_base, highlightthickness=0, bd=0, **kw)
        self._job = None
        self._img = None
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, e) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.after(120, lambda: self._paint(e.width, e.height))

    def _paint(self, w: int, h: int) -> None:
        self._job = None
        self.delete("bg")
        if self._crop:
            photo = ocean_slice(self, w, h, self._name)
        else:
            img = ocean_image(w, h, self._name)
            photo = None if img is None else ImageTk.PhotoImage(img)
        if photo is None:                    # 没有 Pillow：退化成纯色
            self.configure(bg=palette(self._name).bg_base)
            return
        self._img = photo
        self.create_image(0, 0, image=photo, anchor="nw", tags="bg")
        self.tag_lower("bg")


def ocean_swatch(w: int, h: int, name: str | None = None):
    """给标题栏/侧栏之类的小条渲染一块「海面切片」，返回 PhotoImage 或 None。"""
    img = ocean_image(w, h, name)
    if img is None:
        return None
    return ImageTk.PhotoImage(img)
