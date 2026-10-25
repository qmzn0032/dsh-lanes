#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_kit —— Canvas 自绘控件库（纯 tkinter，零第三方依赖）

为什么需要它
------------
tkinter 原生控件没有圆角、没有 hover 过渡，按钮只有"按下"才变色。要做出现代桌面
应用的观感，只能自己在 Canvas 上画。本模块提供：

    RoundedButton   圆角按钮（4 变体 × 3 尺寸 × 3 态 × 可选图标）
    RoundedCard     圆角卡片 / 圆角面板（fill=True 时撑满父容器，装日志这类可滚动区）
    Pill            状态胶囊（圆点 + 文字，全圆角）
    Chip            标签芯片（版本号 / ★主要版本）
    MiniScrollbar   极简滚动条（只有一根圆角滑块，接口同 tk.Scrollbar）
    divider()       1px 分隔线工厂

设计约束（维护者请先读）
------------------------
1. **不引第三方依赖**：启动器用 PyInstaller 打包发布，多一个依赖就多几 MB。
   本模块只用标准库 tkinter + math。
2. **API 向 tk.Button 靠拢**：`.pack()/.grid()`、`configure(text=...)`、`cget("text")`、
   `.invoke()` 都能用。这样把老的 `tk.Button` 换掉时，调用点不必改。
3. **整块重绘**：按钮只有几十像素，delete+draw 比 itemconfigure 更简单可靠。
4. **不做颜色渐变**：tkinter 逐帧改色要重建图形项，对 20 多个按钮不划算；
   三态直接切换在 120ms 内看不出跳变。
5. **DPR**：本模块不做 DPI 感知，调用方需先 `SetProcessDpiAwareness(1)`
   （dsh_lanes_gui.enable_dpi_awareness 已做），否则高分屏会发虚。

配色与 `_ui-preview/launcher-redesign.html` 一一对应，改一处要同步另一处。
"""

from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont

# ── 设计令牌 ────────────────────────────────────────────────────────────────
BG = "#F4F5F7"
CARD = "#FFFFFF"
BORDER = "#E3E5E9"
BORDER_HOVER = "#CDD1D8"
TEXT = "#1F2328"
MUTED = "#6B7280"
SUBTLE = "#8A8F98"
ACCENT = "#4D6BFE"
ACCENT_DK = "#3A56E0"
ACCENT_PRESS = "#2F49C9"
ACCENT_WASH = "#F5F7FF"
GREEN = "#16A34A"
GREEN_DK = "#13803A"
GREEN_BG = "#E8F6EE"
GRAY = "#9CA3AF"
GRAY_DOT = "#B6BBC3"
GRAY_BG = "#F0F1F3"
RED = "#DC2626"
RED_BG = "#FDECEC"
AMBER = "#B45309"
AMBER_BG = "#FEF3E2"
DISABLED_FG = "#A7ADB6"
DISABLED_FILL = "#F0F1F3"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# 按钮变体：fill=None 表示透明（露出父容器底色）
_BTN = {
    "primary": {
        "fill": ACCENT, "fg": "#FFFFFF", "line": "",
        "hover_fill": ACCENT_DK, "hover_line": "", "press_fill": ACCENT_PRESS,
    },
    "secondary": {
        "fill": "#FFFFFF", "fg": TEXT, "line": BORDER,
        "hover_fill": "#F7F8FA", "hover_line": BORDER_HOVER, "press_fill": "#EFF1F4",
    },
    "ghost": {
        "fill": None, "fg": "#4B5563", "line": "",
        "hover_fill": "#EDEEF1", "hover_line": "", "press_fill": "#E4E6EA",
    },
    "danger": {
        "fill": "#FFFFFF", "fg": RED, "line": "#F3D3D3",
        "hover_fill": RED_BG, "hover_line": "#EBB4B4", "press_fill": "#F9DCDC",
    },
}

# 尺寸：h=高度，padx=左右内边距，size=字号(pt)，r=圆角半径
_SZ = {
    "xs": {"h": 22, "padx": 8, "size": 9, "r": 6},
    "sm": {"h": 26, "padx": 10, "size": 9, "r": 7},
    "md": {"h": 30, "padx": 13, "size": 9, "r": 8},
}


# ── 底层：圆角矩形 ──────────────────────────────────────────────────────────
def _rr_points(x1: float, y1: float, x2: float, y2: float, r: float, steps: int = 8) -> list:
    """生成圆角矩形的多边形顶点（四个角各 steps 段圆弧，顺时针）。"""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts: list = []
    corners = (
        (x2 - r, y1 + r, -90, 0),    # 右上
        (x2 - r, y2 - r, 0, 90),     # 右下
        (x1 + r, y2 - r, 90, 180),   # 左下
        (x1 + r, y1 + r, 180, 270),  # 左上
    )
    for cx, cy, a0, a1 in corners:
        for i in range(steps + 1):
            a = math.radians(a0 + (a1 - a0) * i / steps)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def _round_rect(cv: tk.Canvas, x1, y1, x2, y2, r, fill, outline="", width=1, tags=()):
    """在 Canvas 上画一个圆角矩形（多边形近似）。"""
    pts = _rr_points(x1, y1, x2, y2, r)
    return cv.create_polygon(
        *pts, fill=fill, outline=(outline or fill), width=width,
        smooth=False, tags=tags,
    )


def _parent_bg(master) -> str:
    try:
        return master.cget("bg")
    except Exception:
        return CARD


def divider(master, bg: str | None = None) -> tk.Frame:
    """1px 分隔线（tk.Frame 画，比 Canvas 省资源）。"""
    return tk.Frame(master, height=1, bg=(bg or BORDER), bd=0, highlightthickness=0)


# ── 圆角按钮 ────────────────────────────────────────────────────────────────
class RoundedButton(tk.Canvas):
    """圆角按钮。

    参数
    ----
    text     按钮文字
    command  点击回调
    variant  primary / secondary / ghost / danger
    size     md(30px) / sm(26px) / xs(22px)
    icon     文字前的符号（如 "⟳" "＋" "■"），用同一字体测量宽度
    width    显式宽度（像素）；不传则按文字自适应
    bg       父容器底色（透明变体要用它当填充色）
    bold     文字是否加粗

    兼容性：`.pack()/.grid()`、`configure(text=..., state=...)`、`.invoke()` 均可用。
    """

    def __init__(
        self,
        master,
        text: str = "",
        command=None,
        variant: str = "secondary",
        size: str = "md",
        icon: str = "",
        width: int | None = None,
        bg: str | None = None,
        font_family: str = FONT_UI,
        bold: bool = False,
        fg: str | None = None,
        **kw,
    ) -> None:
        self._sz = _SZ.get(size, _SZ["md"])
        self._var = _BTN.get(variant, _BTN["secondary"])
        self._variant = variant
        self._text = text
        self._icon = icon
        self._cmd = command
        self._state = "normal"      # normal | disabled
        self._hover = False
        self._press = False
        self._fg_override = fg      # 覆盖变体的文字色（文字链按钮用：蓝/红/琥珀）
        self._owner_bg = bg or _parent_bg(master)
        self._font = tkfont.Font(
            family=font_family, size=self._sz["size"],
            weight=("bold" if bold else "normal"),
        )
        w = width if width else self._measure()
        super().__init__(
            master,
            width=w,
            height=self._sz["h"],
            bg=self._owner_bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            **kw,
        )
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    # ── 尺寸测量 ──
    def _label(self) -> str:
        return f"{self._icon} {self._text}" if self._icon else self._text

    def _measure(self) -> int:
        text_w = self._font.measure(self._label())
        return int(text_w + self._sz["padx"] * 2 + 2)

    # ── 绘制 ──
    def _colors(self) -> tuple:
        """当前应使用的 (填充色, 描边色, 文字色)。"""
        if self._state == "disabled":
            fill = DISABLED_FILL if self._var["fill"] is not None else self._owner_bg
            return fill, "", DISABLED_FG
        fill, line = self._var["fill"], self._var["line"]
        if self._press:
            fill = self._var["press_fill"] if fill is not None else self._var["press_fill"]
            line = self._var["hover_line"] or line
        elif self._hover:
            fill = self._var["hover_fill"] if fill is not None else self._var["hover_fill"]
            line = self._var["hover_line"] or line
        if fill is None:
            fill = self._owner_bg
        return fill, line, (self._fg_override or self._var["fg"])

    def _draw(self) -> None:
        self.delete("all")
        w = int(self.cget("width"))
        h = self._sz["h"]
        fill, line, fg = self._colors()
        # 有描边时内缩 0.5px，避免线被裁掉一半
        if line:
            _round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, self._sz["r"],
                        fill=fill, outline=line, width=1, tags="body")
        else:
            _round_rect(self, 0, 0, w, h, self._sz["r"],
                        fill=fill, outline="", width=0, tags="body")
        self.create_text(
            w / 2,
            h / 2 + (1 if self._press else 0),
            text=self._label(),
            fill=fg,
            font=self._font,
            anchor="center",
            tags="label",
        )

    # ── 事件 ──
    def _on_enter(self, _e) -> None:
        if self._state != "normal" or self._hover:
            return
        self._hover = True
        self._draw()

    def _on_leave(self, _e) -> None:
        if not self._hover and not self._press:
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
        w, h = int(self.cget("width")), self._sz["h"]
        self._draw()
        if was and 0 <= e.x <= w and 0 <= e.y <= h:
            self.invoke()

    # ── 对外 API（向 tk.Button 靠拢）──
    def invoke(self) -> None:
        if self._state == "normal" and callable(self._cmd):
            self._cmd()

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):           # 支持 widget.configure({"text": "x"}) 这种老写法
            kw = {**cnf, **kw}
        redraw = False
        if "text" in kw:
            self._text = kw.pop("text")
            if "width" not in kw:
                kw["width"] = self._measure()
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
            self._var = _BTN.get(self._variant, _BTN["secondary"])
            redraw = True
        if kw:
            super().configure(**kw)
        if redraw:
            self._draw()
        return self

    config = configure

    # 让 `btn["state"]` / `btn.cget("text")` 这类面向 tk.Button 的老写法照常可用
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
    """圆角卡片容器：Canvas 画圆角底 + 内嵌一个 Frame 装内容。

    两种模式
    --------
    fill=False（默认）  高度跟着 inner 的需求高度自动走（内容变了自动撑高），
                        宽度跟着 pack 拉伸。← lane 卡片、说明块用这个
    fill=True           宽高都跟着父容器分配的空间走，inner 撑满内边距。
                        ← 日志面板这种「要占满整列」的容器用这个

    用法：`card = RoundedCard(parent); 内容挂到 card.inner`。
    注意 fill=True 时**不要**让默认请求尺寸（378×265）漏进布局：
    传 `width=1, height=1`，实际大小交给 grid/pack 分配。
    """

    def __init__(
        self,
        master,
        bg: str = CARD,
        border: str = BORDER,
        border_hover: str | None = None,
        border_width: int = 1,
        radius: int = 10,
        padx: int = 14,
        pady: int = 11,
        owner_bg: str | None = None,
        fill: bool = False,
        rail: str | None = None,
        rail_w: int = 3,
        **kw,
    ) -> None:
        self._bg = bg
        self._border = border
        self._border_hover = border_hover or border
        self._bw = border_width
        self._r = radius
        self._padx = padx
        self._pady = pady
        self._owner_bg = owner_bg or _parent_bg(master)
        self._fill = fill
        self._rail = rail            # 左侧状态色条（运行中=绿，已停止=浅灰），None 表示不画
        self._rail_w = rail_w
        self._h = 0
        self._hover = False
        super().__init__(master, bg=self._owner_bg, highlightthickness=0, bd=0, **kw)
        self.inner = tk.Frame(self, bg=bg)
        self._win = self.create_window(padx, pady, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", lambda _e: self.set_hover(True))
        self.bind("<Leave>", lambda _e: self.after(40, self._check_out))
        self.inner.bind("<Configure>", self._on_inner)

    # ── 悬停：鼠标进到子控件时父控件会收到 <Leave>，所以延迟 40ms 再确认指针还在不在卡内 ──
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
                return                      # 还在卡片里，保持悬停
            try:
                w = w.master
            except Exception:
                w = None
        self.set_hover(False)

    def _on_inner(self, _e=None) -> None:
        if self._fill:                      # 撑满模式：高度由外部决定，不跟着内容长
            return
        h = self.inner.winfo_reqheight() + self._pady * 2 + self._bw * 2
        if h != self._h and h > 1:
            self._h = h
            self.configure(height=h)

    def _on_resize(self, e) -> None:
        self._paint(e.width, e.height)
        try:
            self.itemconfigure(self._win, width=max(1, e.width - self._padx * 2 - self._bw * 2))
            if self._fill:
                self.itemconfigure(
                    self._win, height=max(1, e.height - self._pady * 2 - self._bw * 2)
                )
        except tk.TclError:
            pass

    def _paint(self, w: int, h: int) -> None:
        if w < 4 or h < 4:
            return
        bw = self._bw
        line = self._border_hover if self._hover else self._border
        self.delete("frame")
        self.delete("rail")
        if bw:
            _round_rect(self, bw / 2, bw / 2, w - bw / 2, h - bw / 2, self._r,
                        fill=self._bg, outline=line, width=bw, tags="frame")
        else:
            _round_rect(self, 0, 0, w, h, self._r,
                        fill=self._bg, outline="", width=0, tags="frame")
        self.tag_lower("frame")   # 背景压到内嵌窗口下面
        if self._rail:
            # 只在圆角的直边段里画，碰不到四个圆角，所以不会和圆角打架
            x0 = bw + 3
            y0 = self._r * 0.6
            y1 = h - self._r * 0.6
            if y1 - y0 > self._rail_w * 2:
                _round_rect(self, x0, y0, x0 + self._rail_w, y1, self._rail_w / 2,
                            fill=self._rail, outline="", width=0, tags="rail")
                self.tag_raise("rail")


# ── 状态胶囊 ────────────────────────────────────────────────────────────────
_PILL = {
    "on":   {"bg": GREEN_BG, "fg": GREEN_DK, "dot": GREEN},
    "off":  {"bg": GRAY_BG,  "fg": SUBTLE,   "dot": GRAY_DOT},
    "warn": {"bg": AMBER_BG, "fg": AMBER,    "dot": AMBER},
}


class Pill(tk.Canvas):
    """状态胶囊：圆点 + 文字，全圆角。kind = on / off / warn。"""

    H = 23

    def __init__(self, master, text: str = "", kind: str = "on",
                 bg: str | None = None, **kw) -> None:
        self._text = text
        self._kind = kind
        self._pal = _PILL.get(kind, _PILL["off"])
        self._owner_bg = bg or _parent_bg(master)
        self._font = tkfont.Font(family=FONT_UI, size=9, weight="bold")
        w = int(self._font.measure(text) + 9 * 2 + 6 + 5 + 4)
        super().__init__(master, width=w, height=self.H, bg=self._owner_bg,
                         highlightthickness=0, bd=0, **kw)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = int(self.cget("width")), self.H
        _round_rect(self, 0, 0, w, h, h / 2, fill=self._pal["bg"], outline="", width=0)
        cy = h / 2
        self.create_oval(9, cy - 3, 15, cy + 3, fill=self._pal["dot"], outline="")
        self.create_text(20, cy + 0.5, text=self._text, fill=self._pal["fg"],
                         font=self._font, anchor="w")

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):           # 支持 widget.configure({"text": "x"}) 这种老写法
            kw = {**cnf, **kw}
        if "text" in kw:
            self._text = kw.pop("text")
            kw["width"] = int(self._font.measure(self._text) + 9 * 2 + 6 + 5 + 4)
        if "kind" in kw:
            self._kind = kw.pop("kind")
            self._pal = _PILL.get(self._kind, _PILL["off"])
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
_CHIP = {
    "ver": {"bg": "#F3F4F6", "fg": "#6B7280", "line": "#E5E7EB", "mono": True},
    "primary": {"bg": ACCENT, "fg": "#FFFFFF", "line": "", "mono": False},
    "soft": {"bg": ACCENT_WASH, "fg": ACCENT_DK, "line": "#E1E7FF", "mono": False},
    # 键位提示：中性灰，像键盘帽，不像按钮
    "key": {"bg": "#F4F5F7", "fg": "#4B5563", "line": "#E1E4E8", "mono": False},
    # 本机已经装了的那一版
    "ok": {"bg": GREEN_BG, "fg": GREEN_DK, "line": "#CDEBD9", "mono": True},
}


class Chip(tk.Canvas):
    """标签芯片：版本号用 kind="ver"（等宽字），"★ 主要版本" 用 kind="primary"。"""

    H = 20

    def __init__(self, master, text: str = "", kind: str = "ver",
                 bg: str | None = None, **kw) -> None:
        self._text = text
        self._kind = kind
        self._pal = _CHIP.get(kind, _CHIP["ver"])
        self._owner_bg = bg or _parent_bg(master)
        self._font = tkfont.Font(
            family=(FONT_MONO if self._pal["mono"] else FONT_UI),
            size=9, weight=("normal" if self._pal["mono"] else "bold"),
        )
        w = int(self._font.measure(text) + 6 * 2 + 5)
        super().__init__(master, width=w, height=self.H, bg=self._owner_bg,
                         highlightthickness=0, bd=0, **kw)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = int(self.cget("width")), self.H
        line = self._pal["line"]
        if line:
            _round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, 5,
                        fill=self._pal["bg"], outline=line, width=1)
        else:
            _round_rect(self, 0, 0, w, h, 5, fill=self._pal["bg"], outline="", width=0)
        self.create_text(w / 2, h / 2 + 0.5, text=self._text, fill=self._pal["fg"],
                         font=self._font, anchor="center")

    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):           # 支持 widget.configure({"text": "x"}) 这种老写法
            kw = {**cnf, **kw}
        if "text" in kw:
            self._text = kw.pop("text")
            kw["width"] = int(self._font.measure(self._text) + 6 * 2 + 5)
        if "kind" in kw:
            self._kind = kw.pop("kind")
            self._pal = _CHIP.get(self._kind, _CHIP["ver"])
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
    """没有箭头、没有槽边框的一根圆角滑块。

    为什么不用 ttk.Scrollbar：Windows 主题下的滚动条带上下箭头和凹陷槽，
    和圆角按钮、圆角卡片完全不是一个时代的东西。整块界面里最显眼的"老气"就来自它。

    接口和 tk.Scrollbar 一致，换上去只改两行：
        sb = MiniScrollbar(parent, command=text.yview)
        text.configure(yscrollcommand=sb.set)

    内容没超出时不画滑块（不占视觉），悬停加深，支持拖动和点槽翻页。
    """

    def __init__(self, master, command=None, bg: str | None = None, width: int = 11, **kw) -> None:
        self._cmd = command
        self._first, self._last = 0.0, 1.0
        self._owner_bg = bg or _parent_bg(master)
        self._hover = False
        self._grab_off: float | None = None
        super().__init__(master, width=width, bg=self._owner_bg,
                         highlightthickness=0, bd=0, **kw)
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)

    # ── 对内嵌控件暴露的接口（同 tk.Scrollbar）──
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

    # ── 几何 ──
    def _track(self) -> tuple:
        h = max(1, int(self.winfo_height()))
        pad = 3
        return pad, max(1, h - pad * 2)

    def _thumb(self) -> tuple:
        pad, track = self._track()
        y0 = pad + track * self._first
        y1 = pad + track * self._last
        if y1 - y0 < 22:                    # 内容再长也留一个抓得住的滑块
            y1 = pad + track
            y0 = max(pad, y1 - 22)
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
        if self._last - self._first >= 0.999:
            return                          # 一屏装得下：不画
        y0, y1 = self._thumb()
        _round_rect(self, 3, y0, w - 3, y1, (w - 6) / 2,
                    fill=(MUTED if self._hover else GRAY_DOT), outline="", width=0)

    # ── 拖动 ──
    def _press(self, e) -> None:
        y0, y1 = self._thumb()
        if y0 <= e.y <= y1:
            self._grab_off = e.y - y0
        else:                               # 点槽：滑块中心跳到指针处，再继续拖
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
        y0, y1 = self._thumb()
        room = max(1.0, track - (y1 - y0))
        frac = (thumb_top - pad) / room
        try:
            self._cmd("moveto", max(0.0, min(1.0, frac)))
        except Exception:
            pass

    # ── 兼容 tk.Scrollbar 的配置写法：sb.configure(command=widget.yview) ──
    def configure(self, cnf=None, **kw):  # type: ignore[override]
        if isinstance(cnf, dict):           # 支持 widget.configure({"text": "x"}) 这种老写法
            kw = {**cnf, **kw}
        if "command" in kw:
            self._cmd = kw.pop("command")
        kw.pop("orient", None)      # 只有一个方向，传了也忽略
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

