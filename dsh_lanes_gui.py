#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 多版本启动器 —— 应用窗口（图形界面版）
==========================================
tkinter 实现（Python 自带，零第三方依赖）。它是 `dsh_lanes.py` 的**薄壳**：
所有真正的活儿（查版本 / 安装 / 启动 / 停止 / 握手自检）都调用已经验证过的核心函数，
界面只负责交互与实时日志。

用法：
    pyw dsh_lanes_gui.py          # 不弹控制台窗口
    py  dsh_lanes_gui.py          # 带控制台（排障用）
    py  dsh_lanes_gui.py --selfcheck   # 只做构建/刷新自检后退出（自动化用）
"""

from __future__ import annotations

import argparse
import ctypes
import io
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, simpledialog, ttk

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dsh_lanes as core  # noqa: E402
import ui_kit as ui  # noqa: E402  ← 自绘控件库（圆角按钮 / 胶囊 / 芯片）

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
LANE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

# ── 配色：全部指向「蓝色大肥鱼」令牌层（theme_whale）─────────────────────────
# 这里不再写死十六进制：换主题只走 apply_theme()，整窗跟着走。
# 旧值（#F4F5F7 / #6B7280 / #4D6BFE …）是通用灰调，次要文字对比度只有 2.96:1，
# 已低于 WCAG AA 正文的 4.5:1；新令牌的对应值是 5.51:1。
def _saved_theme() -> str:
    """上次选的主题（lanes.json → ui.theme），没选过就浅海。"""
    try:
        name = str((core.load_config().get("ui") or {}).get("theme") or "")
    except Exception:
        name = ""
    return "dark" if name.startswith("d") else "light"


def apply_theme(name: str) -> None:
    """切换主题 = 换这一层的全局色常量。

    Tk 没有样式层，这些常量在 import 时就落到各处的默认参数里了，所以改主题之后
    必须**重建窗口**（见 main() 的重启循环），不能"就地变色"。
    """
    global _T, BG, CARD, BORDER, TEXT, MUTED, ACCENT, ACCENT_DK, GREEN, GREEN_BG
    global GRAY, GRAY_BG, RED, RED_BG, AMBER, AMBER_BG
    ui.set_theme(name)
    _T = ui.theme()
    BG = _T.bg_base
    CARD = _T.layer1
    BORDER = _T.line1
    TEXT = _T.label1
    MUTED = _T.label2
    ACCENT = _T.brand
    ACCENT_DK = _T.brand_hover
    GREEN = _T.success
    GREEN_BG = _T.success_bg
    GRAY = _T.decor
    GRAY_BG = _T.layer2
    RED = _T.error_fg
    RED_BG = _T.error_bg
    AMBER = _T.warn_fg
    AMBER_BG = _T.warn_bg


apply_theme(_saved_theme())

# ── 版本号：窗口标题和状态条都会打出它 ──────────────────────────────────────
# 加这个的原因很实在：你会怀疑"我开的和你的不是同一版"，而界面上原来没有任何
# 版本标识 —— 只能靠背景色猜。以后打开先看右下角/标题栏这几个字，一眼对上。
APP_VERSION = "v0.3.10"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

# ── 版式常量（窗口四周留白 / 分栏缝宽 / 左列卡片留白）───────────────────────
# 整窗背景是一片海：头卡、状态卡两边各留 GUT，海就从缝里露出来；
# 缝隙给得比一般界面慷慨（卡片间距 12px），因为"浮在水上"的观感全靠这些缝 ——
# 缝太窄，海就只剩一圈描边，等于白铺。
GUT = 16
SASH_W = 12
LIST_PAD = (GUT, 10)
CARD_GAP = 6
SPLIT_DEFAULT = 0.57

# ── 日志行的级别样式 ────────────────────────────────────────────────────────
# 级别 → (前缀字形, 标记颜色, 正文颜色, 正文是否加粗)
# 字形只用 Consolas 确实收录的那几个（实测：✓ ✗ ▸ · 都在），不靠系统字体回退，
# 否则缺字形时会掉成方框，比不加还难看。
_LOG_STYLE = {
    "ok": ("✓", ui.GREEN, TEXT, False),
    "err": ("×", ui.RED, ui.RED, False),
    "warn": ("!", ui.AMBER, ui.AMBER, False),
    "head": ("▸", ui.ACCENT, ui.ACCENT_DK, True),
    "muted": ("·", ui.SUBTLE, MUTED, False),
    "": ("·", ui.SUBTLE, TEXT, False),
}
_LOG_MARKERS = (("[OK]", "ok"), ("[XX]", "err"), ("[!!]", "warn"))
_LOG_GLYPH_W = "✓ "        # 用来量标记列宽度（等宽字，所有字形同宽）


def enable_dpi_awareness() -> None:
    """让窗口在高 DPI 屏上不发虚（本机 2560×1440 / 133%）。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_dark_titlebar(root: tk.Tk, dark: bool) -> None:
    """让 Windows 的标题栏跟着主题走（深海下不再是那条浅灰带）。

    为什么非做不可：窗口内容换成深海之后，上面还压着一条 #F3F3F3 的系统标题栏，
    整个窗口像被横着切了一刀 —— 这是"两种模式割裂感"里最扎眼的一条。
    接口是 Win10 1809+ 的 DWMWA_USE_IMMERSIVE_DARK_MODE：1909 及以前是 19，
    2004 之后是 20，两个都发一遍（发错的那个会被系统忽略）。
    设完要 SWP_FRAMECHANGED 一次，否则已经画出来的标题栏不会重绘。
    纯外观问题：失败一律吞掉，不影响任何功能。
    """
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)
            )
        SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x20
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED
        )
    except Exception:
        pass


# RedrawWindow 的标志位（winuser.h）。抽成常量是为了能被测试读到。
# 只用 INVALIDATE，**绝不带 ERASE，也不带 UPDATENOW，也不再带 ALLCHILDREN**：
#   · ERASE = 先把那块区域擦成窗口类的底色，再让控件画内容。擦和画之间的那一帧
#     就是用户眼里的"闪"（v0.3.5 的元凶，v0.3.6 已去掉）。
#   · UPDATENOW = 在我们的滚动处理里当场同步画完。屏幕上一半是挪过的、一半是旧的，
#     一次滚动擦十几次，看着就是一格一格地抖。
#   · ALLCHILDREN（v0.3.9 试过去掉，结果滚动后卡片里的字会整片消失：子窗口一旦被
#     滚动作废，只标父窗口唤不回来；实测 heal()/tag_raise() 都救不了）。留着的代价
#     是每格滚轮 13~60ms，这条成本记在 CHANGELOG 里，以后再想别的办法。
_RDW_INVALIDATE, _RDW_ERASE, _RDW_ALLCHILDREN, _RDW_UPDATENOW = 0x0001, 0x0004, 0x0080, 0x0100
_REDRAW_FLAGS = _RDW_INVALIDATE | _RDW_ALLCHILDREN

_GWL_STYLE = -16
_WS_CLIPCHILDREN = 0x02000000


def enable_clip_children(widget) -> None:
    """给窗口加 WS_CLIPCHILDREN：让 ScrollWindowEx 不去动子窗口。

    为什么非有它不可（v0.3.9）：Tk 在 Windows 上滚动 canvas 用的是 ScrollWindowEx；
    **没有 WS_CLIPCHILDREN 的窗口**会把子窗口连着像素一起搬走、再擦掉 —— 卡片里的
    Label/Button 就是真子窗口，于是"滚一下就空白一片"，以前只能靠
    RedrawWindow(ALLCHILDREN) 把所有子窗口重画一遍来兜（每格 13~60ms）。
    加上这个样式之后，滚动只搬画布自己，Tk 再按新位置挪窗口项，子窗口内容原样保留。
    """
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
        hwnd = int(widget.winfo_id())
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), _GWL_STYLE)
        user32.SetWindowLongW(ctypes.c_void_p(hwnd), _GWL_STYLE, style | _WS_CLIPCHILDREN)
    except Exception:
        pass


def redraw_windows(*widgets) -> None:
    """把 Windows 上这些控件（含子控件）标记成"脏"，让它们重新画一遍。

    为什么非有它不可：左列是个 Canvas，lane 卡片是 canvas 上的「窗口项」——
    在 Windows 上那是一个**真的子窗口**。滚轮滚动时 Tk 用 ScrollWindowEx 搬像素，
    被搬到新位置的卡片子窗口不会因此重画，屏幕上就留成一片海面底色：
    白底和卡片里的文字/按钮全都不见了。用户看到的就是"滚一下，渲染丢了"。
    （X11 上不会：那边没有真子窗口，都是父窗口画的。）

    为什么只是"标记脏"、不当场画：当场画（RDW_UPDATENOW）会把一次滚动拆成十几次
    同步重画，每次还先擦一遍底色 —— 那就是"一闪一闪"。标记脏之后 Windows 自己
    会把这一轮的所有脏区域合并成一次 WM_PAINT，中间没有"擦完还没画"的那一帧。
    纯外观兜底，任何失败都吞掉，不影响功能。
    """
    if os.name != "nt":
        return
    try:
        user32 = ctypes.windll.user32
    except Exception:
        return
    flags = _REDRAW_FLAGS
    for widget in widgets:
        try:
            hwnd = int(widget.winfo_id())
        except (tk.TclError, AttributeError, TypeError):
            continue
        if not hwnd:
            continue
        try:
            user32.RedrawWindow(ctypes.c_void_p(hwnd), None, None, flags)
        except Exception:
            pass


class QueueWriter(io.TextIOBase):
    """把核心模块 print 出来的内容按行送进队列，供主线程写进日志面板。"""

    def __init__(self, q: queue.Queue) -> None:
        self.q = q
        self._buf = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(("line", line))
        return len(text)

    def flush(self) -> None:
        # 故意不把半行推出去：否则 `dsh web: <url>` 会被拆成两行显示
        return


def short_url(url: str, keep: int = 14) -> str:
    """卡片上只显示缩略 URL（完整地址点一下就复制），避免长 token 把布局挤变形。"""
    m = re.match(r"^(.*\?token=)(.+)$", url or "")
    if not m:
        return url or ""
    head, tok = m.group(1), m.group(2)
    if len(tok) <= keep + 8:
        return url
    return f"{head}{tok[:keep]}…{tok[-4:]}"


def _set_wrap(label: tk.Label, width: int, min_w: int) -> None:
    """把标签的 wraplength 跟着容器宽度调。

    先比较再改：无脑 configure(wraplength=...) 每次都会让标签重新排版，
    而标签变高又会触发父容器的 <Configure>，窄窗口下会来回抖。
    """
    try:
        want = max(min_w, int(width) - 6)
        if abs(int(label.cget("wraplength")) - want) > 10:
            label.configure(wraplength=want)
    except tk.TclError:
        pass


class LaneCard(ui.RoundedCard):
    """一条 lane 的卡片：名称 / 版本 / 端口 / 状态 + 操作。

    主要版本（primary）会被置顶，卡片加粗蓝边、标题旁挂 ★ 徽章，
    底部那行说明文字也换成主要版本的特别标注。

    v2：改成 RoundedCard（Canvas 画圆角底），内容挂到 self.inner。
    """

    def __init__(
        self,
        master,
        app: "LauncherApp",
        name: str,
        data: dict,
        runtime: dict | None,
        is_primary: bool = False,
        primary_name: str | None = None,
        primary_refs: dict | None = None,
    ) -> None:
        super().__init__(
            master,
            bg=CARD,
            border=(ACCENT if is_primary else BORDER),
            border_hover=(ACCENT if is_primary else ui.BORDER_HOVER),
            border_width=(2 if is_primary else 1),
            radius=10,
            padx=13,
            pady=10,
            # 左边一根状态色条：扫一眼就知道哪条在跑（没在跑就不画，绿色才代表"活着"）
            rail=(ui.GREEN if runtime else None),
        )
        self.app = app
        self.name = name
        self.data = data
        self.runtime = runtime
        self.is_primary = is_primary

        pad = tk.Frame(self.inner, bg=CARD)
        pad.pack(fill="x")

        # ── 第一行：主体信息 + 主操作
        # 先放右侧定宽部件（按钮、徽章），最后放会撑开的左侧块，
        # 否则长 URL 会把徽章挤没（这是 pack 的经典坑）。
        head = tk.Frame(pad, bg=CARD)
        head.pack(fill="x")

        btns = tk.Frame(head, bg=CARD)
        btns.pack(side="right")
        self.app.button(btns, "打开", lambda: self.app.open_lane(self.name),
                        primary=True, size="sm").pack(side="left", padx=3)
        self.app.button(btns, "停止", lambda: self.app.stop_lane(self.name),
                        danger=True, size="sm").pack(side="left", padx=3)
        self.app.button(btns, "日志", lambda: self.app.show_lane_log(self.name),
                        size="sm").pack(side="left", padx=3)

        badge = ui.Pill(
            head,
            "运行中" if runtime else "已停止",
            "on" if runtime else "off",
            bg=CARD,
        )
        badge.pack(side="right", padx=(0, 12))

        left = tk.Frame(head, bg=CARD)
        left.pack(side="left", fill="x", expand=True)

        title = tk.Frame(left, bg=CARD)
        title.pack(anchor="w")
        tk.Label(title, text=name, bg=CARD, fg=TEXT, font=(FONT_UI, 13, "bold")).pack(side="left")
        ui.Chip(title, str(data.get("version", "?")), "ver", bg=CARD).pack(side="left", padx=(7, 0))
        if is_primary:
            ui.Chip(title, "★ 主要版本", "primary", bg=CARD).pack(side="left", padx=(6, 0))

        meta = f"端口 {data.get('port', '-')}"
        if runtime:
            meta += f"   PID {runtime.get('pid')}"
        tk.Label(left, text=meta, bg=CARD, fg=MUTED, font=(FONT_UI, 9)).pack(anchor="w", pady=(3, 0))

        url = (runtime or {}).get("url") or ""
        if url:
            link = tk.Label(
                left,
                text=short_url(url),
                bg=CARD,
                fg=ACCENT,
                font=(FONT_MONO, 8),
                cursor="hand2",
                anchor="w",
                wraplength=330,
                justify="left",
            )
            link.pack(anchor="w", pady=(2, 0))
            link.bind("<Button-1>", lambda _e: self.app.copy_text(url))
            link.bind("<Enter>", lambda _e: link.configure(fg=ACCENT_DK, text=short_url(url) + "   ⧉ 点击复制"))
            link.bind("<Leave>", lambda _e: link.configure(fg=ACCENT, text=short_url(url)))

        # ── 第二行：次要操作（设为主要版本 / 删除这套 DSH）+ 主要版本说明
        tk.Frame(pad, bg=_T.line1, height=1).pack(fill="x", pady=(7, 5))
        foot = tk.Frame(pad, bg=CARD)
        foot.pack(fill="x")

        # 这条 lane 还缺 API key 吗？（主要版本自己不用比；接管的 lane 用真实 HOME，一般已有）
        my_refs = {}
        if not is_primary and primary_refs:
            try:
                my_refs = core.read_credential_refs(data.get("home") or "")
            except Exception:
                my_refs = {}
        missing = [] if is_primary else sorted(set(primary_refs or {}) - set(my_refs))

        # ── 说明文字单独占一行；操作链接另起一行右对齐
        # 原来这俩挤在同一行：链接一多（现在有 5~7 个），_rewrap 算出的可用宽度就会
        # 撞到 120px 下限，说明被压成竖排。分成两行后说明能用满宽度，链接也不再打架。
        if is_primary:
            note_text = (
                "主要版本＝你日常真正在用的那一套。新建版本会自动复制它的 API key"
                "（只复制密钥，会话与设置仍各自独立）；删除它需要手打名称二次确认。"
            )
            note_fg = ACCENT_DK
        elif missing:
            note_text = (
                f"还缺 API key（{primary_name} 有 {'、'.join(missing)}）—— "
                "不理它的话，这条 lane 启动时会让你重新填一次。"
            )
            note_fg = AMBER
        elif data.get("kind") == "clone":
            note_text = (
                f"这是「{data.get('clonedFrom') or '?'}」的副本：独立安装树 + 独立 HOME，"
                "升级它不碰原件。用「升级版本…」换版本，升完点「核对隔离」确认插件还都在。"
            )
            note_fg = ACCENT_DK
        else:
            note_text = "设为主要版本后，以后新建的版本会自动继承它的 API key。"
            note_fg = GRAY

        note = tk.Label(
            foot,
            text=note_text,
            bg=CARD,
            fg=note_fg,
            font=(FONT_UI, 8),
            justify="left",
            anchor="w",
            wraplength=420,
        )
        note.pack(fill="x")

        # 次要操作：圆角按钮比原来的文字链宽，一行塞不下 7 个 → 每行最多 5 个，多了自动换行。
        # 列表按**从左到右**的阅读顺序构建（身份类 → 内容类 → 删除），pack 时倒序贴右侧。
        acts_items = []
        if missing:
            acts_items.append(("补齐 API key", lambda: self.app.sync_key(self.name), AMBER))
        elif not is_primary:
            acts_items.append(
                ("★ 设为主要版本", lambda: self.app.set_primary_lane(self.name), ACCENT)
            )
        if data.get("kind") == "clone":
            acts_items.append(("核对隔离", lambda: self.app.verify_lane(self.name), ACCENT_DK))
        if not is_primary:
            acts_items.append(
                ("同步对话…", lambda: self.app.sync_chat_dialog(self.name), ACCENT_DK)
            )
        acts_items += [
            ("备份对话…", lambda: self.app.chat_dialog(self.name), ACCENT_DK),
            ("插件市场…", lambda: self.app.market_dialog(self.name), ACCENT_DK),
            ("升级版本…", lambda: self.app.upgrade_dialog(self.name), ACCENT_DK),
            ("复制…", lambda: self.app.clone_lane(self.name), ACCENT_DK),
            ("删除这套 DSH", lambda: self.app.delete_lane(self.name), RED),
        ]

        # 5 个以内单行；超过则每行 4 个（避免最后一行只剩一两个孤儿按钮）
        if len(acts_items) <= 5:
            groups = [acts_items]
        else:
            groups = [acts_items[i:i + 4] for i in range(0, len(acts_items), 4)]
        for chunk in groups:
            row_f = tk.Frame(foot, bg=CARD)
            row_f.pack(fill="x", pady=(3, 0))
            # 左对齐：说明和链接都从左边起头，右下的空白比左下的空白看着自然
            for text, cmd, color in chunk:
                self.app.link_button(row_f, text, cmd, fg=color).pack(side="left", padx=(0, 8))

        # 两行布局之后说明是满宽的，只需跟随 foot 宽度即可
        def _rewrap(event) -> None:
            try:
                w = max(200, event.width - 8)
                if abs(int(note.cget("wraplength")) - w) > 12:
                    note.configure(wraplength=w)
            except tk.TclError:
                pass

        foot.bind("<Configure>", _rewrap)

        # 双击卡片任意位置＝打开这条 lane
        def bind_open(widget) -> None:
            widget.bind("<Double-Button-1>", lambda _e: self.app.open_lane(self.name))
            for child in widget.winfo_children():
                # 按钮/胶囊/芯片上不要再挂双击打开（它们自己处理点击，双击会点两次）
                if not isinstance(child, (tk.Button, ui.RoundedButton, ui.Pill, ui.Chip)):
                    bind_open(child)

        bind_open(self)


class LauncherApp:
    _MAX_LOG_LINES = 4000

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self._switching = False   # 正在换主题（防止重复点）
        self.cfg = core.load_config()
        # 记住的分栏比例（0＝没存过，用 SPLIT_DEFAULT）。存在 lanes.json → ui.split
        self._ui = dict(self.cfg.get("ui") or {})
        try:
            self._split_frac = float(self._ui.get("split") or 0.0)
        except (TypeError, ValueError):
            self._split_frac = 0.0
        self._split_retry = 0
        self._sash_hot = False
        self._poll_id = None
        self._tick_id = None
        self._split_id = None
        self._list_bg = None          # 左列底图（一张海面切片）；不持引用会被回收
        self._list_bg_job = None
        self._list_size = (0, 0)
        self._heal_at = 0.0           # 上一次"卡片补画"写日志的时间（防刷屏）
        self._wheel_acc = 0           # 滚轮增量累积（高精度滚轮一格只报 30~40）
        self._redraw_job = None       # 滚动后标记重画的 idle 任务（见 redraw_windows）

        root.title(f"DSH 多版本启动器  ·  {APP_VERSION}")
        root.configure(bg=BG)
        g_ui = self.cfg.get("ui") or {}
        if g_ui.get("w") and g_ui.get("h"):
            root.geometry(f"{int(g_ui['w'])}x{int(g_ui['h'])}+{int(g_ui.get('x', 80))}+{int(g_ui.get('y', 60))}")
        else:
            self._center(1060, 720)
        root.minsize(880, 560)
        self._style()

        self._build_ui()                 # 海面垫底 → 头 → 身 → 脚（见 _build_ui）
        self._titlebar_tries = 0
        self._apply_titlebar()           # 标题栏配色要等窗口映射后才设得上
        self._split_id = self.root.after(90, self._apply_split)  # 分栏位置要等窗口量出真实宽度

        self._sig: tuple | None = None
        self._snap: dict | None = None      # 一轮刷新共用的快照（见 _snapshot）
        self._snap_at = 0.0
        self._remote_busy = False           # 顶部「npm 上：…」那行：是否查询中 / 上次查询时间
        self._remote_at = 0.0
        try:
            self.refresh_lanes(force=True)
        except Exception as exc:  # noqa: BLE001
            # 首次刷新失败也要把窗口开出来（能看日志、能手动重试），
            # 而不是留一个半成品窗口或者干脆消失。
            self.log(f"[XX] 首次刷新失败：{type(exc).__name__}: {exc}", "warn")
            self.log("     窗口仍然可用：修掉原因后按 F5 / 点「刷新」重试。", "muted")
            self.log(traceback.format_exc().rstrip(), "muted")
        self._poll_id = self.root.after(120, self._poll_queue)
        self.root.after(200, lambda: self.refresh_remote(force=True))  # 不挡开窗，查完再贴上去
        self._tick_id = self.root.after(2000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _e: self.manual_refresh())
        self.root.bind("<Control-l>", lambda _e: self.clear_log())
        self.root.bind("<Control-n>", lambda _e: self.new_version_dialog())
        self.log("欢迎使用 DSH 多版本启动器。左边是各版本 lane，右边是实时日志。", "head")
        self.log("快捷键：F5 刷新　Ctrl+L 清空日志　Ctrl+N 新建版本　双击卡片＝打开　点地址＝复制", "muted")
        self.log("标题下面那行的「√」＝本机已经装了这个版本；点「官方代码库 ↗」直接去 dsh 的仓库。", "muted")

        # 降权（Low 完整性 / 受限令牌）是最容易看错的一种失败：文件权限、icacls
        # 全都正常，可写文件、结束进程一律被拒，用户第一反应永远是"去改 D 盘权限"。
        # 2026-10-27 那次就是：窗口跑在 **Low 完整性 + 非受限令牌** 上，而 v0.3.9
        # 只判了 restricted → 这条提醒整段没出现，只剩「根目录不可写」和
        # 「结束不掉它，常见原因①受限沙箱②它是管理员启动的」两句越看越糊涂的话。
        # 所以这里把**实测到的东西**摊开：完整性级别 + 父进程链。
        tok = core.token_state()
        level = str(tok.get("integrity") or "?")
        self._crippled = bool(tok.get("restricted")) or level in ("Low", "Untrusted")
        if self._crippled:
            self.log(
                f"[!!] 这个窗口被降权了（完整性={level}"
                f"{'，令牌受限' if tok.get('restricted') else ''}）"
                "——不是 D 盘权限问题，别去改目录权限", "warn")
            chain = core.proc_parent_chain()
            if chain:
                self.log(f"     是谁把它拉起来的：{chain}", "warn")
            self.log(
                "     表现：写不了 lane 根目录、停不掉 lane 进程、打开 lane 也可能起不来。",
                "warn")
            self.log(
                "     解法：关掉这个窗口，用「资源管理器」双击 DSH多版本启动器-轻量.exe；"
                "从 DSH 终端 / 插件 / agent 里起的窗口，令牌一定是降权的。", "warn")

        # 根目录不可写时说清楚（不是不能用，而是"记不住 + 部分命令会失败"），
        # 别让它以"某个操作突然报错"的形式冒出来。
        # 探测放后台：失败时会重试 3 次、每次 sleep 150ms —— 那是 0.45 秒的同步等待，
        # 放在建窗路径上就是"打开慢半拍"，放在换主题路径上就是"切主题卡一下"。
        self._probe_root_async()

    # ── 基础 ────────────────────────────────────────────────────────────────
    def _build_ui(self, host=None) -> None:
        """整窗三块：海面垫底 → 头 → 身 → 脚（Tk 的叠放顺序＝创建顺序）。

        host 默认就是窗口本身；换主题时会把整窗建在一个临时 Frame 里
        （那个 Frame 先压在海水底下，建完再翻上来，见 _rebuild_ui）。
        """
        self._host = host if host is not None else self.root
        self._build_ocean_backdrop()
        self._chrome = [
            self._build_header(),
            self._build_body(),
            self._build_status(),
        ]

    def _probe_root_async(self) -> None:
        """在后台线程里探"根目录能不能写"，结果走日志队列回来。

        它是纯提示、不挡任何操作，所以没理由占着主线程 —— 而且探测失败时会重试
        3 次 × sleep 150ms = 0.45 秒，正好卡在建窗那一帧上。
        """
        cfg = self.cfg
        crippled = self._crippled        # 在起线程前定好，避免线程里读到的时序不确定

        def worker() -> None:
            try:
                ok, detail = core.lane_root_writable(cfg)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"{type(exc).__name__}: {exc}"
            if ok:
                return
            if crippled:
                # 已经在上面的降权提醒里解释过原因了，这里只报事实，别再给一遍误会的线索
                self.q.put(("line", "[!!] lane 根目录写不进去 —— 就是上面那个「降权」，不是 D 盘权限"))
                return
            self.q.put(("line", f"[!!] lane 根目录不可写：{detail}"))
            self.q.put((
                "line",
                "     卡片和打开/停止仍可用（状态现场探测）；但运行态记不住，"
                "克隆 / 删除 / 升级这些要写目录的命令会失败。",
            ))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_titlebar(self) -> None:
        """标题栏跟着主题走 —— 但只能在窗口**已经映射**之后设。

        窗口还没上屏时 GetParent(winfo_id()) 拿不到外框（返回 0），
        设下去是静默失败的：Win11 的标题栏仍然是那条浅灰带（这就是"深海窗口顶着
        一条白条"的原因）。所以这里没映射就过 60ms 再试，最多 10 次。
        """
        self._titlebar_job = None
        if not self.root.winfo_ismapped():
            if self._titlebar_tries < 10:
                self._titlebar_tries += 1
                self._titlebar_job = self.root.after(60, self._apply_titlebar)
            return
        set_dark_titlebar(self.root, ui.theme_name() == "dark")

    def _center(self, w: int, h: int) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TCombobox", padding=4)
        style.configure("TEntry", padding=4)
        style.configure("Vertical.TScrollbar", background=BORDER, troughcolor=BG, borderwidth=0)

    def button(self, master, text, command, primary=False, danger=False, width=None,
               size="md", icon="", variant=None):
        """圆角按钮工厂（返回 ui_kit.RoundedButton，不再是 tk.Button）。

        签名对旧调用**向后兼容**：primary / danger / width 照旧。新增三个可选参数：
          size    md(30px) / sm(26px) / xs(22px)
          icon    文字前的符号（⟳ ⧉ ■ ＋ 等），跟文字同一字体测宽
          variant 直接指定四变体（primary/secondary/ghost/danger），优先于 primary/danger

        注意：Canvas 的 width 是**像素**，而旧签名传的是"字符数"。这里按 1 字≈8px 折算，
        只是防止老调用点传参时按钮突然变窄。
        """
        kind = variant or ("primary" if primary else ("danger" if danger else "secondary"))
        px = int(width) * 8 + 16 if width else None
        try:
            owner_bg = master.cget("bg")
        except Exception:
            owner_bg = CARD
        return ui.RoundedButton(
            master, text, command, variant=kind, size=size,
            icon=icon, width=px, bg=owner_bg,
        )

    def link_button(self, master, text, command, fg=None, bg=None):
        """次要操作的"文字链"——统一成幽灵圆角按钮（无边框，hover 出浅灰底）。

        签名不变：fg 覆盖文字颜色（蓝 / 红 / 琥珀）；bg 一般不用传，自动取父容器底色。
        以前这里是 tk.Button + hover 下划线，和主按钮的圆角风格不统一。
        """
        try:
            owner_bg = bg or master.cget("bg")
        except Exception:
            owner_bg = CARD
        return ui.RoundedButton(
            master, text, command, variant="ghost", size="xs",
            fg=fg or TEXT, bg=owner_bg,
        )

    # ── 下载源选择（新建 / 升级共用） ─────────────────────────────────────────
    def registry_row(self, parent, cfg: dict, pady=(12, 0)):
        """「下载源」三选一 ＋「记住这个选择」。返回 (取值 StringVar, 记住 BooleanVar)。

        为什么把它摆到明面上（2026-09-28 实测）：官方源当天发新版时，镜像常常只同步了一半
        —— dsh@0.2.0-rc.1 镜像有了，可它的 12 个 @deepseek-ai/dsh-* 子包还没到（查 259 个包）。
        以前"查版本走官方、下载走镜像"两边不一致，就会 npm ETARGET 装不上。现在的规矩：
        选谁就**用它查版本、也用它下载**；万一它还没同步全，会自动换另一个源重试一次（写进日志）。
        """
        box = tk.Frame(parent, bg=BG)
        box.pack(fill="x", padx=18, pady=pady)
        var = tk.StringVar(value=core.registry_choice_of(cfg))
        remember = tk.BooleanVar(value=False)
        tk.Label(box, text="下载源", bg=BG, fg=MUTED, font=(FONT_UI, 9)).pack(anchor="w")
        row = tk.Frame(box, bg=BG)
        row.pack(anchor="w", pady=(2, 0))
        for value, text in (
            ("auto", "自动（先官方，缺东西再换镜像）"),
            ("official", "官方源 npmjs.org"),
            ("mirror", "镜像源 npmmirror.com（国内快）"),
        ):
            tk.Radiobutton(
                row, text=text, value=value, variable=var, bg=BG, fg=TEXT, selectcolor=CARD,
                activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
                highlightthickness=0, cursor="hand2",
            ).pack(side="left", padx=(0, 14))
        tk.Checkbutton(
            box, text="记住这个选择（写进 lanes.json，以后新建/升级默认用它）",
            variable=remember, bg=BG, fg=MUTED, selectcolor=CARD,
            activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
            highlightthickness=0, cursor="hand2",
        ).pack(anchor="w", pady=(2, 0))
        raw = str(cfg.get("registry") or "").strip()
        if raw.lower() not in ("", "auto", "official", "mirror", "npmjs", "npm", "npmmirror", "taobao"):
            tk.Label(box, text=f"（lanes.json 里现在写的是自定义地址 {raw}；不想换掉就别勾「记住」）",
                     bg=BG, fg=AMBER, font=(FONT_UI, 8)).pack(anchor="w")
        return var, remember

    def _save_registry_choice(self, var, remember) -> None:
        """勾了「记住」才写 lanes.json；不勾就只是这一次有效，不动你的全局设置。"""
        if not remember.get():
            return
        try:
            cfg = core.load_config()
            cfg["registry"] = core.registry_value_of(var.get())
            core.save_config(cfg)
            self.log(f"下载源已记住：{core.registry_choice_of(cfg)}"
                     + (f"（{cfg['registry']}）" if cfg["registry"] else "（auto＝先官方再镜像）"),
                     "muted")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 记住下载源失败：{exc}", "warn")

    def no_inherit_row(self, parent, hint_on: str, hint_off: str, pady=(10, 0)):
        """「不继承密钥和设置」勾选框 —— 新建版本 / 复制一套 DSH 两个对话框共用。

        口径和 CLI 的 `--no-inherit` 完全一致（走 core.NO_INHERIT_HOME_ITEMS）：
        不带 API key、匿名身份、DSH 设置、agent 预设；插件包、插件数据、对话、附件照旧。
        默认**不勾** —— 也就是保持老行为，不点它就跟以前一模一样。
        """
        var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            parent,
            text="不继承密钥和设置（不带 API key、匿名身份、DSH 设置、agent 预设）",
            variable=var, bg=BG, fg=TEXT, selectcolor=CARD,
            activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
            highlightthickness=0, cursor="hand2", anchor="w", justify="left",
        ).pack(anchor="w", padx=18, pady=pady)
        hint = tk.Label(parent, text=hint_off, bg=BG, fg=MUTED, font=(FONT_UI, 8),
                        wraplength=470, justify="left")
        hint.pack(anchor="w", padx=34)

        def on_toggle(*_a) -> None:
            try:
                hint.configure(text=hint_on if var.get() else hint_off)
            except tk.TclError:
                pass

        var.trace_add("write", on_toggle)
        on_toggle()
        return var, hint

    # ── 顶部 ────────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        self._remote_payload: dict | None = None   # 上一次查到的 npm 版本（窄窗口要按宽度重排）
        self._remote_job = None
        # 顶部工具栏：白底 + 一条下边框。原来整页都是灰底、按钮直接浮在上面，
        # 看不出"窗口骨架"；加这一条之后，头/身/脚三段才分得开。
        # 顶部工具栏：一块浮在海水上的白卡（上面和两边各留 GUT，露出来的就是海面）。
        # 原来是通栏白带 + 一条下边框；整窗铺海之后，通栏白带会把海拦腰截断。
        card = ui.RoundedCard(
            self._host, bg=CARD, border=BORDER, radius=14, padx=18, pady=12,
            shadow="s2", ocean=True,
        )
        card.pack(fill="x", padx=GUT, pady=(GUT, 0))
        head = card.inner

        # 右侧按钮**先** pack：窗口拉到最窄时宁可挤左边的文字，也不能把「＋ 新建版本」挤没
        right = tk.Frame(head, bg=CARD)
        right.pack(side="right")
        # 「官方代码库」放这一簇里：夹在 npm 那行会被挤掉（窄窗口下整行都会被裁）
        self.link_button(right, "官方代码库 ↗", self.open_repo, fg=ACCENT_DK).pack(
            side="left", padx=(0, 12)
        )
        self.button(right, "刷新", self.manual_refresh, variant="ghost").pack(
            side="left", padx=2
        )
        self.button(right, "环境自检", lambda: self.run_job("环境自检", lambda: core.cmd_doctor(self.cfg, None)),
                    variant="ghost").pack(side="left", padx=2)
        # 主题切换：标签写"将要去的那一套"，比写当前状态更好理解（点一下不会误以为关灯）
        self.button(right, ("浅海" if ui.theme_name() == "dark" else "深海"),
                    self.toggle_theme, variant="ghost", icon="◐").pack(side="left", padx=2)
        self.button(right, "全部停止", self.stop_all, danger=True).pack(side="left", padx=6)
        self.button(right, "打开根目录", self.open_root_dir).pack(side="left", padx=6)
        self.button(right, "＋ 新建版本", self.new_version_dialog, primary=True).pack(
            side="left", padx=(10, 0)
        )

        left = tk.Frame(head, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="DSH 多版本启动器", bg=CARD, fg=TEXT, font=(FONT_UI, 15, "bold")).pack(anchor="w")

        # 第二行：npm 上现在有哪些版本（只读查询）+ 官方代码库入口。
        # 版本号改成芯片排：原来是一串「latest 0.2.1-rc.2√　·　next 0.2.1-rc.3　·　…」，
        # 挤在一起要靠读才知道哪个是本机已有的；芯片能一眼分出"有"和"没有"。
        #
        # 原来夹在标题和这一行中间的「★ 主要版本常驻置顶 · 新建版本自动继承它的 API key ·
        # 双击卡片即可打开」撤掉了：这三条在卡片说明里、日志开头的欢迎语里都已经讲过，
        # 常驻在标题下面只是噪音，还挡住视线。
        remote = tk.Frame(left, bg=CARD)
        remote.pack(anchor="w", pady=(6, 0))
        self.remote_box = tk.Frame(remote, bg=CARD)     # 内容每次查询完重建
        self.remote_box.pack(side="left")
        self._remote_note("正在查询 npm 上的版本…", ui.SUBTLE)
        self._header_left = left
        left.bind("<Configure>", self._on_header_resize)
        return card

    # ── 主体 ────────────────────────────────────────────────────────────────
    def _build_body(self) -> None:
        """左列表 / 右日志 —— 用 PanedWindow 分栏，中间那条缝能拖（比例会记住）。

        整窗背景是一片海（见 _build_ocean_backdrop）：
          · 左列：滚动区里垫一张海面切片，lane 卡片浮在海上，卡片缝里看得见海；
          · 右列：一块白卡，工具栏也放进卡片内部 —— 右栏就不会剩一条纯色带；
          · 中间：SASH_W 像素的缝，海从缝里透出来，鼠标移上去会亮成品牌蓝。
        """
        self.split = tk.PanedWindow(
            self._host, orient="horizontal", bg=BG, bd=0, relief="flat",
            sashwidth=SASH_W, sashrelief="flat", sashcursor="sb_h_double_arrow",
            opaqueresize=True, showhandle=False,
        )
        self.split.pack(fill="both", expand=True, pady=(10, 0))
        self.split.bind("<Motion>", self._on_split_motion)
        self.split.bind("<Leave>", lambda _e: self._set_sash_hot(False))
        self.split.bind("<ButtonRelease-1>", self._on_split_release)

        # ── 左：lane 列表（可滚动）
        left_wrap = tk.Frame(self.split, bg=BG)
        left_wrap.rowconfigure(0, weight=1)
        left_wrap.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(left_wrap, bg=BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb = ui.MiniScrollbar(left_wrap, command=self._scroll_cmd, bg=BG, ocean=True)
        sb.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        self._list_sb = sb
        self._align_job = None
        self._redraw_job = None
        self.canvas.configure(yscrollcommand=self._on_list_scroll)
        # 关键（v0.3.9）：让滚动只搬画布自己，别把卡片这些真子窗口一起搬走擦掉
        enable_clip_children(self.canvas)

        # 列表底：Label 装一张海面切片。
        # 为什么不用 Frame：Tk 的容器全都不透明，Frame 会整片盖住背后的海；
        # Label 的 image 画在**子控件下面**（实测），所以卡片之间的缝里看见的是真海面。
        self.list_frame = tk.Label(self.canvas, bd=0, highlightthickness=0, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", self._on_list_configure)
        self.canvas.bind("<Configure>", lambda e: self._layout_list(e.width, e.height))
        self._wheel_acc = 0
        self._list_size = (0, 0)      # 新画布＝还没量过，底图必须重画一次
        self._layout_job = None       # 布局落定后再量一次滚动区高度
        self._bind_wheel(self.canvas)
        self._bind_wheel(self.list_frame)
        self.split.add(left_wrap, minsize=420, stretch="always")

        # ── 右：日志 —— 海面垫底 + 一整块白卡（标题栏/清空/复制也在卡片里）
        right = ui.OceanBackdrop(self.split, crop=True)
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        self.split.add(right, minsize=330, stretch="always")

        panel = ui.RoundedCard(
            right, bg=CARD, border=BORDER, radius=12,
            padx=10, pady=4, fill=True, width=1, height=1, shadow="s2", ocean=True,
        )
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, GUT), pady=(0, GUT - 2))
        wrap = panel.inner
        wrap.rowconfigure(2, weight=1)
        wrap.columnconfigure(0, weight=1)

        bar = tk.Frame(wrap, bg=CARD)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        tk.Label(bar, text="实时日志", bg=CARD, fg=TEXT, font=(FONT_UI, 11, "bold")).pack(side="left")
        self.log_count = tk.Label(bar, text="", bg=CARD, fg=ui.SUBTLE, font=(FONT_UI, 9))
        self.log_count.pack(side="left", padx=(8, 0))
        self.button(bar, "清空", self.clear_log, variant="ghost", size="sm").pack(side="right")
        self.button(bar, "复制全部", self.copy_log, variant="ghost", size="sm").pack(
            side="right", padx=4
        )
        tk.Frame(wrap, bg=_T.line1, height=1).grid(row=1, column=0, columnspan=2, sticky="ew")

        # 日志排版：时间戳小一号、级别字形一列、正文 10pt；折行用 lmargin2 缩进到正文起点。
        self._f_log = tkfont.Font(family=FONT_MONO, size=10)
        self._f_ts = tkfont.Font(family=FONT_MONO, size=9)
        self._f_log_b = tkfont.Font(family=FONT_MONO, size=10, weight="bold")
        self._log_indent = self._f_ts.measure("00:00:00 ") + self._f_log.measure(_LOG_GLYPH_W)

        self.log_text = tk.Text(
            wrap,
            width=1,  # 关键：默认 80 字符会撑爆右列、把左列压扁（grid 先满足请求宽度再分权重）
            height=1,
            # 日志区做成**凹进去的水槽**：整块卡片全白的话，右栏就是一大片白纸，
            # 和左边的海水/白卡片是两种材质（这就是"割裂"）。用水色垫底之后，
            # 它读起来像卡片上开了一扇看水的窗，两种材质是同一套。
            bg=_T.well,
            fg=TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            # 中文日志里整段没有空格，用 word 换行时 Tk 会把整段当成一个"词"，
            # 前面放不下就整段挪到下一行 —— 结果行首只剩个时间戳和字形。
            # 按字符换行才是中文的正确行为（浏览器对 CJK 也是按字断行）。
            wrap="char",
            font=self._f_log,
            padx=10,
            pady=8,
            spacing1=1,
            spacing3=1,
            insertbackground=TEXT,
            state="disabled",
        )
        self.log_text.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        self.log_sb = ui.MiniScrollbar(wrap, command=self.log_text.yview, bg=_T.well)
        self.log_sb.grid(row=2, column=1, sticky="ns", padx=(0, 4), pady=(6, 0))
        self.log_text.configure(yscrollcommand=self.log_sb.set)

        T = self.log_text
        T.tag_configure("ts", foreground=ui.SUBTLE, font=self._f_ts)
        T.tag_configure("pad", font=self._f_log)          # 只有对齐用的空格，不上色
        for lvl, (_glyph, mk_fg, msg_fg, bold) in _LOG_STYLE.items():
            T.tag_configure(f"mk_{lvl}", foreground=mk_fg, font=self._f_log_b)
            T.tag_configure(
                f"m_{lvl}",
                foreground=msg_fg,
                font=(self._f_log_b if bold else self._f_log),
                lmargin2=self._log_indent,
                spacing1=(9 if lvl == "head" else 2),   # 小节标题上方留白，日志才分得段
                spacing3=1,
            )

        # 空状态：日志一条都没有时，别让面板塌成一张白纸
        self._log_hint = tk.Frame(wrap, bg=_T.well)
        tk.Label(self._log_hint, text="还没有日志", bg=_T.well, fg=TEXT,
                 font=(FONT_UI, 11, "bold")).pack()
        tk.Label(
            self._log_hint,
            text="打开任意版本、刷新，或做一次操作，\n输出会实时出现在这里。",
            bg=_T.well, fg=MUTED, font=(FONT_UI, 9), justify="center",
        ).pack(pady=(5, 11))
        keys = tk.Frame(self._log_hint, bg=_T.well)
        keys.pack()
        for key_text in ("F5 刷新", "Ctrl+L 清空", "Ctrl+N 新建"):
            ui.Chip(keys, key_text, "key", bg=_T.well).pack(side="left", padx=3)
        self._show_log_hint()

        # 自动滚动：手动往上翻就停住，右下角浮出「回到最新」，而不是每来一行把你拽回底部
        self._autoscroll = True
        self._back_btn = self.button(
            wrap, "↓ 回到最新", self._scroll_to_bottom, variant="primary", size="xs"
        )
        T.bind("<MouseWheel>", self._on_log_wheel, add="+")
        for seq in ("<ButtonRelease-1>", "<B1-Motion>", "<KeyRelease>"):
            T.bind(seq, lambda _e: self.root.after(20, self._sync_autoscroll), add="+")
        return self.split

    def _build_status(self) -> None:
        # 底部状态条：也是一块浮在海水上的白卡，收住整个窗口
        card = ui.RoundedCard(self._host, bg=CARD, border=BORDER, radius=12,
                              padx=16, pady=7, ocean=True)
        card.pack(fill="x", padx=GUT, pady=(8, GUT))
        bar = card.inner
        self.status = tk.Label(bar, text="就绪", bg=CARD, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        self.status.pack(side="left")
        # 版本号常驻左下角：以后"我开的是不是最新"不用再靠背景色猜
        self.ver_label = tk.Label(bar, text=APP_VERSION, bg=CARD, fg=ui.SUBTLE,
                                  font=(FONT_UI, 8))
        self.ver_label.pack(side="left", padx=(10, 0))
        self.busy_label = tk.Label(bar, text="", bg=CARD, fg=ACCENT, font=(FONT_UI, 9, "bold"))
        self.busy_label.pack(side="right")
        self.counts = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=(FONT_UI, 9))
        self.counts.pack(side="right", padx=14)
        return card

    # ── 整窗海面 / 可拖拽分栏 ────────────────────────────────────────────────
    def _build_ocean_backdrop(self) -> None:
        """把整窗铺成一片海：先 place 一张海面画布，后面建的控件都盖在它上面。

        为什么用 place 而不是 pack：pack 会把窗口切成一条条，它只会分到"剩下的
        那条缝"；place(relwidth=1, relheight=1) 才真正铺满整窗。Tk 的叠放顺序＝
        创建顺序，所以它必须**最先**建 —— 后面 build 的头部/主体/状态都压在它上面，
        没被控件盖住的地方（窗口四边、分栏缝、卡片之间）露出来的就是海面。
        """
        self._ocean = ui.OceanBackdrop(self._host)
        self._ocean.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        # 注意：不要在这里调 self._ocean.lower() —— Canvas 上的 lower/raise 是「图形项
        # 层级」，要 tagOrId，会直接抛 wrong # args。叠放顺序＝创建顺序，先建就够低了。
        return self._ocean

    def _on_list_scroll(self, first, last) -> None:
        """滚动位置变了：更新滚动条，并给"垫了海面切片"的卡片重新对位。

        为什么单说这类卡片：它们的底图是从整窗那片海里按自己的屏幕位置裁的，
        一滚就错位（圆角外面那圈海会跟周围差一档颜色）。滚动**不会**给卡片发
        <Configure>，所以没法靠事件触发，只能在这里跟着滚动命令刷一次（90ms 去抖，
        一次滚动只重画一次，不是每格一次）。
        """
        try:
            self._list_sb.set(first, last)
        except (tk.TclError, AttributeError):
            pass
        if self._align_job is not None:
            try:
                self.root.after_cancel(self._align_job)
            except Exception:
                pass
        self._align_job = self.root.after(90, self._align_ocean_cards)
        # Windows 上「滚完卡片变空白」的补丁：把左列标成脏（见 redraw_windows）
        self._schedule_list_redraw()

    def _schedule_list_redraw(self) -> None:
        """把左列标成脏。**立刻标，不排队。**

        这个名字留着只是因为调用点都在这份代码里，实际已经"不排队"了 ——
        v0.3.6 是 `after(40, ...)` 延迟标脏，而每次来新的滚动/改尺寸事件都会
        `after_cancel` 掉上一次再重排。问题就出在这：拖分栏、拖窗口边的时候事件
        每 16~45ms 来一次，**永远比 40ms 早**，于是那个任务被无限次取消、一次也
        没跑过 —— 左列在整个拖动过程中都是空的，鼠标一停才"啪"一下长回来。
        这就是「一变就一闪一闪」的真正来源，跟擦不擦底色无关。

        标脏本身只有几微秒（不擦、不画，只是告诉 Windows 这块要重画），
        一秒标 60 次也毫无成本：Windows 会把它们合并成一次 WM_PAINT。
        所以正确的做法是"每次事件都标"，而不是"等一会儿再标"。
        """
        self._redraw_job = None          # 不再排队，保留属性只是为了 _stop_jobs 兼容
        self._repaint_list_now()

    def _align_ocean_cards(self) -> None:
        """滚动后给"垫了海面切片"的卡片重新对位。

        v0.3.9 起只处理**真的垫了切片**的卡片（`_ocean_refs` 非空）：平色主题里
        `ocean_slice()` 恒为 None，每张卡都没有切片可对位，以前照样把每张卡
        `heal()` 一遍（=整块重画），滚动停下后白闪一下。
        """
        self._align_job = None
        try:
            kids = list(self.list_frame.winfo_children())
        except tk.TclError:
            return
        healed = 0
        for child in kids:
            if not (isinstance(child, ui.RoundedCard) and child._ocean and child._ocean_refs):
                continue
            try:
                # 同 _heal_cards：滚动后 ismapped 不可信，按尺寸判断
                if child.winfo_width() >= 4 and child.winfo_height() >= 4:
                    child.heal()
                    healed += 1
            except tk.TclError:
                continue
        # 对位等于"换了一张底图"，换完同样要把子窗口标脏才看得见
        if healed:
            self._repaint_list_now()

    def _on_list_configure(self, e) -> None:
        """左列内容变高/变宽了才重算布局。

        为什么要先比一下尺寸：**滚动 canvas 本身就会给里面的控件发 <Configure>**
        （只是 x/y 变了）。以前这里无条件跑 _layout_list()，于是"滚一格轮子" =
        一次重排 + 可能一次整块海面底图重绘，滚起来就一顿一顿的。
        """
        if (int(e.width), int(e.height)) == self._list_size:
            return
        self._layout_list()

    def _content_height(self) -> int:
        """左列内容真正有多高。

        不能用 list_frame.winfo_reqheight()：那块底图是画在 Label 上的，
        设图之后 Label 的 reqheight 就等于**上一张图的高度**，拿它算滚动区
        等于让底图自己往上反馈 —— 列表只会越长越缩不回来。
        这里两条路取大：pack 出来的实际底部、卡片自身要求的和（布局还没跑时用得上）。
        """
        real = 0
        asked = 0
        try:
            kids = list(self.list_frame.winfo_children())
        except tk.TclError:
            return 0
        for child in kids:
            try:
                if child.winfo_ismapped():
                    real = max(real, int(child.winfo_y()) + int(child.winfo_height()))
                # 要的是"每张卡自己要求多高"。只看实际摆出来的高度不行：列表装不下时
                # pack 会把最后一张压扁，于是滚区永远刚好差那一截 —— 底下几行字被裁掉，
                # 还滚不到（这是"内容到底了、却少一块"的根因）。
                asked += max(int(child.winfo_reqheight()), int(child.winfo_height())) + CARD_GAP * 2
            except tk.TclError:
                continue
        return max(real, asked)

    def _layout_list(self, w: int | None = None, h: int | None = None) -> None:
        """左列布局：内容比视口短时把列表撑满视口（否则底下会露一条纯色），
        同时决定底图要多大，变了才重画（拖动窗口时不会每像素重算一张图）。
        """
        try:
            cw = int(w if w is not None else self.canvas.winfo_width())
            ch = int(h if h is not None else self.canvas.winfo_height())
        except tk.TclError:
            return
        need = max(1, self._content_height())
        ch2 = max(need, ch)
        try:
            self.canvas.itemconfigure(self._win, width=max(1, cw), height=ch2)
            self.canvas.configure(scrollregion=(0, 0, cw, ch2))
        except tk.TclError:
            return
        if (cw, ch2) != self._list_size:
            self._list_size = (cw, ch2)
            self._paint_list_bg()
            # 改窗口项尺寸＝在里面挪动/缩放卡片，Windows 上同样会留下没重画的空白，
            # 所以这里也要补一次强制重画（不只是滚动时需要）。
            self._schedule_list_redraw()

    def _paint_list_bg(self) -> None:
        if self._list_bg_job is not None:
            try:
                self.root.after_cancel(self._list_bg_job)
            except Exception:
                pass
        self._list_bg_job = self.root.after(120, self._do_paint_list_bg)

    def _do_paint_list_bg(self) -> None:
        self._list_bg_job = None
        try:
            w, h = int(self.list_frame.winfo_width()), int(self.list_frame.winfo_height())
        except tk.TclError:
            return
        photo = ui.ocean_slice(self.list_frame, w, h)
        if photo is None:
            return
        self._list_bg = photo
        try:
            self.list_frame.configure(image=photo)
        except tk.TclError:
            pass

    def _on_split_motion(self, e) -> None:
        """鼠标靠近分栏缝时把它点亮 —— 光一条 9px 的缝，不亮起来没人知道能拖。"""
        try:
            self._set_sash_hot(abs(e.x - int(self.split.sash_coord(0)[0])) <= 7)
        except (tk.TclError, IndexError):
            pass

    def _set_sash_hot(self, hot: bool) -> None:
        if hot == self._sash_hot:
            return
        self._sash_hot = hot
        # 亮成品牌蓝，而不是"淡一点的蓝"：缝本来就窄，淡色在整窗海面上根本看不见
        try:
            self.split.configure(bg=(ui.ACCENT if hot else BG))
        except tk.TclError:
            pass

    def _split_frac_now(self) -> float:
        try:
            w = int(self.split.winfo_width())
            x = int(self.split.sash_coord(0)[0])
        except (tk.TclError, AttributeError, IndexError, ValueError):
            return 0.0
        if w < 200:
            return 0.0
        return max(0.05, min(0.95, x / float(w)))

    def _apply_split(self) -> None:
        """把记住的比例还原成分栏位置；窗口还没量出宽度就过一会儿再试（最多 25 次）。"""
        self._split_id = None
        try:
            w = int(self.split.winfo_width())
        except (tk.TclError, AttributeError):
            return
        if w < 200:
            if self._split_retry < 25:
                self._split_retry += 1
                self._split_id = self.root.after(120, self._apply_split)
            return
        frac = self._split_frac if 0.15 <= self._split_frac <= 0.85 else SPLIT_DEFAULT
        x = max(420, min(int(w * frac), w - 330 - SASH_W))
        try:
            self.split.sash_place(0, x, 0)
        except tk.TclError:
            pass

    def _on_split_release(self, _e=None) -> None:
        """拖完就存：别等关窗口，万一被强杀也不丢比例。

        顺手把"缝亮成品牌蓝"复位：以前只有 <Leave> 才复位，而松手时指针
        正好还停在缝上 —— 于是那条 12px 的蓝就一直亮着，看起来像被"选中"了。
        """
        self._set_sash_hot(False)
        frac = self._split_frac_now()
        if not frac:
            return
        self._split_frac = frac
        try:
            cfg = core.load_config()
            u = dict(cfg.get("ui") or {})
            u["split"] = round(frac, 4)
            cfg["ui"] = u
            core.save_config(cfg)
        except Exception:
            pass

    def _bind_wheel(self, widget) -> None:
        """给控件（连同现在已有的子控件）挂滚轮。卡片是随刷新重建的，所以每次
        建完卡片要再挂一次 —— 旧的一起销毁了，不会越挂越多。
        """
        try:
            widget.bind("<MouseWheel>", self._on_wheel, add="+")
            for child in widget.winfo_children():
                self._bind_wheel(child)
        except tk.TclError:
            pass

    def _on_wheel(self, event) -> str:
        """滚左列。两种改法都是有原因的：

        ① 不再 bind_all + winfo_containing。全局绑定要靠"指针下面那一刻是哪个
           控件"反推是列表还是日志，指针压在按钮/胶囊/浮层上时就会判断错、甚至
           直接丢掉这次滚轮；它还会在日志面板上重复触发一次。
        ② 认 delta 的累积值。高精度滚轮和触控板一格只报 30~40，以前
           int(-delta/120) 直接算成 0 —— 表现为"滚了没反应"。
        """
        delta = int(getattr(event, "delta", 0) or 0)
        if not delta:
            return "break"
        self._wheel_acc += delta
        steps = int(self._wheel_acc / 120.0)
        if not steps:
            return "break"
        self._wheel_acc -= steps * 120
        try:
            top, bottom = self.canvas.yview()
            if (steps > 0 and top <= 0.0) or (steps < 0 and bottom >= 1.0):
                self._wheel_acc = 0          # 到底/到顶，别攒着等会儿一下子跳
                return "break"
            self.canvas.yview_scroll(-steps, "units")
            # 标脏不在这里做：yview_scroll 会触发 yscrollcommand → _on_list_scroll，
            # 那边统一标一次。两边都标就是每格滚轮标两遍（v0.3.9 实测 12 次/6 格）。
        except tk.TclError:
            pass
        return "break"

    def _scroll_cmd(self, *args) -> None:
        """给滚动条用的 yview：标脏交给 _on_list_scroll（yscrollcommand 会回调它）。"""
        try:
            self.canvas.yview(*args)
        except tk.TclError:
            return

    def _repaint_list_now(self) -> None:
        """把左列标记成脏，Windows 会合并成一次重画。

        为什么必须带 ALLCHILDREN：卡片里几十个 Label/Button 是真子窗口，滚动作废
        它们之后，只标父窗口唤不回来（v0.3.9 试过 heal()/tag_raise() 都救不了，
        实测滚完卡片整片空白）。代价是每格滚轮 11~36ms。

        为什么不再 update_idletasks()：那是 v0.3.5"当场同步画"的必需品 ——
        Tk 把窗口项挪位置排在 idle 里，不先推完，同步补画补的是挪之前的位置。
        现在不急着自己画了：等真正的 WM_PAINT 到来时，Tk 早就把窗口挪好了，
        顺带还把这一轮十几次滚动合并成一次画（这才是"不闪"的关键）。
        """
        try:
            redraw_windows(self.list_frame)
        except tk.TclError:
            pass

    # ── 日志 ────────────────────────────────────────────────────────────────
    def _tag_for(self, text: str, tag: str | None = None) -> str:
        if tag:
            return tag
        for marker, lvl in _LOG_MARKERS:
            if text.startswith(marker):
                return lvl
        return ""

    def _split_line(self, line: str, tag: str | None) -> tuple:
        """把一行拆成 (级别, 正文)。

        `[OK]` / `[XX]` / `[!!]` 前缀换成对应颜色的一个字形，正文保持中性色——
        整行染绿会让日志变得花里胡哨，真正该注意的红色反而被淹没。
        小节标题（`──── 打开 lane：global ────`）去掉装饰线本身，靠颜色和上方留白分区。
        """
        for marker, lvl in _LOG_MARKERS:
            if line.startswith(marker):
                line = line[len(marker):].lstrip()
                tag = tag or lvl
                break
        lvl = tag if tag in _LOG_STYLE else ""
        if lvl == "head":
            line = line.strip().strip("─").strip()
        else:
            # 正文里那些"     └ 补充说明"式的悬挂缩进要丢掉：
            # 和左边的痕迹列拼在一起时，行首那几个空格会变成唯一的换行点，
            # 结果就是"23:28:20 !"孤零零一行、正文掉到下一行去。
            line = line.lstrip()
        return lvl, line

    def _log_lines(self, items: list[tuple[str, str | None]]) -> None:
        """批量写日志：一次插入、只滚动一次、并裁掉过老的尾巴。

        安装 npm 时一秒能来上百行，逐行 insert + 逐行 see("end") 会把界面拖住；
        而且文本控件只增不减，跑几次安装后连滚动都会变卡。

        v3 排版：每行 = 时间戳 + 级别字形 + 正文，三块各占一列（等宽字，天然对齐）。
        一条消息自带多行时只有首行带时间戳，续行留白——否则同一秒里几十行
        都顶着同一个时刻，反而更乱。
        """
        if not items:
            return
        text = self.log_text
        stamp = time.strftime("%H:%M:%S")
        text.configure(state="normal")
        for raw, tag in items:
            block = ANSI_RE.sub("", str(raw)).split("\n")
            lvl, body = self._split_line(block[0], tag)
            text.insert("end", stamp + " ", "ts")
            text.insert("end", _LOG_STYLE[lvl][0] + " ", f"mk_{lvl}")
            text.insert("end", body + "\n", f"m_{lvl}")
            for line in block[1:]:      # 续行：空出前两列，不再重复时间戳和字形
                text.insert("end", " " * 9, "ts")
                text.insert("end", "  ", "pad")
                text.insert("end", line + "\n", f"m_{lvl}")

        total = 0
        try:  # 只留尾部若干行
            total = int(str(text.index("end-1c")).split(".")[0])
            if total > self._MAX_LOG_LINES:
                text.delete("1.0", f"{total - self._MAX_LOG_LINES}.0")
                total = self._MAX_LOG_LINES
        except (tk.TclError, ValueError):
            pass
        if self._autoscroll:
            text.see("end")
        text.configure(state="disabled")
        self._hide_log_hint()
        self.log_count.configure(text=(f"{max(0, total - 1)} 行" if total > 1 else ""))

    def log(self, text: str, tag: str | None = None) -> None:
        self._log_lines([(text, tag)])

    # ── 日志面板的空状态 / 自动滚动 ──
    def _show_log_hint(self) -> None:
        try:
            self._log_hint.place(relx=0.5, rely=0.44, anchor="center")
        except (tk.TclError, AttributeError):
            pass

    def _hide_log_hint(self) -> None:
        try:
            self._log_hint.place_forget()
        except (tk.TclError, AttributeError):
            pass

    def _on_log_wheel(self, event) -> str:
        """滚轮翻日志：先滚动，再判断是否已经离开底部（所以延后一点再同步状态）。"""
        try:
            self.log_text.yview_scroll(-1 if event.delta > 0 else 1, "units")
        except tk.TclError:
            pass
        self.root.after(15, self._sync_autoscroll)
        return "break"

    def _sync_autoscroll(self) -> None:
        try:
            at_bottom = self.log_text.yview()[1] >= 0.999
        except tk.TclError:
            return
        self._autoscroll = at_bottom
        if at_bottom:
            self._back_btn.place_forget()
        else:                       # 停在半路：右下角浮出「回到最新」，不抢滚动
            self._back_btn.place(relx=1.0, rely=1.0, x=-28, y=-14, anchor="se")

    def _scroll_to_bottom(self) -> None:
        self.log_text.see("end")
        self._autoscroll = True
        self._back_btn.place_forget()

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._autoscroll = True
        self._back_btn.place_forget()
        self.log_count.configure(text="")
        self._show_log_hint()

    def copy_log(self) -> None:
        self.copy_text(self.log_text.get("1.0", "end").strip())

    def copy_text(self, text: str) -> None:
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("已复制到剪贴板")

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # ── 数据刷新 ────────────────────────────────────────────────────────────
    def _snapshot(self, force: bool = False, ttl: float = 1.0) -> dict:
        """一轮刷新要用到的全部"外部事实"，只算一次。

        以前算签名时算一遍、建卡片时再算一遍、更新计数时又算一遍——同一件事算三遍，
        每次都是真的去读 TCP 表 / 探端口。现在一次算完，后面全部复用。
        """
        now = time.monotonic()
        if not force and self._snap is not None and now - self._snap_at < ttl:
            return self._snap

        cfg = core.load_config()
        # 台账自愈：接管型 lane（npm 全局那份）是你自己在升级的，登记版本要跟上事实
        version_fixes = core.sync_lane_versions(cfg)
        states = core.lane_states(cfg)          # 复用同一份 TCP 表，不再每条 lane 各探一次
        try:
            foreign = core.discover_instances(cfg)
        except Exception:
            foreign = []
        try:
            installs = core.find_installs(cfg)
        except Exception:
            installs = []

        signature = (
            str(cfg.get("primary") or ""),
            tuple(
                (
                    name,
                    data.get("version"),
                    data.get("port"),
                    (states.get(name) or {}).get("pid"),
                    (states.get(name) or {}).get("url"),
                )
                for name, data in cfg.get("lanes", {}).items()
                if isinstance(data, dict)
            ),
            tuple((i["port"], i["pid"]) for i in foreign),
        )
        self._snap = {
            "cfg": cfg,
            "states": states,
            "foreign": foreign,
            "installs": installs,
            "signature": signature,
            "version_fixes": version_fixes,
        }
        self._snap_at = now
        return self._snap

    def _invalidate(self) -> None:
        """有东西被改动过（起停 / 删除 / 接管）→ 下一轮强制重算。"""
        self._snap = None
        core.invalidate_tcp_cache()

    def _update_counts(self, snap: dict) -> None:
        cfg = snap["cfg"]
        lanes = [d for d in cfg.get("lanes", {}).values() if isinstance(d, dict)]
        running = sum(1 for v in snap["states"].values() if v)
        top = core.primary_lane(cfg)
        self.counts.configure(
            text=f"lane {len(lanes)} 条（运行 {running}）｜主要版本 "
            f"{top or '未设置'}｜未登记实例 {len(snap['foreign'])} 个｜根目录 {cfg['root']}"
        )

    def refresh_lanes(self, force: bool = True) -> None:
        snap = self._snapshot(force=force)
        if not force and snap["signature"] == self._sig:
            return
        self._sig = snap["signature"]
        self.cfg = snap["cfg"]
        for fix in snap.get("version_fixes") or []:
            self.log(f"台账已更正：lane「{fix['lane']}」登记的是 {fix['was'] or '?'}，"
                     f"安装树里实际是 {fix['now']}（接管型 lane 是你自己在升级，启动器只读不改）",
                     "ok" if fix["now"] else "muted")
        for child in self.list_frame.winfo_children():
            child.destroy()

        lanes = self.cfg.get("lanes", {})
        top = core.primary_lane(self.cfg)
        if not lanes:
            card = ui.RoundedCard(self.list_frame, bg=CARD, border=BORDER, radius=10,
                                  padx=16, pady=14, ocean=True)
            card.pack(fill="x", pady=CARD_GAP, padx=LIST_PAD)
            box = card.inner
            tk.Label(
                box,
                text="还没有任何 lane",
                bg=CARD,
                fg=TEXT,
                font=(FONT_UI, 11, "bold"),
            ).pack(anchor="w", pady=(0, 2))
            tk.Label(
                box,
                text="点右上角「＋ 新建版本」，选一个版本装进来（例如 next / 0.1.7-rc.2），\n"
                     "装好之后这里会出现卡片，点「打开」就能起一套独立的 DSH。\n\n"
                     "如果你已经在用某个版本，用下面「本机各套安装」里的「加入 lane 列表」把它接进来，\n"
                     "再点卡片上的「★ 设为主要版本」——以后新建的版本就会自动继承它的 API key。",
                bg=CARD,
                fg=MUTED,
                font=(FONT_UI, 9),
                justify="left",
            ).pack(anchor="w")
        else:
            top_refs = (
                core.read_credential_refs((self.cfg["lanes"].get(top) or {}).get("home") or "")
                if top
                else {}
            )
            # core.ordered_lanes：主要版本永远第一
            for name, data in core.ordered_lanes(self.cfg):
                # 一张卡画失败不能让整列停在那儿：以前循环里抛异常，列表就只剩
                # 前几张（而且下面几段全没了），看起来像"卡片没了、内容也空了"。
                try:
                    card = LaneCard(
                        self.list_frame,
                        self,
                        name,
                        data,
                        snap["states"].get(name),
                        is_primary=(name == top),
                        primary_name=top,
                        primary_refs=top_refs,
                    )
                except Exception as exc:  # noqa: BLE001
                    self.log(f"[XX] lane「{name}」的卡片画不出来："
                             f"{type(exc).__name__}: {exc}", "warn")
                    continue
                card.pack(fill="x", pady=CARD_GAP, padx=LIST_PAD)
                self._bind_wheel(card)

        self._build_foreign_section(snap)
        self._update_counts(snap)
        # 重建完之后滚区要跟着新内容重算：以前靠 Label 的 <Configure> 顺带跑，
        # 现在那条路被 _on_list_configure 的尺寸比对挡住了，得显式来一次。
        for child in self.list_frame.winfo_children():
            if isinstance(child, ui.RoundedCard):
                self._watch_card_size(child)
        self._layout_list()
        # 再来一次：刚建出来的卡片这一刻还没有真实高度（见 _content_height 的说明），
        # 等布局落定再量一次，滚动区才不会多出一截空白。
        if self._layout_job is not None:
            try:
                self.root.after_cancel(self._layout_job)
            except Exception:
                pass
        self._layout_job = self.root.after(150, self._relayout_settled)

    def _watch_card_size(self, card) -> None:
        """卡片高度变了 → 左列滚区重算一次。

        为什么监听卡片而不是等 Label：Label 的窗口项高度是**我们固定给它的**，
        卡片在里面变高变矮它自己不会变，所以再也不会发 <Configure>。
        而卡片刚建出来时宽度还是 1px，文字会被折成一长条、高度虚高；
        等真实宽度下来后高度缩回去，滚区却留在虚高那个值上 ——
        表现就是"内容早到底了，还能往下滚一大截空白"。
        """
        def on_cfg(e, c=card):
            h = int(e.height)
            if h == getattr(c, "_watched_h", None):
                return
            c._watched_h = h
            if self._layout_job is None:
                self._layout_job = self.root.after(120, self._relayout_settled)

        card.bind("<Configure>", on_cfg, add="+")

    def _relayout_settled(self) -> None:
        self._layout_job = None
        self._layout_list()

    # ── 未登记实例 / 系统全局安装 ───────────────────────────────────────────
    def _section_card(self, text: str, count: int | None = None, action=None) -> tk.Frame:
        """段落卡片：标题 + 计数 + 可选操作长在卡片里，返回下面那个内容区 Frame。

        以前是"一条无底色的标题带 + 一张卡片"，标题带在铺满整窗的海面上会切出
        一条纯色横条。现在标题就是卡片的第一行 —— 左列只剩白卡片，和卡片之间的海面。
        """
        card = ui.RoundedCard(self.list_frame, bg=CARD, border=BORDER, radius=10,
                              padx=13, pady=10, ocean=True)
        card.pack(fill="x", pady=(10, CARD_GAP), padx=LIST_PAD)
        self._bind_wheel(card)
        head = tk.Frame(card.inner, bg=CARD)
        head.pack(fill="x")
        if action is not None:
            self.link_button(head, action[0], action[1]).pack(side="right")
        tk.Label(head, text=text, bg=CARD, fg=TEXT,
                 font=(FONT_UI, 10, "bold")).pack(side="left")
        if count:
            ui.Chip(head, str(count), "ver", bg=CARD).pack(side="left", padx=(7, 0))
        tk.Frame(card.inner, bg=_T.line1, height=1).pack(fill="x", pady=(8, 8))
        body = tk.Frame(card.inner, bg=CARD)
        body.pack(fill="x")
        return body

    def _build_foreign_section(self, snap: dict) -> None:
        instances = snap["foreign"]
        # lane 自己的安装树不在这里重复列（上面已经有卡片了）
        installs = [i for i in snap["installs"] if not i["source"].startswith("lane ")]
        if not instances and not installs:
            return

        # 没有未登记实例时整段省略：原来会留一个标题 +「（当前没有别的 DSH 实例在跑）」，
        # 两行字只说明"什么都没发生"，纯属噪音。
        if instances:
            pad = self._section_card("未登记的实例（不是本启动器启动的）", len(instances))
            for idx, item in enumerate(instances):
                if idx:
                    tk.Frame(pad, bg=_T.line1, height=1).pack(fill="x", pady=8)
                row = tk.Frame(pad, bg=CARD)
                row.pack(fill="x")

                btns = tk.Frame(row, bg=CARD)
                btns.pack(side="right")
                self.button(btns, "打开页面", lambda u=item["url"]: self.open_url(u),
                            primary=True, size="sm").pack(side="left", padx=3)
                self.button(btns, "结束进程", lambda i=item: self.kill_instance(i),
                            danger=True, size="sm").pack(side="left", padx=3)

                ui.Pill(row, "像是 DSH" if item["dsh_like"] else "占用中",
                        "warn" if item["dsh_like"] else "off",
                        bg=CARD).pack(side="right", padx=(0, 12))

                left = tk.Frame(row, bg=CARD)
                left.pack(side="left", fill="x", expand=True)
                tk.Label(
                    left,
                    text=f"端口 {item['port']}",
                    bg=CARD,
                    fg=TEXT,
                    font=(FONT_UI, 13, "bold"),
                ).pack(anchor="w")
                tk.Label(
                    left,
                    text=f"PID {item['pid']}　进程 {item['exe']}",
                    bg=CARD,
                    fg=MUTED,
                    font=(FONT_UI, 9),
                ).pack(anchor="w", pady=(3, 0))
                tk.Label(
                    left,
                    text=f"启动于 {item.get('started') or '?'}　·　非本启动器管理",
                    bg=CARD,
                    fg=GRAY,
                    font=(FONT_UI, 8),
                ).pack(anchor="w", pady=(1, 0))
                link = tk.Label(
                    left,
                    text=item["url"] + "   ⧉ 点击复制",
                    bg=CARD,
                    fg=ACCENT,
                    font=(FONT_MONO, 8),
                    cursor="hand2",
                    anchor="w",
                )
                link.pack(anchor="w", pady=(2, 0))
                link.bind("<Button-1>", lambda _e, u=item["url"]: self.copy_text(u))

        if installs:
            pad = self._section_card(
                "本机各套安装（你平时在用的那份在这里）",
                len(installs),
                action=("打开根目录", self.open_root_dir),
            )
            for idx, item in enumerate(installs):
                if idx:
                    tk.Frame(pad, bg=_T.line1, height=1).pack(fill="x", pady=8)
                row = tk.Frame(pad, bg=CARD)
                row.pack(fill="x")
                self.button(
                    row, "加入 lane 列表", lambda i=item: self.adopt_install(i), size="sm",
                ).pack(side="right", padx=(10, 0))
                text = tk.Frame(row, bg=CARD)
                text.pack(side="left", fill="x", expand=True)
                tk.Label(
                    text,
                    text=f"{item['source']}　·　{item['version']}",
                    bg=CARD, fg=TEXT, font=(FONT_UI, 10, "bold"), anchor="w",
                ).pack(anchor="w")
                tk.Label(
                    text,
                    text=f"写入 {item.get('installedAt') or '?'}",
                    bg=CARD, fg=MUTED, font=(FONT_UI, 9), anchor="w",
                ).pack(anchor="w")
                path_lbl = tk.Label(
                    text,
                    text=item["path"],
                    bg=CARD, fg=ui.SUBTLE, font=(FONT_MONO, 8),
                    anchor="w", justify="left", cursor="hand2", wraplength=250,
                )
                path_lbl.pack(anchor="w", pady=(2, 0))
                path_lbl.bind("<Button-1>", lambda _e, p=item["path"]: self.copy_text(p))
                # 路径很长，宽度跟着卡片走，别写死 250 把路径折成三四行
                # （比一下再改：无脑 configure 会触发新的 <Configure>，来回抖）
                text.bind("<Configure>", lambda e, l=path_lbl: _set_wrap(l, e.width, 160))
            help_lbl = tk.Label(
                pad,
                text="「加入 lane 列表」＝用它现有的 DSH_HOME（~/.dsh）注册成一条 lane，之后就能在这里打开/停止它。",
                bg=CARD, fg=GRAY, font=(FONT_UI, 8), justify="left", wraplength=330,
            )
            help_lbl.pack(anchor="w", pady=(9, 0))
            pad.bind("<Configure>", lambda e, l=help_lbl: _set_wrap(l, e.width, 240))

    def _tick(self) -> None:
        if not self.busy:
            try:
                self.refresh_lanes(force=False)
            except Exception:
                pass
        self._heal_cards()
        self._tick_id = self.root.after(2500, self._tick)

    def _heal_cards(self) -> None:
        """对账：每张卡片画的图是不是还跟当前尺寸/位置对得上。

        为什么要有这个：卡片的圆角面是靠 <Configure> + 60ms 去抖画的。只要有一次
        重画被吞掉（Tk 丢了 Configure、去抖被 after_cancel 掉、PIL 抛异常），
        界面上就会留一块"旧图"甚至空白卡片 —— 而且它自己永远不会好，只能重启。
        与其赌"不会发生"，不如每 2.5 秒对一次账：过期就按当前尺寸补画。
        """
        fixed = 0
        try:
            kids = list(self.list_frame.winfo_children())
        except tk.TclError:
            return
        for child in kids:
            if not isinstance(child, ui.RoundedCard):
                continue
            try:
                # 不能用 winfo_ismapped()：canvas 上的窗口项一旦被滚出可视区，Tk 就把
                # 它标成"未映射"（滚回来才恢复）。自检最需要干活的时刻恰好是"刚滚过"，
                # 用 ismapped 等于在这个场景里整队跳过 —— 白卡片就永远没人补。
                if child.winfo_width() < 4 or child.winfo_height() < 4:
                    continue
                # 已经排着 60ms 后要重画的，就别插一脚：插进去会和那边互相取消，
                # 变成"永远在补画"（每 2.5 秒重画一轮，白烧 CPU 还可能闪）。
                if child._resize_job is not None:
                    continue
                # 只补"画的图跟当前尺寸对不上"。**不要**在这里按内容高度去 configure
                # 卡片高度：卡片自己的 _on_inner 也在管这件事，两边各按各的时刻算，
                # 会互相改来改去（高度 250↔292 来回跳，卡片就一直画不完）。
                if child.is_stale():
                    child.heal()
                    fixed += 1
            except (tk.TclError, AttributeError):
                continue
        if fixed:
            self._schedule_list_redraw()
            now = time.monotonic()
            if now - self._heal_at > 10:
                self._heal_at = now
                self.log(f"[!!] 有 {fixed} 张卡片的重绘没跟上，已按当前尺寸补画。"
                         f"反复出现的话请把这段发给我。", "warn")

    # ── 任务执行（后台线程 + 日志流）────────────────────────────────────────
    def run_job(self, title: str, fn, on_done=None) -> None:
        if self.busy:
            self.set_status("上一个任务还在进行中，请稍候…")
            return
        self.busy = True
        self.busy_label.configure(text=f"● {title} 进行中…")
        self.set_status(title)
        self.log("", None)
        self.log(f"──── {title} ────", "head")

        def worker() -> None:
            old = sys.stdout
            sys.stdout = QueueWriter(self.q)  # type: ignore[assignment]
            try:
                fn()
                self.q.put(("job_done", title))
                if on_done:
                    self.q.put(("callback", on_done))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("line", f"[XX] {type(exc).__name__}: {exc}"))
                self.q.put(("job_fail", title))
            finally:
                sys.stdout = old

        threading.Thread(target=worker, daemon=True).start()

    def _poll_queue(self) -> None:
        pending: list[tuple[str, str | None]] = []   # 攒一批一起写，避免逐行重绘
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    pending.append((payload, None))
                    continue
                if pending:                            # 先落盘日志，保持顺序可读
                    self._log_lines(pending)
                    pending = []
                if kind == "job_done":
                    self.busy = False
                    self.busy_label.configure(text="")
                    self.set_status(f"{payload} 完成")
                    self._invalidate()                 # 刚起停过，状态必变
                    self.refresh_lanes(force=True)
                elif kind == "job_fail":
                    self.busy = False
                    self.busy_label.configure(text="")
                    self.set_status(f"{payload} 失败")
                    self._invalidate()
                    self.refresh_lanes(force=True)
                elif kind == "callback":
                    try:
                        payload()
                    except Exception as exc:  # noqa: BLE001
                        self._log_lines([(f"[XX] 回调异常：{exc}", None)])
        except queue.Empty:
            pass
        if pending:
            self._log_lines(pending)
        self._poll_id = self.root.after(120, self._poll_queue)

    # ── 操作 ────────────────────────────────────────────────────────────────
    def open_lane(self, lane: str, port: int | None = None) -> None:
        args = argparse.Namespace(
            target=lane, port=port, cwd=None, timeout=None, no_browser=False, detach=True
        )

        def job() -> None:
            core.cmd_open(core.load_config(), args)

        self.run_job(f"打开 {lane}", job)

    def stop_lane(self, lane: str) -> None:
        args = argparse.Namespace(target=[lane])

        def job() -> None:
            core.cmd_stop(core.load_config(), args)

        self.run_job(f"停止 {lane}", job)

    def stop_all(self) -> None:
        cfg = core.load_config()
        running = [name for name in cfg.get("lanes", {}) if core.lane_runtime(cfg, name)]
        if not running:
            self.set_status("没有正在运行的 lane")
            return
        self.log(f"准备停止：{'、'.join(running)}", "muted")

        def job() -> None:
            core.cmd_stop(core.load_config(), argparse.Namespace(target=running))

        self.run_job("全部停止", job)

    def show_lane_log(self, lane: str) -> None:
        log_path = core.paths(self.cfg)["logs"] / f"{lane}.log"
        if not log_path.is_file():
            self.log(f"[!!] 这条 lane 还没有日志：{log_path}", "warn")
            return
        self.log(f"──── {lane} 日志尾部（{log_path}）────", "head")
        for line in core.tail_lines(log_path, 40):
            self.log(line)

    def manual_refresh(self) -> None:
        """「刷新」按钮 / F5：重画卡片，顺带重查一次 npm 上的版本（10 分钟内查过就用缓存）。"""
        self.refresh_lanes(force=True)
        self.refresh_remote(force=True)

    def open_repo(self) -> None:
        """打开 dsh 官方代码库（地址来自已装包 package.json 的 repository 字段）。"""
        self.log(f"用浏览器打开官方代码库 {core.REPO_URL}", "muted")
        try:
            webbrowser.open(core.REPO_URL)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 打开失败：{exc}", "warn")

    def refresh_remote(self, force: bool = False) -> None:
        """查一次 npm 的 dist-tag，显示在标题下面那一行。

        纯只读、走后台线程：查不到只把这行文字变黄并往日志里记一句，不弹窗、不写文件。
        默认 10 分钟内不重复查（点「刷新」= force 重查）。
        """
        if self._remote_busy:
            return
        if not force and time.time() - self._remote_at < 600:
            return
        self._remote_busy = True
        self._remote_note("正在查询 npm 上的版本…", ui.SUBTLE)

        # 本机已装了哪些版本（拿登记表比，不发任何请求）
        installed = {str((d or {}).get("version") or "") for d in (self.cfg.get("lanes") or {}).values()}

        def fail(reason: str) -> None:
            self._remote_done({"error": "查不到 npm 上的版本（离线或 registry 不通），点「刷新」重试"})
            self.log(f"[!!] 查询 npm 版本失败：{reason}", "warn")

        def worker() -> None:
            try:
                packument, reg = core.fetch_index(core.load_config())
            except Exception as exc:  # noqa: BLE001
                # 用主线程轮询的队列回话（而不是 worker 里调 root.after）：
                # 窗口不在 mainloop 里时（自检、正在关闭）after 会直接抛 RuntimeError。
                self.q.put(("callback", lambda: fail(str(exc))))
                return
            tags = packument.get("dist-tags") or {}
            order = ["latest", "next", "alpha"]
            keys = [k for k in order if k in tags] + sorted(k for k in tags if k not in order)
            # 把实际答话的源带上：镜像常常落后官方一两天，"这行数字从哪来"必须看得见
            payload = {
                "source": core.registry_label(reg),
                "tags": [(tag, str(tags[tag]), str(tags[tag]) in installed) for tag in keys],
            }
            self.q.put(("callback", lambda p=payload: self._remote_done(p)))

        threading.Thread(target=worker, daemon=True).start()

    # ── 标题下那行：状态文字 / 版本芯片 ──
    def _remote_reset_box(self) -> None:
        for child in self.remote_box.winfo_children():
            child.destroy()

    def _remote_note(self, text: str, fg: str) -> None:
        """只显示一句话（查询中 / 查不到）。"""
        self._remote_payload = None
        try:
            self._remote_reset_box()
            tk.Label(self.remote_box, text=text, bg=CARD, fg=fg,
                     font=(FONT_UI, 9)).pack(side="left")
        except tk.TclError:
            pass

    def _remote_done(self, payload: dict) -> None:
        self._remote_busy = False
        self._remote_at = time.time()
        if payload.get("error"):
            self._remote_note(payload["error"], AMBER)
            return
        self._remote_payload = payload
        self._render_remote()

    def _render_remote(self) -> None:
        """把 dist-tag 排成芯片；窗口窄就先丢后面的，别让芯片被切一半。"""
        payload = self._remote_payload
        if not payload:
            return
        self._remote_job = None
        try:
            self._remote_reset_box()
            src = tk.Label(
                self.remote_box, text=f"npm · {payload['source']}", bg=CARD,
                fg=ui.SUBTLE, font=(FONT_UI, 8),
            )
            src.pack(side="left", padx=(0, 8))
            # 预留 "+N" 那个小标签的位置：不然放完芯片再补它，整行又超了
            avail = max(90, self._header_left.winfo_width() - 34)
            used = src.winfo_reqwidth() + 8
            overflow = 0
            for tag, ver, have in payload["tags"]:
                chip = ui.Chip(
                    self.remote_box,
                    f"{tag} {ver}" + ("  ✓" if have else ""),
                    "ok" if have else "ver",
                    bg=CARD,
                )
                need = chip.winfo_reqwidth() + 5
                if used + need > avail:      # 放不下：销毁它，记一笔"还有几个没显示"
                    chip.destroy()
                    overflow += 1
                    continue
                chip.pack(side="left", padx=(0, 5))
                used += need
            if overflow:
                tk.Label(self.remote_box, text=f"+{overflow}", bg=CARD, fg=ui.SUBTLE,
                         font=(FONT_UI, 8)).pack(side="left", padx=(2, 0))
        except tk.TclError:
            pass

    def _on_header_resize(self, _e=None) -> None:
        """窗口变宽变窄时重排那排芯片（延迟 150ms，别在拖拽中反复重建）。"""
        if not self._remote_payload:
            return
        try:
            if self._remote_job:
                self.root.after_cancel(self._remote_job)
        except Exception:
            pass
        self._remote_job = self.root.after(150, self._render_remote)

    def open_root_dir(self) -> None:
        root = core.ensure_layout(core.load_config())["root"]
        try:
            os.startfile(str(root))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 打不开目录：{exc}", "warn")

    def open_url(self, url: str) -> None:
        """未登记实例只能用裸地址打开：浏览器里若已有该实例的登录 cookie 就能直接进，
        否则会看到 401（DSH 的信任围栏，token 只在它自己打印的 URL 里）。"""
        self.log(f"用浏览器打开 {url}", "muted")
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[!!] 打开失败：{exc}", "warn")

    def kill_instance(self, item: dict) -> None:
        pid = int(item["pid"])
        if not messagebox.askyesno(
            "结束进程",
            f"确定结束端口 {item['port']} 上的进程吗？\n\n"
            f"PID {pid}　进程 {item['exe']}\n"
            "这不是本启动器启动的实例——如果它正是你此刻在用的 DSH，"
            "当前页面会立刻断开。",
            icon="warning",
            default="no",
        ):
            return

        def job() -> None:
            core.kill_tree(pid)
            print(f"已结束 PID {pid}（端口 {item['port']}）")
            core.invalidate_tcp_cache()  # 让下一次刷新重新发现

        self.run_job(f"结束 {item['exe']} (PID {pid})", job)

    def adopt_install(self, item: dict) -> None:
        """把本机已有的一份安装注册成 lane，沿用它的 DSH_HOME——
        这样就能用同一个窗口"打开 / 停止"你平时在用的那套。"""
        cfg = core.load_config()
        want = str(item["path"]).lower()
        for name, data in cfg.get("lanes", {}).items():
            if isinstance(data, dict) and str(data.get("installDir", "")).lower() == want:
                self.log(f"[!!] 这份安装已经注册为 lane「{name}」了", "warn")
                self.set_status(f"已注册为 lane「{name}」")
                return
        default_name = {"npm 全局": "global", "npx 缓存": "npx"}.get(item["source"], "adopted")
        home = str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".dsh")
        if not messagebox.askyesno(
            "加入 lane 列表",
            "把这份安装注册成一条 lane？\n\n"
            f"来源　　　{item['source']}\n"
            f"版本　　　{item['version']}\n"
            f"安装目录　{item['path']}\n"
            f"lane 名　　{default_name}\n"
            "端口　　　3080（被别的程序占用会自动顺延）\n"
            f"DSH_HOME　{home}\n"
            "　　　　　（就是你现有的那份，含已有会话 / 设置 / 插件）\n\n"
            "注册后就能在本窗口「打开 / 停止」它，和你原来的启动方式是同一套环境。",
        ):
            return
        args = argparse.Namespace(
            lane=default_name, install=item["path"], port=None, home=None, force=True
        )
        self.run_job(f"加入 lane：{default_name}", lambda: core.cmd_adopt(core.load_config(), args))

    # ── 主要版本 ────────────────────────────────────────────────────────────
    def set_primary_lane(self, lane: str) -> None:
        """把某条 lane 标成「主要版本」：置顶 + 新建版本时继承它的 API key。"""
        cfg = core.load_config()
        if lane not in cfg.get("lanes", {}):
            self.log(f"[!!] 没有 lane「{lane}」", "warn")
            return
        old = core.primary_lane(cfg)
        refs = core.read_credential_refs((cfg["lanes"][lane] or {}).get("home") or "")
        if not messagebox.askyesno(
            "设为主要版本",
            f"把 lane「{lane}」设为主要版本？\n\n"
            f"　版本　　　{(cfg['lanes'][lane] or {}).get('version')}\n"
            f"　DSH_HOME　{(cfg['lanes'][lane] or {}).get('home')}\n"
            f"　可继承的 API key　{'、'.join(sorted(refs)) if refs else '（还没有，复制不到东西）'}\n\n"
            "之后新建的版本会自动复制它的 API key（只复制密钥，\n"
            "会话与设置仍各自独立）；它会被置顶，删除时需要手打名称二次确认。\n"
            + (f"\n原主要版本：{old}" if old and old != lane else ""),
        ):
            return
        core.set_primary(cfg, lane)
        self.log(f"★ 已把「{lane}」设为主要版本（置顶）", "ok")
        if refs:
            self.log(f"   以后新建版本会自动继承：{'、'.join(sorted(refs))}", "muted")
        else:
            self.log("   [!!] 它的 DSH_HOME 里还没有 API key，新建版本时复制不到东西", "warn")
        self.set_status(f"主要版本 → {lane}")
        self.refresh_lanes(force=True)

    def sync_key(self, lane: str) -> None:
        """把主要版本的 API key 补到这条 lane（加功能之前建的 lane 通常缺这一段）。"""
        cfg = core.load_config()
        src = core.primary_lane(cfg)
        if not src:
            self.log("[!!] 还没有主要版本，先点某张卡片上的「★ 设为主要版本」", "warn")
            return
        if src == lane:
            self.set_status("它自己就是主要版本")
            return
        refs = core.read_credential_refs((cfg["lanes"].get(src) or {}).get("home") or "")
        if not refs:
            self.log(f"[!!] 主要版本「{src}」的 DSH_HOME 里还没有 API key，没有东西可复制", "warn")
            return
        if not messagebox.askyesno(
            "补齐 API key",
            f"把主要版本「{src}」的 API key 复制给 lane「{lane}」？\n\n"
            f"　要复制的键　{'、'.join(sorted(refs))}\n"
            f"　写入位置　　{core.credentials_path((cfg['lanes'].get(lane) or {}).get('home') or '')}\n\n"
            "只补密钥这一项；这条 lane 自己的会话、设置、插件都不受影响。\n"
            "（原文件会先备份成 .credentials.yaml.bak）\n\n"
            "若它正在运行，需要重启一次才会用上新 key。",
        ):
            return
        args = argparse.Namespace(lane=[lane])

        def job() -> None:
            core.cmd_sync_key(core.load_config(), args)

        self.run_job(f"补齐 {lane} 的 API key", job)

    # ── 删除一套 DSH ────────────────────────────────────────────────────────
    def delete_lane(self, lane: str) -> None:
        """删除按钮：先算清会动哪些文件，再弹确认框（主要版本要手打名称）。"""
        cfg = core.load_config()
        if lane not in cfg.get("lanes", {}):
            self.log(f"[!!] 没有 lane「{lane}」", "warn")
            return
        try:
            plan = core.lane_delete_plan(cfg, lane, sizes=False)   # 体积放后台算，别卡界面
        except Exception as exc:  # noqa: BLE001
            self.log(f"[XX] 无法计算删除范围：{exc}", "err")
            return
        rt = core.lane_runtime(cfg, lane)

        dlg = tk.Toplevel(self.root)
        dlg.title(f"删除 DSH · {lane}")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)

        # 标题区
        title = tk.Frame(dlg, bg=BG)
        title.pack(fill="x", padx=18, pady=(14, 0))
        tk.Label(
            title,
            text=("删除主要版本" if plan["is_primary"] else "删除这套 DSH"),
            bg=BG,
            fg=(RED if plan["is_primary"] else TEXT),
            font=(FONT_UI, 14, "bold"),
        ).pack(anchor="w")
        tk.Label(
            title,
            text=f"lane「{lane}」　·　版本 {plan['version']}"
                 + ("　·　接管型（安装树在 npm 全局，本启动器不会删它）" if plan["adopted"] else ""),
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
        ).pack(anchor="w", pady=(2, 0))

        # 主要版本的特殊提醒
        if plan["is_primary"]:
            warn = tk.Frame(dlg, bg=RED_BG, highlightbackground=RED, highlightthickness=1)
            warn.pack(fill="x", padx=18, pady=(10, 0))
            tk.Label(
                warn,
                text="★ 这是你的【主要版本】—— 你日常真正在用的那一套",
                bg=RED_BG,
                fg=RED,
                font=(FONT_UI, 10, "bold"),
            ).pack(anchor="w", padx=12, pady=(9, 2))
            tk.Label(
                warn,
                text="删除后：这里的会话记录、设置、插件组合会一并消失，无法恢复，\n"
                     "主要版本标记也会被清空（需要另外指定一个）。\n"
                     "如果只是不想让它当主要版本，请改用「★ 设为主要版本」换到别的 lane 上。",
                bg=RED_BG,
                fg=RED,
                font=(FONT_UI, 9),
                justify="left",
            ).pack(anchor="w", padx=12, pady=(0, 9))

        # 内容清单：每一条右侧带一个勾选框（勾＝删，不勾＝留）
        box = tk.Frame(dlg, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        box.pack(fill="x", padx=18, pady=(10, 0))

        def row(text: str, color=MUTED) -> tk.Label:
            lab = tk.Label(
                box,
                text=text,
                bg=CARD,
                fg=color,
                font=(FONT_UI, 9),
                anchor="w",
                justify="left",
                wraplength=520,
            )
            lab.pack(anchor="w", padx=14, pady=(4, 2))
            return lab

        def item(label_text: str, size: str = "", check: tk.BooleanVar | None = None,
                 color=TEXT, note: str = "") -> tk.Label:
            line = tk.Frame(box, bg=CARD)
            line.pack(fill="x", padx=(14, 14), pady=1)
            if check is not None:
                tk.Checkbutton(
                    line,
                    variable=check,
                    bg=CARD,
                    activebackground=CARD,
                    selectcolor=_T.overlay,
                    highlightthickness=0,
                    bd=0,
                    width=2,
                    command=None,
                ).pack(side="right", padx=(8, 0))
            txt = label_text + (f"　（{size}）" if size else "")
            lab = tk.Label(
                line,
                text=txt,
                bg=CARD,
                fg=color,
                font=(FONT_UI, 9),
                anchor="w",
                justify="left",
                wraplength=560,
            )
            lab.pack(side="left", anchor="w")
            if note:  # 单独一行，否则长理由会被窗口边缘截断
                tk.Label(
                    box,
                    text=note,
                    bg=CARD,
                    fg=GRAY,
                    font=(FONT_UI, 8),
                    anchor="w",
                    justify="left",
                    wraplength=560,
                ).pack(anchor="w", padx=(32, 14), pady=(0, 2))
            return lab

        # 目录体积要全盘遍历（几百毫秒），不能在主线程算 —— 先摆"计算中…"，后台补上。
        size_slots: list[tuple[tk.Label, str, Path]] = []

        def item_sized(prefix: str, path: Path, check: tk.BooleanVar | None = None,
                       color=TEXT, note: str = "") -> None:
            lab = item(f"{prefix}　（计算中…）", check=check, color=color, note=note)
            size_slots.append((lab, prefix, path))

        row("将删除 / 保留", TEXT)
        item(f"登记信息　lane「{lane}」" + ("（含主要版本标记）" if plan["is_primary"] else ""), size="必删")
        if plan["run"].is_file():
            item(f"运行态　　{plan['run'].name}", size="必删")
        if plan["log"].is_file():
            item(f"启动日志　{plan['log'].name}", size="必删")

        # 安装树
        can_install = plan["install_inside"] and not plan["shared_with"]
        del_install_var = tk.BooleanVar(value=True)
        if can_install:
            item_sized(f"安装树　　{plan['install']}", Path(plan["install"]), del_install_var)
        else:
            why = (
                "保留 —— 接管型：删的是 npm 全局那份，要卸载请用 npm uninstall -g"
                if plan["adopted"]
                else f"保留 —— 其它 lane 还在共用这棵树：{'、'.join(plan['shared_with'])}"
            )
            item_sized(f"安装树　　{plan['install']}", Path(plan["install"]), color=GRAY, note=why)

        # DSH_HOME
        home_inside = plan["home_inside"]
        keep_home_var = tk.BooleanVar(value=False)   # 根目录内：不勾＝删
        danger_var = tk.BooleanVar(value=False)      # 根目录外：勾上才会删
        if home_inside:
            item_sized(f"DSH_HOME　{plan['home']}", Path(plan["home"]), keep_home_var)
            note = tk.Label(
                box,
                text="勾上＝保留 DSH_HOME（只删安装树和登记，会话记录留着）",
                bg=CARD,
                fg=GRAY,
                font=(FONT_UI, 8),
                anchor="w",
            )
            note.pack(anchor="w", padx=(30, 14), pady=(0, 2))
        else:
            item_sized(f"DSH_HOME　{plan['home']}", Path(plan["home"]),
                       danger_var, color=RED)
            tk.Label(
                box,
                text="★ 它不在 lane 根目录里（像是你现有的 HOME），里面有真实会话记录、设置和插件，\n"
                     "　 默认保留。确实要一起删才勾左边的框 —— 删了无法恢复。",
                bg=CARD,
                fg=RED,
                font=(FONT_UI, 8),
                justify="left",
                anchor="w",
            ).pack(anchor="w", padx=(30, 14), pady=(0, 2))

        box.pack_configure(pady=(10, 0))

        def fill_sizes() -> None:
            """后台算体积，算完通过 after 回主线程贴到标签上（对话框可能已经被关掉）。"""
            done = [(lab, prefix, core.dir_size(p)) for lab, prefix, p in size_slots]

            def apply() -> None:
                for lab, prefix, nbytes in done:
                    try:
                        if lab.winfo_exists():
                            lab.configure(text=f"{prefix}　（{core.human_size(nbytes)}）")
                    except tk.TclError:
                        pass  # 对话框已关闭

            try:
                self.root.after(0, apply)
            except tk.TclError:
                pass

        threading.Thread(target=fill_sizes, daemon=True).start()

        # 运行中：必须先停下来
        stop_var = tk.BooleanVar(value=True)
        if rt:
            stopbar = tk.Frame(dlg, bg=AMBER_BG, highlightbackground=AMBER, highlightthickness=1)
            stopbar.pack(fill="x", padx=18, pady=(10, 0))
            tk.Checkbutton(
                stopbar,
                text=f"它正在运行（PID {rt.get('pid')}，端口 {rt.get('port')}）—— 先停止它再删除",
                variable=stop_var,
                bg=AMBER_BG,
                fg=AMBER,
                font=(FONT_UI, 9, "bold"),
                activebackground=AMBER_BG,
                selectcolor=_T.overlay,
                anchor="w",
                padx=10,
                pady=6,
            ).pack(anchor="w")

        # 确认输入
        conf = tk.Frame(dlg, bg=BG)
        conf.pack(fill="x", padx=18, pady=(12, 0))
        tk.Label(
            conf, text="请输入 lane 名称 ", bg=BG, fg=TEXT, font=(FONT_UI, 10)
        ).pack(side="left")
        tk.Label(conf, text=lane, bg=BG, fg=RED, font=(FONT_MONO, 10, "bold")).pack(side="left")
        tk.Label(conf, text=" 以确认：", bg=BG, fg=TEXT, font=(FONT_UI, 10)).pack(side="left")
        entry_var = tk.StringVar()
        entry = ttk.Entry(conf, textvariable=entry_var, width=18)
        entry.pack(side="left", padx=(8, 0))
        entry.focus_set()

        hint = tk.Label(dlg, text="", bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        hint.pack(fill="x", padx=18, pady=(6, 0))

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=18, pady=(8, 16))

        def collect():
            """把三个勾选框翻译成 cmd_delete 的参数。"""
            if home_inside:
                keep_home, home_too = bool(keep_home_var.get()), False
            else:
                keep_home, home_too = True, bool(danger_var.get())
            keep_install = not bool(del_install_var.get()) if can_install else True
            return keep_home, home_too, keep_install

        del_btn = self.button(btns, "删除", lambda: None, danger=True)
        del_btn.pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)

        def refresh_btn(*_a) -> None:
            name_ok = entry_var.get().strip() == lane
            run_ok = (not rt) or stop_var.get()
            _keep_home, _home_too, keep_install = collect()
            if not name_ok:
                hint.configure(text=f"为避免误删，请完整输入「{lane}」", fg=MUTED)
            elif not run_ok:
                hint.configure(text="它正在运行，必须先勾上「先停止它」", fg=RED)
            else:
                bits = []
                bits.append("保留安装树" if keep_install else "删除安装树")
                if home_inside:
                    bits.append("保留 DSH_HOME" if keep_home_var.get() else "删除 DSH_HOME")
                elif danger_var.get():
                    bits.append("连现有 DSH_HOME 一起删除")
                else:
                    bits.append("保留现有 DSH_HOME")
                hint.configure(text="确认无误：" + "、".join(bits), fg=GREEN)
            del_btn.configure(state=("normal" if (name_ok and run_ok) else "disabled"))
            del_btn.configure(text=("删除主要版本" if plan["is_primary"] else "删除"))

        def do_delete() -> None:
            keep_home, home_too, keep_install = collect()
            args = argparse.Namespace(
                lane=lane,
                yes=True,
                stop=bool(rt) and stop_var.get(),
                keep_home=keep_home,
                keep_install=keep_install,
                home_too=home_too,
            )
            dlg.destroy()

            def job() -> None:
                core.cmd_delete(core.load_config(), args)
                core.invalidate_tcp_cache()  # 让下一次刷新重新发现实例

            self.run_job(f"删除 {lane}", job)

        del_btn.configure(command=do_delete)
        entry_var.trace_add("write", refresh_btn)
        stop_var.trace_add("write", refresh_btn)
        keep_home_var.trace_add("write", refresh_btn)
        danger_var.trace_add("write", refresh_btn)
        del_install_var.trace_add("write", refresh_btn)
        refresh_btn()
        entry.bind("<Return>", lambda _e: do_delete() if str(del_btn["state"]) == "normal" else None)

        dlg.update_idletasks()
        w = 660
        h = min(760, max(360, dlg.winfo_reqheight() + 8))
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 40
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

    # ── 新建版本对话框 ──────────────────────────────────────────────────────
    def clone_lane(self, lane: str) -> None:
        """复制这条 lane（安装树 + DSH_HOME 一起）——「先升级副本、看插件还活不活」的入口。"""
        cfg = core.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("复制这套 DSH")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 580, 452
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 80
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(
            dlg, text=f"复制「{lane}」生成副本", bg=BG, fg=TEXT, font=(FONT_UI, 13, "bold")
        ).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(
            dlg,
            text=(
                "副本带走这套的全部状态：会话记录、设置、已装插件、插件数据、API key。\n"
                "副本有自己的安装树和 DSH_HOME —— 升级副本不会碰到原件，出问题删掉副本就行。\n"
                "典型用法：复制主要版本 → 升级副本 → 点「核对隔离」→ 看插件还都在不在。"
            ),
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
            wraplength=520,
            justify="left",
        ).pack(anchor="w", padx=20)

        no_inherit_var, _ = self.no_inherit_row(
            dlg,
            hint_off="（默认＝整套复制。想要一个不带你的 key、也不带你设置的干净副本，就勾上）",
            hint_on="（副本会跳过 API key、匿名身份、DSH 设置与 agent 预设；"
                    "插件、插件数据、对话、附件照旧。启动后卡片会提示「补齐 API key…」，想补随时点）",
            pady=(10, 0),
        )

        form = tk.Frame(dlg, bg=BG)
        form.pack(fill="x", padx=20, pady=14)
        form.columnconfigure(1, weight=1)
        tk.Label(form, text="新 lane 名", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(
            row=0, column=0, sticky="w", pady=6
        )
        name_var = tk.StringVar(value=f"{lane}-copy")
        ttk.Entry(form, textvariable=name_var).grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)
        tk.Label(form, text="端口", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(
            row=1, column=0, sticky="w", pady=6
        )
        port_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=port_var, width=12).grid(
            row=1, column=1, sticky="w", padx=(10, 0), pady=6
        )
        try:
            port_var.set(str(core.pick_port(cfg)))
        except Exception:
            port_var.set("")

        size_hint = tk.Label(dlg, text="正在估算体积…", bg=BG, fg=GRAY, font=(FONT_UI, 9), anchor="w")
        size_hint.pack(fill="x", padx=20)

        def fill_size() -> None:
            # 体积统计要扫 1GB 多的目录，放后台线程，别冻住对话框
            try:
                plan = core.lane_clone_plan(
                    core.load_config(), lane, name_var.get().strip() or f"{lane}-copy", sizes=True
                )
                need = (plan["home_size"] or 0) + (plan["install_size"] or 0)
                parts = [
                    f"预计复制 {core.human_size(need)}"
                    f"（安装树 {core.human_size(plan['install_size'] or 0)}"
                    f" + HOME {core.human_size(plan['home_size'] or 0)}）"
                ]
                if plan["free"]:
                    parts.append(f"目标盘剩余 {core.human_size(plan['free'])}")
                if plan["problems"]:
                    parts.append("问题：" + "；".join(plan["problems"]))
                text = "，".join(parts)
                bg, fg = (AMBER_BG, AMBER) if plan["problems"] else (BG, GRAY)
            except Exception as exc:  # noqa: BLE001
                text, bg, fg = f"体积估算失败：{exc}", AMBER_BG, AMBER

            def apply() -> None:
                try:
                    if dlg.winfo_exists():
                        size_hint.configure(text=text, bg=bg, fg=fg)
                except tk.TclError:
                    pass

            self.root.after(0, apply)

        threading.Thread(target=fill_size, daemon=True).start()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(6, 16), side="bottom")

        def start() -> None:
            new = name_var.get().strip()
            if not new:
                messagebox.showwarning("复制", "先填新 lane 名", parent=dlg)
                return
            if new == lane:
                messagebox.showwarning("复制", "新 lane 名不能和源一样", parent=dlg)
                return
            if new in (core.load_config().get("lanes") or {}):
                messagebox.showwarning("复制", f"lane「{new}」已经存在了", parent=dlg)
                return
            port: int | None = None
            if port_var.get().strip():
                try:
                    port = int(port_var.get().strip())
                except ValueError:
                    messagebox.showwarning("复制", "端口必须是数字", parent=dlg)
                    return
            # Tk 变量只在主线程读：Namespace 在这里一次拼好，worker 线程只管跑
            ns = argparse.Namespace(source=lane, lane=new, port=port,
                                    no_inherit=no_inherit_var.get())
            dlg.destroy()
            self.run_job(
                f"复制 {lane} → {new}",
                lambda: core.cmd_clone(core.load_config(), ns),
            )

        ttk.Button(btns, text="开始复制", command=start).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))

    def verify_lane(self, lane: str) -> None:
        """核对这条 lane 是否真的和别的版本隔离（副本最该看这个）。"""
        self.run_job(
            f"核对隔离：{lane}",
            lambda: core.cmd_verify(core.load_config(), argparse.Namespace(lane=lane)),
        )

    def upgrade_dialog(self, lane: str) -> None:
        """把这条 lane 精确换到另一个版本（升完核对 + 启动冒烟，起不来自动退回）。

        为什么值得单独一个对话框：换 DSH 版本是这套启动器里**最容易出大事**的动作，
        所以三件事必须摆在明面上 —— 当前版本、会装到哪棵树、失败怎么退。
        真正干活的是 `core.cmd_upgrade`（和 CLI 同一条路），输出进下面的日志面板。
        """
        cfg = core.load_config()
        core.sync_lane_versions(cfg)      # 「当前版本」必须是安装树里的事实，不是旧台账
        data = (cfg.get("lanes") or {}).get(lane) or {}
        plan = core.lane_upgrade_plan(cfg, lane)
        external = plan["mode"] == "external"
        running = core.lane_runtime(cfg, lane)

        dlg = tk.Toplevel(self.root)
        dlg.title("升级默认路径的 DSH" if external else "升级版本")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        w, h = 700, 640
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 50
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"换 DSH 版本：lane「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        head = tk.Label(dlg, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9), justify="left",
                        wraplength=650, anchor="w")
        head.pack(anchor="w", padx=18)
        head.configure(text=(
            f"当前版本  {data.get('version') or '?'}\n"
            f"安装树    {plan['dir']}\n"
            f"升级方式  {plan['why']}"
            + (f"\n它现在正在运行（pid {running.get('pid')}，端口 {running.get('port')}）"
               " —— 升级会先停掉它，升完再起回来。" if running else "")
        ))

        note = tk.Label(dlg, text="", bg=BG, fg=AMBER, font=(FONT_UI, 9), justify="left",
                        anchor="w", wraplength=650)
        note.pack(anchor="w", padx=18, pady=(6, 0))
        if external:
            note.configure(
                text="这条 lane 的安装树在启动器目录之外（你日常真正在用的那一套）。"
                     "本窗口不会替你装：点下面「在终端里升级」会新开一个终端窗口，"
                     "把那条 npm 命令交给你看着跑完——本工具只铺命令、不执行安装。",
                fg=AMBER,
            )

        reg_var, reg_remember = self.registry_row(dlg, cfg, pady=(12, 0))

        tk.Label(dlg, text="从 registry 挑一版（点一下填进下面，也可以自己手打版本号）",
                 bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w").pack(anchor="w", padx=18, pady=(12, 2))
        listbox = tk.Listbox(dlg, height=9, bg=CARD, fg=TEXT, font=(FONT_MONO, 9),
                             selectbackground=ACCENT, highlightthickness=0, activestyle="none")
        listbox.pack(fill="both", expand=True, padx=18)

        row = tk.Frame(dlg, bg=BG)
        row.pack(fill="x", padx=18, pady=(10, 2))
        tk.Label(row, text="版本", bg=BG, fg=MUTED, font=(FONT_UI, 9)).pack(side="left")
        var = tk.StringVar(value="")
        entry = tk.Entry(row, textvariable=var, font=(FONT_MONO, 10), width=24)
        entry.pack(side="left", padx=(8, 0))
        hint = tk.Label(row, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9))
        hint.pack(side="left", padx=(10, 0))

        stop_first = tk.BooleanVar(value=True)
        if external and running:
            # npm 覆盖全局目录时，正在被 node 打开的文件在 Windows 上改不动（EPERM/EBUSY），
            # 先停更省事：装完他从卡片上重新打开即可。
            chk = tk.Frame(dlg, bg=BG)
            chk.pack(fill="x", padx=18, pady=(8, 0))
            tk.Checkbutton(
                chk,
                text="先停掉它再开终端（推荐：运行中的 node 占着文件，npm 覆盖会失败）",
                variable=stop_first, bg=BG, fg=TEXT, font=(FONT_UI, 9),
                selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                highlightthickness=0, bd=0, anchor="w",
            ).pack(anchor="w")

        foot = tk.Frame(dlg, bg=BG)
        foot.pack(fill="x", padx=18, pady=(8, 14))
        order: list[str] = []

        def pick(_event=None) -> None:
            sel = listbox.curselection()
            if sel and sel[0] < len(order):
                var.set(order[sel[0]])

        listbox.bind("<<ListboxSelect>>", pick)

        def start(rollback: bool, dry_run: bool = False) -> None:
            version = None if rollback else var.get().strip()
            if rollback:
                target = (data.get("previousVersion") or "").strip()
                if not target:
                    messagebox.showinfo("没有可退回的版本",
                                        "这条 lane 还没有升级记录，没有可退回的版本。", parent=dlg)
                    return
                if not dry_run and not messagebox.askyesno(
                    "退回上一版",
                    f"把「{lane}」退回 {target}？\n\n"
                    "会做：换回那棵树 → 核对 → 启动冒烟；起不来自动退回。",
                    parent=dlg,
                ):
                    return
            else:
                if not version:
                    messagebox.showinfo("先填版本号",
                                        "在上面挑一版或手打一个版本号（例如 0.1.7-rc.2 / latest）。",
                                        parent=dlg)
                    return
                if not dry_run and not messagebox.askyesno(
                    "升级",
                    f"把「{lane}」升到 {version}？\n\n"
                    f"当前 {data.get('version') or '?'} → 装到 {plan['dir']}\n"
                    f"下载源：{core.registry_label(core.registry_plan(cfg, reg_var.get())[0])}\n"
                    "会做：升级 → 核对 → 启动冒烟；起不来自动退回原版本。",
                    parent=dlg,
                ):
                    return
            args = argparse.Namespace(
                lane=lane, version=version, rollback=rollback, stop=True,
                no_boot_check=False, dry_run=dry_run, registry=reg_var.get(),
            )
            if not dry_run:
                self._save_registry_choice(reg_var, reg_remember)
            title = f"升级 {lane}" + ("（只看会做什么）" if dry_run else "")
            dlg.destroy()
            self.run_job(title, lambda: core.cmd_upgrade(core.load_config(), args))

        def start_external() -> None:
            """外部安装（默认路径那份）：开一个新终端跑 npm，本进程不装。"""
            version = var.get().strip()
            if not version:
                messagebox.showinfo("先填版本号",
                                    "在上面挑一版或手打一个版本号（例如 0.2.0-rc.2 / latest）。",
                                    parent=dlg)
                return
            try:
                ext = core.external_upgrade_plan(cfg, lane, version, reg_var.get())
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("算不出升级命令", f"{type(exc).__name__}: {exc}", parent=dlg)
                return
            want_stop = bool(external and running and stop_first.get())
            stop_line = f"· 先停掉「{lane}」，装完请从卡片上重新打开\n" if want_stop else ""
            if not messagebox.askyesno(
                "在终端里升级",
                f"会新开一个终端窗口，在里面执行：\n\n{ext['command']}\n\n"
                f"· 装到：{ext['install_dir']}\n"
                + stop_line
                + "· 本工具不执行安装，只在新窗口里把那条命令跑起来\n"
                  "· 装完回这里点「刷新」（F5），版本号会按安装树自动更正\n\n继续？",
                parent=dlg,
            ):
                return
            self._save_registry_choice(reg_var, reg_remember)
            if self.upgrade_in_terminal(lane, version, reg_var.get(), want_stop):
                dlg.destroy()
                self.refresh_lanes(force=True)

        self.link_button(foot, "关闭", dlg.destroy, fg=MUTED).pack(side="right")
        self.link_button(foot, "先看会做什么", lambda: start(False, dry_run=True),
                         fg=ACCENT_DK).pack(side="right", padx=(0, 14))
        if data.get("previousVersion"):
            self.link_button(
                foot, f"退回上一版（{data['previousVersion']}）", lambda: start(True), fg=AMBER
            ).pack(side="right", padx=(0, 14))
        if not external:
            up = self.link_button(foot, "升级到这一版", lambda: start(False), fg=ACCENT_DK)
            up.pack(side="right", padx=(0, 14))
        else:
            # 外部安装这条例外：本进程不装，只把命令铺到新终端里（见 core 的注释）。
            self.link_button(foot, "在终端里升级", start_external, fg=ACCENT_DK).pack(
                side="right", padx=(0, 14)
            )

        def load(choice: str) -> None:
            try:
                plan = core.registry_plan(cfg, choice)
                packument, reg = core.fetch_index(cfg, registries=plan)
                versions = core.sorted_versions(packument)
                tags = packument.get("dist-tags", {})
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: hint.configure(
                    text=f"查 registry 失败（{exc}）——自己手打版本号也行", fg=AMBER))
                return

            def apply() -> None:
                try:
                    if not dlg.winfo_exists():
                        return
                except tk.TclError:
                    return
                tag_names: dict[str, list[str]] = {}
                for tag, ver in tags.items():
                    tag_names.setdefault(ver, []).append(tag)
                current = str(data.get("version") or "")
                for ver in reversed(versions[-30:]):
                    marks = []
                    if ver == current:
                        marks.append("现在用的")
                    try:
                        # 外部安装的树是"dsh 包目录"本身，不是 `<前缀>/versions/<版本>`：
                        # 拿本启动器的 versions 目录去核对会把"本地已装"标错。
                        if external:
                            if core.package_version_at(Path(plan["dir"])) == ver:
                                marks.append("已是这一版")
                        elif core.installed_version(core.paths(cfg)["versions"] / ver) == ver:
                            marks.append("本地已装")
                    except Exception:  # noqa: BLE001
                        pass
                    order.append(ver)
                    listbox.insert(
                        "end",
                        f"  {ver:<16}{','.join(tag_names.get(ver, [])) or '-':<12}"
                        + ("  ← " + "、".join(marks) if marks else ""),
                    )
                if versions:
                    var.set(versions[-1])
                    newest = versions[-1]
                    hint.configure(
                        text=f"registry 最新 {newest}"
                             + ("（就是你现在这个）" if newest == current else " ← 比你现在的新")
                             + f"　来源 {reg}",
                        fg=GRAY,
                    )

            self.root.after(0, apply)

        # 换源就重查一次版本列表（列表和「最新版」都跟着源走）。
        # Tk 变量只能在主线程读，所以在这里读好再交给子线程。
        def reload(*_a) -> None:
            choice = reg_var.get()
            threading.Thread(target=load, args=(choice,), daemon=True).start()

        reg_var.trace_add("write", reload)
        reload()


    def upgrade_in_terminal(self, lane: str, version: str, registry: str,
                            stop_first: bool) -> str | None:
        """外部安装（默认路径那份）升级：新开一个终端跑 npm，**本进程不装**。

        为什么不在本进程里 `subprocess` 跑：npm 要装两三分钟、输出几十行，那些东西
        他得当场看着；而 GUI 壳只能把输出攒进日志面板。失败的现场留在终端里，
        他还能照着改版本号重跑。本函数只负责铺脚本 + 开窗口。
        返回终端脚本路径；失败返回 None（已弹框说明）。
        """
        try:
            got = core.run_external_upgrade_in_terminal(
                core.load_config(), lane, version, registry, stop_first=stop_first
            )
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("没开成终端", f"{type(exc).__name__}: {exc}", parent=self.root)
            return None
        self.log(f"已新开终端，在里面执行：{got['command']}", "ok")
        self.log(f"脚本：{got['bat']}", "muted")
        if stop_first:
            self._invalidate()
        messagebox.showinfo(
            "已在新终端里升级",
            f"{got['command']}\n\n装完回到启动器点「刷新」（F5），版本号会按安装树自动更正。",
            parent=self.root,
        )
        return got["bat"]

    def market_dialog(self, lane: str) -> None:
        """插件市场单独开关 + 更新。

        它值得单独一个对话框：关掉别的插件容易，关掉「插件市场」本身以前只能手改 YAML；
        而它恰恰是唯一需要"更新"的那个插件——更新别的插件是它的活儿，没人能更新它自己。
        更新走的是 dsh 自己的插件管理通道（dsh plugin → pnpm），所以和插件市场内部
        更新别的插件是同一条路：钉具体版本号、必要时带一次性绕过。
        """
        cfg = core.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("插件市场")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        w, h = 680, 470
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 60
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"插件市场（dshmarket）：lane「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Label(
            dlg,
            text=(
                "关掉它不会动别的插件，也不会动官方组合包；「打开」把启动器写的那一小段删掉就回来了。\n"
                "「更新」只更新插件市场自己：点名到版本地装（pnpm 的「新版本按住」只拦自动升级），"
                "装完启动一次确认能起来，起不来自动回滚。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=620,
        ).pack(anchor="w", padx=18)

        state = tk.Label(dlg, text="正在读状态（顺便查一次 registry）…", bg=BG, fg=TEXT,
                         font=(FONT_UI, 11, "bold"), anchor="w")
        state.pack(fill="x", padx=18, pady=(12, 0))
        detail = tk.Label(dlg, text="", bg=BG, fg=GRAY, font=(FONT_UI, 9), justify="left",
                          anchor="w", wraplength=620)
        detail.pack(fill="x", padx=18, pady=(4, 0))
        note = tk.Label(dlg, text="", bg=BG, fg=AMBER, font=(FONT_UI, 9), justify="left",
                        anchor="w", wraplength=620)
        note.pack(fill="x", padx=18, pady=(8, 0))

        holder: dict = {"state": None, "latest": None}

        def fill() -> None:
            try:
                st = core.market_status(cfg, lane, with_registry=True)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: state.configure(text=f"读取失败：{exc}", fg=RED))
                return

            def apply() -> None:
                try:
                    if not dlg.winfo_exists():
                        return
                except tk.TclError:
                    return
                holder["state"] = st
                holder["latest"] = st.get("latest")
                off = core.market_disabled_rows(st)
                if not st["spec"] and not st["package_dir"].is_dir():
                    state.configure(text="这条 lane 没装插件市场", fg=AMBER)
                elif off and st["rows"] and len(off) == len(st["rows"]):
                    state.configure(text="已关掉（插件开关块在）", fg=GREEN)
                elif off:
                    state.configure(text=f"部分关着：{'、'.join(off)}", fg=AMBER)
                else:
                    state.configure(text="启用中", fg=TEXT)
                lines = [f"已装版本  {st['installed'] or '（没装）'}"
                         + (f"      profile 里写的是 {st['spec']}" if st["spec"] else ""),
                         f"行 id     {'、'.join(st['rows']) or '(没有补丁行)'}"]
                if st["latest"]:
                    same = st["latest"] == st["installed"]
                    lines.append(f"registry  最新 {st['latest']}"
                                 + ("（就是你现在这个）" if same else "  ← 有新版本"))
                elif st["registry_error"]:
                    lines.append(f"registry  查不到：{st['registry_error']}")
                else:
                    lines.append("registry  没查（离线）")
                if st["snapshots"]:
                    lines.append(f"更新快照  {len(st['snapshots'])} 个，最新是 "
                                 f"{st['snapshots'][0]['version'] or '?'}")
                detail.configure(text="\n".join(lines))
                if st["general_block"]:
                    note.configure(text="注意：这条 lane 的补丁层里还挂着一个"
                                        "「dsh-lanes 禁用第三方插件」块（启动器已不再维护它，"
                                        "要撤只能手删那几行）")
                elif st["latest"] and st["latest"] != st["installed"]:
                    note.configure(text=f"有新版 {st['latest']}：点「更新」即可（会先存快照）")
                else:
                    note.configure(text="")

            self.root.after(0, apply)

        threading.Thread(target=fill, daemon=True).start()

        boot_var = tk.BooleanVar(value=True)
        anyway_var = tk.BooleanVar(value=False)
        opt = tk.Frame(dlg, bg=BG)
        opt.pack(fill="x", padx=18, pady=(6, 0))
        tk.Checkbutton(opt, text="改动后启动一次确认还能起来（起不来就自动还原）", variable=boot_var,
                       bg=BG, fg=TEXT, selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                       font=(FONT_UI, 9), anchor="w").pack(anchor="w")
        tk.Checkbutton(opt, text="新版本被 pnpm 的 strict 按住也硬闯（--anyway，带一次性绕过）",
                       variable=anyway_var, bg=BG, fg=TEXT, selectcolor=CARD,
                       activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
                       anchor="w").pack(anchor="w")

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=18, pady=(10, 14))

        def do_off() -> None:
            args = argparse.Namespace(lane=lane, no_verify=False, no_boot_check=not boot_var.get())
            dlg.destroy()
            self.run_job(f"关掉插件市场：{lane}",
                         lambda: core.cmd_market_off(core.load_config(), args))

        def do_on() -> None:
            args = argparse.Namespace(lane=lane, no_verify=False)
            dlg.destroy()
            self.run_job(f"打开插件市场：{lane}",
                         lambda: core.cmd_market_on(core.load_config(), args))

        def do_update() -> None:
            st = holder["state"]
            if not st:
                messagebox.showwarning("插件市场", "状态还没读出来，稍等一下再点", parent=dlg)
                return
            latest = holder["latest"]
            if not latest:
                messagebox.showwarning(
                    "插件市场",
                    "没查到 registry 上的版本（网络？）。可以先用命令行指定版本：\n"
                    f"py dsh_lanes.py market-update {lane} --version <版本号>",
                    parent=dlg,
                )
                return
            if not messagebox.askyesno(
                "更新插件市场",
                f"把插件市场更新到 {latest}？\n\n"
                "· 会先把现在的包与清单存一份快照（失败能退回）\n"
                "· 走 dsh plugin → pnpm，钉死这个版本号\n"
                "· 装完启动一次确认能起来，起不来自动回滚\n"
                "· 更新完要重启这条 lane 才生效",
                parent=dlg,
            ):
                return
            args = argparse.Namespace(lane=lane, version=None, anyway=anyway_var.get(),
                                      force=False, offline=False, no_verify=False,
                                      no_boot_check=not boot_var.get())
            dlg.destroy()
            self.run_job(f"更新插件市场：{lane}",
                         lambda: core.cmd_market_update(core.load_config(), args))

        ttk.Button(btns, text="更新", command=do_update).pack(side="right")
        ttk.Button(btns, text="取消", command=dlg.destroy).pack(side="right", padx=(0, 8))
        ttk.Button(btns, text="打开", command=do_on).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="关掉", command=do_off).pack(side="left")

    def chat_dialog(self, lane: str) -> None:
        """备份对话：列出快照，可立即备份 / 恢复 / 删除。

        备份的是 <HOME>\\sessions —— 对话记录本体（每个会话一个 .zstd）。
        恢复之前会先**校验快照自身**（坏备份恢复只会更糟），再把现状另存一份（恢复错了能回来），
        恢复完逐文件核对指纹。
        """
        cfg = core.load_config()
        dlg = tk.Toplevel(self.root)
        dlg.title("备份对话")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        w, h = 780, 540
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 50
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"备份对话：lane「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=18, pady=(14, 4))
        tk.Label(
            dlg,
            text=(
                "备份的是这条 lane 的对话记录（DSH_HOME\\sessions，一个会话一个文件）。\n"
                "恢复前会先校验快照本身，再把现在的记录另存一份；恢复后逐文件比对指纹。\n"
                "这条 lane 正在运行时，恢复会先被拦住（先停它）。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=730,
        ).pack(anchor="w", padx=18)

        head = tk.Frame(dlg, bg=BG)
        head.pack(fill="x", padx=18, pady=(10, 0))
        tk.Label(head, text=f"{'名称':<30}{'时间':<20}{'会话':>5}{'大小':>11}", bg=BG, fg=MUTED,
                 font=(FONT_MONO, 9)).pack(anchor="w")

        wrap = tk.Frame(dlg, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True, padx=18, pady=(4, 6))
        scroll = ui.MiniScrollbar(wrap, command=None, bg=CARD)
        box = tk.Listbox(wrap, bg=CARD, fg=TEXT, font=(FONT_MONO, 9), activestyle="none",
                         selectbackground=ACCENT, selectforeground=_T.on_brand,
                         highlightthickness=0, yscrollcommand=scroll.set)
        scroll.configure(command=box.yview)
        scroll.pack(side="right", fill="y")
        box.pack(side="left", fill="both", expand=True)

        status = tk.Label(dlg, text="正在读取备份列表…", bg=BG, fg=GRAY, font=(FONT_UI, 9),
                          justify="left", anchor="w", wraplength=730)
        status.pack(fill="x", padx=18)
        items: list[dict] = []

        def load() -> None:
            try:
                snaps = core.chat_snapshots(cfg, lane)
                now = core.count_sessions(core.lane_home(cfg, lane))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: status.configure(text=f"读取失败：{exc}", fg=RED))
                return

            def apply() -> None:
                try:
                    if not dlg.winfo_exists():
                        return
                except tk.TclError:
                    return
                items.clear()
                items.extend(snaps)
                box.delete(0, "end")
                for it in snaps:
                    flag = "" if it["ok"] else "  [不可用：缺 manifest.json]"
                    note = "运行时备份" if it["running"] else (it["note"] or "")
                    box.insert(
                        "end",
                        f"{it['name'][:29]:<30}{str(it['created'])[:19]:<20}"
                        f"{it['sessions']:>5}{core.human_size(it['size']):>11}  {note}{flag}",
                    )
                if snaps:
                    total = sum(int(it["size"] or 0) for it in snaps)
                    status.configure(
                        text=f"{len(snaps)} 份备份 / {core.human_size(total)}；"
                             f"这条 lane 现在的会话文件数 {now}",
                        fg=GRAY,
                    )
                else:
                    status.configure(text=f"还没有备份。当前会话文件数 {now}，点「立即备份」来一份。",
                                     fg=AMBER)

            self.root.after(0, apply)

        threading.Thread(target=load, daemon=True).start()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=18, pady=(8, 14))

        def picked() -> dict | None:
            sel = box.curselection()
            if not sel:
                messagebox.showinfo("备份对话", "先在列表里选一份备份", parent=dlg)
                return None
            index = int(sel[0])
            if index >= len(items):
                return None
            return items[index]

        def do_backup() -> None:
            suffix = simpledialog.askstring(
                "备份对话", "给这份快照起个后缀名（可留空，例如 before-upgrade）：", parent=dlg
            )
            if suffix is None:
                return
            args = argparse.Namespace(lane=lane, name=(suffix.strip() or None))
            self.run_job(f"备份对话：{lane}",
                         lambda: core.cmd_chat_backup(core.load_config(), args),
                         on_done=load)

        def do_restore() -> None:
            target = picked()
            if not target:
                return
            if not target["ok"]:
                messagebox.showwarning("备份对话", "这份快照不完整（缺 manifest.json），不能用", parent=dlg)
                return
            running = bool(core.lane_runtime(cfg, lane))
            question = (
                f"把对话记录恢复到「{target['name']}」？\n\n"
                f"· 快照里 {target['sessions']} 个会话文件 / {core.human_size(target['bytes'])}\n"
                "· 先校验快照自身，再把现在的记录另存一份（恢复错了能回来）\n"
                "· 恢复完逐文件比对指纹"
            )
            if running:
                if not messagebox.askyesno(
                    "备份对话",
                    question + "\n\n这条 lane 正在运行：恢复会先把它停掉，之后请手动「打开」。继续？",
                    parent=dlg,
                ):
                    return
            elif not messagebox.askyesno("备份对话", question, parent=dlg):
                return
            args = argparse.Namespace(lane=lane, name=target["name"], stop=True, yes=True, force=False)
            self.run_job(f"恢复对话：{lane}",
                         lambda: core.cmd_chat_restore(core.load_config(), args),
                         on_done=load)

        def do_rm() -> None:
            target = picked()
            if not target:
                return
            if not messagebox.askyesno(
                "备份对话",
                f"删掉快照「{target['name']}」（{core.human_size(target['size'])}）？删了就没了。",
                parent=dlg,
            ):
                return
            args = argparse.Namespace(lane=lane, name=target["name"], yes=True)
            self.run_job(f"删除备份：{lane}",
                         lambda: core.cmd_chat_rm(core.load_config(), args),
                         on_done=load)

        ttk.Button(btns, text="关闭", command=dlg.destroy).pack(side="right")
        ttk.Button(btns, text="立即备份", command=do_backup).pack(side="left")
        ttk.Button(btns, text="恢复选中的", command=do_restore).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="删除选中的", command=do_rm).pack(side="left", padx=(8, 0))

    def sync_chat_dialog(self, lane: str) -> None:
        """把主 DSH（主要版本）的**对话**同步进这条 lane —— 只搬对话与附件，配置一律不动。

        为什么值得单独一个对话框：这是一个会**覆盖**东西的动作（同名会话里较旧的那份会被
        源里较新的顶掉），所以先把"会动多少"摆出来（预演），再让他决定；还默认帮他先存一份
        快照，同步错了能退回去。
        """
        cfg = core.load_config()
        core.sync_lane_versions(cfg)
        src = core.primary_lane(cfg)
        dlg = tk.Toplevel(self.root)
        dlg.title("同步对话")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 620, 460
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 70
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text=f"把主 DSH 的对话同步进「{lane}」", bg=BG, fg=TEXT,
                 font=(FONT_UI, 13, "bold")).pack(anchor="w", padx=20, pady=(16, 4))
        tk.Label(
            dlg,
            text=(
                f"源：主要版本「{src or '（还没设主要版本）'}」　→　目标：「{lane}」\n"
                "只搬对话：会话正文、会话列表用的投影记录、工作区登记、附件。\n"
                "密钥 / 设置 / 插件 / 插件数据 / agent 预设一律不动。\n"
                "规则：缺的补上；同名会话按修改时间较新的赢（覆盖了几条会打在日志里）。"
            ),
            bg=BG, fg=MUTED, font=(FONT_UI, 9), justify="left", wraplength=570,
        ).pack(anchor="w", padx=20)

        preview = tk.Label(dlg, text="正在算会同步什么…", bg=CARD, fg=GRAY, font=(FONT_UI, 9),
                           justify="left", anchor="w", wraplength=560, height=6)
        preview.pack(fill="x", padx=20, pady=(10, 0))

        backup_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            dlg, text="同步前先给这条 lane 的对话存一份快照（同步错了能退回去）",
            variable=backup_var, bg=BG, fg=TEXT, selectcolor=CARD,
            activebackground=BG, activeforeground=TEXT, font=(FONT_UI, 9),
            highlightthickness=0, cursor="hand2", anchor="w", justify="left",
        ).pack(anchor="w", padx=20, pady=(10, 0))
        if core.lane_runtime(cfg, lane):
            tk.Label(dlg, text="这条 lane 正在运行：同步会先把它停掉（之后请手动「打开」）",
                     bg=BG, fg=AMBER, font=(FONT_UI, 9)).pack(anchor="w", padx=20, pady=(6, 0))

        def load_plan() -> None:
            try:
                plan = core.sync_chat_plan(core.load_config(), src, lane) if src else None
                if plan is None:
                    text, fg = "还没有主要版本 —— 先在别的卡片上点「★ 设为主要版本」", AMBER
                elif plan["problems"]:
                    text, fg = "\n".join(plan["problems"]), RED
                else:
                    files = plan["new_files"] + plan["over_files"]
                    ws = plan["workspace"] or {}
                    text = (
                        f"会话：新增 {len(plan['sessions_new'])} 个 / 覆盖 {len(plan['sessions_over'])} 个"
                        f"（按修改时间较新的赢）\n"
                        f"文件：要复制 {files} 个 / {core.human_size(plan['bytes'])}"
                        f"（跳过 {plan['skip_files']} 个一样新或更旧的）\n"
                        f"工作区登记：新增工作区 {ws.get('new_workspaces', 0)} 个 / "
                        f"补进会话 id {ws.get('new_sessions', 0)} 个"
                    )
                    fg = GRAY if files else MUTED
                    if not files and not ws.get("changed"):
                        text += "\n（已经是最新的，没有要同步的东西）"
            except Exception as exc:  # noqa: BLE001
                text, fg = f"算不出来：{type(exc).__name__}: {exc}", RED

            def apply() -> None:
                try:
                    if dlg.winfo_exists():
                        preview.configure(text=text, fg=fg)
                except tk.TclError:
                    pass

            self.root.after(0, apply)

        threading.Thread(target=load_plan, daemon=True).start()

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(12, 16), side="bottom")

        def start() -> None:
            # Tk 变量只在主线程读
            ns = argparse.Namespace(lane=lane, from_lane=None, dry_run=False,
                                    backup=backup_var.get(), stop=True, yes=True)
            dlg.destroy()
            self.run_job(f"同步对话：{src} → {lane}",
                         lambda: core.cmd_chat_sync(core.load_config(), ns))

        def dry() -> None:
            ns = argparse.Namespace(lane=lane, from_lane=None, dry_run=True,
                                    backup=False, stop=False, yes=True)
            dlg.destroy()
            self.run_job(f"预演同步对话：{src} → {lane}",
                         lambda: core.cmd_chat_sync(core.load_config(), ns))

        self.button(btns, "开始同步", start, primary=True).pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)
        self.button(btns, "先预演一次（不写文件）", dry).pack(side="left")

    def new_version_dialog(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("新建版本")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        w, h = 540, 660
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 90
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        dlg.grab_set()

        tk.Label(dlg, text="新建一条 lane", bg=BG, fg=TEXT, font=(FONT_UI, 13, "bold")).pack(
            anchor="w", padx=20, pady=(16, 2)
        )
        tk.Label(
            dlg,
            text="会把该版本的 DSH 装进独立目录（不覆盖你正在用的版本），并分配独立 DSH_HOME 与端口。",
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 9),
            wraplength=470,
            justify="left",
        ).pack(anchor="w", padx=20)

        # API key 继承说明：只在创建时复制一次（新 lane 没有别的配置可继承）
        cfg_now = core.load_config()
        top_lane = core.primary_lane(cfg_now)
        refs: dict = {}
        if top_lane:
            top_home = (cfg_now["lanes"].get(top_lane) or {}).get("home") or ""
            refs = core.read_credential_refs(top_home)
            if refs:
                key_text = f"将从主要版本「{top_lane}」复制：{'、'.join(sorted(refs))}"
                key_text_on = (f"不继承 —— 主要版本「{top_lane}」的 "
                               f"{'、'.join(sorted(refs))} 不会带过来，首次启动时自己填")
                key_bg, key_fg = _T.brand_wash, ACCENT_DK
            else:
                key_text = f"主要版本「{top_lane}」的 HOME 里还没有 API key，这次复制不到东西"
                key_text_on = f"不继承 —— 主要版本「{top_lane}」本来也没有 key，首次启动时自己填"
                key_bg, key_fg = AMBER_BG, AMBER
        else:
            key_text = "还没有主要版本 —— 新 lane 首次启动时要自己填 API key"
            key_text_on = "不继承 —— 首次启动时自己填"
            key_bg, key_fg = AMBER_BG, AMBER
        keybox = tk.Frame(dlg, bg=key_bg)
        keybox.pack(fill="x", padx=20, pady=(8, 0))
        key_label = tk.Label(
            keybox,
            text="API key：" + key_text,
            bg=key_bg,
            fg=key_fg,
            font=(FONT_UI, 9),
            wraplength=460,
            justify="left",
        )
        key_label.pack(anchor="w", padx=10, pady=(7, 0))
        key_note = tk.Label(
            keybox,
            text="（只在创建时复制这一次；以后要换 key 就各自改各自的 HOME，互不影响）",
            bg=key_bg,
            fg=MUTED,
            font=(FONT_UI, 8),
            wraplength=460,
            justify="left",
        )
        key_note.pack(anchor="w", padx=10, pady=(0, 7))

        no_inherit_var, _ = self.no_inherit_row(
            dlg,
            hint_off="（默认＝照老规矩，把主要版本的 API key 复制这一次。新建本来也只继承这一项）",
            hint_on="（勾上就什么都不继承 —— 新 lane 首启自己填 key，以后想补："
                    "卡片上的「补齐 API key…」）",
        )

        def apply_key_text(*_a) -> None:
            """勾了「不继承」就把上面那条「会复制 key」的说明同步改成「不会复制」。"""
            on = no_inherit_var.get()
            try:
                if on:
                    keybox.configure(bg=AMBER_BG)
                    key_label.configure(text="API key：" + key_text_on, bg=AMBER_BG, fg=AMBER)
                    key_note.configure(text="（这次不带任何密钥与设置；不影响它的插件与对话）",
                                       bg=AMBER_BG, fg=MUTED)
                else:
                    keybox.configure(bg=key_bg)
                    key_label.configure(text="API key：" + key_text, bg=key_bg, fg=key_fg)
                    key_note.configure(
                        text="（只在创建时复制这一次；以后要换 key 就各自改各自的 HOME，互不影响）",
                        bg=key_bg, fg=MUTED)
            except tk.TclError:
                pass

        no_inherit_var.trace_add("write", apply_key_text)

        reg_var, reg_remember = self.registry_row(dlg, cfg_now, pady=(12, 0))

        form = tk.Frame(dlg, bg=BG)
        form.pack(fill="x", padx=20, pady=14)
        form.columnconfigure(1, weight=1)

        tk.Label(form, text="名称", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(row=0, column=0, sticky="w", pady=6)
        name_var = tk.StringVar(value="")
        name_entry = ttk.Entry(form, textvariable=name_var)
        name_entry.grid(row=0, column=1, sticky="ew", padx=(10, 0), pady=6)

        tk.Label(form, text="版本", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(row=1, column=0, sticky="w", pady=6)
        ver_var = tk.StringVar(value="")
        self.ver_combo = ttk.Combobox(form, textvariable=ver_var, values=[], width=30)
        self.ver_combo.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=6)

        tk.Label(form, text="端口", bg=BG, fg=TEXT, font=(FONT_UI, 10)).grid(row=2, column=0, sticky="w", pady=6)
        port_var = tk.StringVar(value="")
        ttk.Entry(form, textvariable=port_var, width=12).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=6)

        hint = tk.Label(dlg, text="正在查询 npm 上可用的版本…", bg=BG, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        hint.pack(fill="x", padx=20)

        try:
            port_var.set(str(core.pick_port(core.load_config())))
        except Exception:
            port_var.set("")

        def base_name(ver: str) -> str:
            tags = {"0.1.7-rc.2": "next", "0.1.5-rc.3": "stable"}
            return tags.get(ver, "lane-" + re.sub(r"[^A-Za-z0-9]+", "-", ver).strip("-")[:12])

        def on_pick(_e=None) -> None:
            text = ver_var.get().strip()
            ver = text.split()[0] if text else ""
            if ver and not name_var.get().strip():
                name_var.set(base_name(ver))

        self.ver_combo.bind("<<ComboboxSelected>>", on_pick)

        def load_versions(choice: str) -> None:
            try:
                plan = core.registry_plan(core.load_config(), choice)
                packument, reg = core.fetch_index(core.load_config(), registries=plan)
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: hint.configure(text=f"查询失败：{exc}（可直接手输版本号）", fg=RED))
                return
            tags = packument.get("dist-tags", {})
            tag_of: dict[str, list[str]] = {}
            for tag, ver in tags.items():
                tag_of.setdefault(ver, []).append(tag)
            versions = core.sorted_versions(packument)
            values = [f"{v}    ({','.join(tag_of[v])})" if v in tag_of else v for v in reversed(versions)]
            summary = "  ".join(f"{k}={v}" for k, v in sorted(tags.items()))

            def apply() -> None:
                self.ver_combo.configure(values=values)
                if values and not ver_var.get().strip():
                    ver_var.set(values[0])
                    on_pick()
                hint.configure(text=f"来源 {reg}｜共 {len(versions)} 个版本｜{summary}", fg=MUTED)

            self.root.after(0, apply)

        # 换源就重查一次（Tk 变量只在主线程读）
        def reload_versions(*_a) -> None:
            hint.configure(text="正在查询 npm 上可用的版本…", fg=MUTED)
            choice = reg_var.get()
            threading.Thread(target=load_versions, args=(choice,), daemon=True).start()

        def do_create() -> None:
            lane = name_var.get().strip()
            ver_text = ver_var.get().strip()
            ver = ver_text.split()[0] if ver_text else ""
            if not LANE_NAME_RE.match(lane):
                messagebox.showwarning("名称不合法", "名称只能用字母/数字/._-，且以字母或数字开头。", parent=dlg)
                return
            if not ver:
                messagebox.showwarning("缺少版本", "请选择或输入一个版本号（如 0.1.7-rc.2 或 next）。", parent=dlg)
                return
            if lane in core.load_config().get("lanes", {}):
                if not messagebox.askyesno("已存在", f"lane「{lane}」已存在，要覆盖它的版本吗？", parent=dlg):
                    return
            port_txt = port_var.get().strip()
            port = int(port_txt) if port_txt.isdigit() else None
            args = argparse.Namespace(lane=lane, version=ver, port=port, force=True, follow=True,
                                      registry=reg_var.get(),
                                      no_inherit=no_inherit_var.get())
            self._save_registry_choice(reg_var, reg_remember)

            def job() -> None:
                core.cmd_create(core.load_config(), args)

            dlg.destroy()
            self.run_job(f"新建 {lane}（{ver}）", job)

        btns = tk.Frame(dlg, bg=BG)
        btns.pack(fill="x", padx=20, pady=(4, 16), side="bottom")
        self.button(btns, "开始安装", do_create, primary=True).pack(side="right")
        self.button(btns, "取消", dlg.destroy).pack(side="right", padx=6)
        self.button(btns, "重新查询版本", reload_versions).pack(side="left")

        reg_var.trace_add("write", reload_versions)
        reload_versions()

    # ── 主题切换：在同一个窗口里重建 ─────────────────────────────────────────
    def toggle_theme(self) -> None:
        """浅海 ↔ 深海：记住选择，然后**原地**重建一遍界面。

        为什么是重建而不是就地改色：Tk 没有样式层，颜色在控件创建时就定死了，
        逐个 configure 既容易漏、又容易把状态搞乱 —— 这一条没变。
        变的是重建的粒度：以前是"写配置 → 销毁窗口 → main() 的循环再开一个进程"，
        实测一次 ~1.2 秒，而且中途那一帧会被看到（海面铺好了、卡片还没长出来）。
        现在只重建窗口里的三块，进程 / 窗口位置 / 分栏比例 / 日志内容全都不动。
        """
        if self._switching:
            return
        want = "dark" if ui.theme_name() == "light" else "light"
        try:
            cfg = core.load_config()
            u = dict(cfg.get("ui") or {})
            u["theme"] = want
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w >= 400 and h >= 300:            # 顺带记住当前窗口位置，下次开窗不跳
                u.update({"w": w, "h": h, "x": self.root.winfo_x(), "y": self.root.winfo_y()})
            cfg["ui"] = u
            core.save_config(cfg)
        except Exception:
            pass
        self._rebuild_ui(want)

    def _rebuild_ui(self, want: str) -> None:
        """原地换主题：新版先在**海水底下**建好，最后一次性翻上来。

        为什么不能直接"销毁重建"：只要中间调过一次 update_idletasks（量尺寸要用），
        Tk 就会把当时的半成品推上屏 —— 用户看到的"切换时闪一下"就是这一帧。
        这里的做法是双缓冲：新的一套建在一个临时 Frame 里，Frame 先 lower() 到
        海面画布底下（尺寸、布局、绘制全都照算，但被不透明的海面盖住看不见），
        等布局稳定了再 lift()，下一次重绘就是老界面 → 新界面，中间没有别的。

        注意：**不要在这里强制重画卡片**（曾经加过一步"立刻 flush 所有卡片"，
        想省掉 60ms 的去抖，结果在几何计算过程中调 `itemconfigure`，卡片的内嵌
        Frame 被挪出了可视区 —— 换完主题左列整片空白）。去抖那 60ms 肉眼看不出来。
        """
        self._switching = True
        try:
            keep = self._capture_view()
            self._stop_jobs()
            apply_theme(want)                     # 模块级色常量 + ui_kit 里的主题
            set_dark_titlebar(self.root, want == "dark")
            host = tk.Frame(self.root, bg=BG, bd=0, highlightthickness=0)
            host.place(x=0, y=0, relwidth=1.0, relheight=1.0)
            host.lower()                          # ← 藏到海面底下（这一刻屏幕不变）
            self._build_ui(host)
            self.root.update_idletasks()          # 量真实尺寸（画在海面底下，看不见）

            self._split_frac = keep["split"] or SPLIT_DEFAULT
            self._split_retry = 0
            self._restore_view(keep)              # 日志 / 状态栏 / 远程版本行 / 分栏
            self.refresh_lanes(force=True)        # lane 卡片全部重建
            self.root.update_idletasks()
            self._apply_split()

            host.lift()                           # ← 一次性翻上来
            self.root.update_idletasks()
            for w in keep["old"]:
                try:
                    w.destroy()
                except tk.TclError:
                    pass
            if keep["autoscroll"]:
                self.log_text.see("end")
            else:
                self.log_text.yview_moveto(keep["yview"])
            self.root.update_idletasks()
        finally:
            self._switching = False
        # _stop_jobs 把定时任务全取消了，这里按建窗时一样的节奏重新挂上
        self._poll_id = self.root.after(120, self._poll_queue)
        self._tick_id = self.root.after(2500, self._tick)

    def _capture_view(self) -> dict:
        """换主题前记下"现场"：老控件、日志全文（连颜色）、分栏比例、状态栏文字。"""

        def txt(widget) -> str:
            try:
                return str(widget.cget("text"))
            except tk.TclError:
                return ""

        view = {
            "old": list(self.root.winfo_children()),
            "split": self._split_frac_now() or self._split_frac,
            "status": txt(self.status),
            "busy": txt(self.busy_label),
            "counts": txt(self.counts),
            "log_count": txt(self.log_count),
            "autoscroll": bool(getattr(self, "_autoscroll", True)),
            "back": bool(self._back_btn.winfo_manager()),
            "yview": 0.0,
            "remote": (self._remote_payload, self._remote_busy, self._remote_at),
            "log": [],
        }
        try:
            view["yview"] = float(self.log_text.yview()[0])
            # dump 出来的日志带 tagon/tagoff 和原文，原样塞回去时间戳和颜色都不变
            view["log"] = self.log_text.dump("1.0", "end-1c", text=True, tag=True)
        except tk.TclError:
            pass
        return view

    def _restore_view(self, view: dict) -> None:
        self._load_log(view["log"])
        try:
            self.status.configure(text=view["status"])
            self.busy_label.configure(text=view["busy"])
            self.counts.configure(text=view["counts"])
            self.log_count.configure(text=view["log_count"])
        except tk.TclError:
            pass
        payload, busy, at = view["remote"]
        if payload is not None:                   # 顶部「npm 上：…」那行，别退回"正在查询…"
            self._remote_payload = payload
            self._remote_busy = busy
            self._remote_at = at
            try:
                self._render_remote()
            except Exception:
                pass
        self._autoscroll = view["autoscroll"]
        if not view["autoscroll"] and view["back"]:
            try:
                self._back_btn.place(relx=1.0, rely=1.0, x=-28, y=-14, anchor="se")
            except tk.TclError:
                pass

    def _load_log(self, dump) -> None:
        """把 Text.dump 出来的日志塞回新控件（颜色/时间戳不变，不重新打时间）。"""
        if not dump:
            return
        text = self.log_text
        text.configure(state="normal")
        text.delete("1.0", "end")
        active: list[str] = []
        for kind, value, _idx in dump:
            if kind == "tagon":
                active.append(value)
            elif kind == "tagoff":
                if value in active:
                    active.remove(value)
            elif kind == "text":
                text.insert("end", value, tuple(active))
        text.configure(state="disabled")
        self._hide_log_hint()

    def _save_geometry(self) -> None:
        try:
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w < 400 or h < 300:  # 最小化/异常状态下不记
                return
            cfg = core.load_config()
            # 保留 ui 里的其它键（主题、分栏比例都存在这里）：整体覆盖会把它们抹掉
            u = dict(cfg.get("ui") or {})
            u.update({"w": w, "h": h, "x": self.root.winfo_x(), "y": self.root.winfo_y()})
            frac = self._split_frac_now()
            if frac:
                u["split"] = round(frac, 4)
            cfg["ui"] = u
            core.save_config(cfg)
        except Exception:
            pass

    def _stop_jobs(self) -> None:
        """销毁窗口之前，把挂着的定时任务取消掉。

        不取消的话，`after` 排着的回调在窗口销毁后仍会被 Tcl 触发一次，
        控制台上就是一串 `invalid command name "..._poll_queue"` —— 换主题时
        每次重建都会刷一遍（不是崩溃，但很难看）。
        """
        for attr in ("_poll_id", "_tick_id", "_split_id", "_list_bg_job", "_remote_job",
                     "_titlebar_job", "_align_job", "_redraw_job", "_layout_job"):
            job = getattr(self, attr, None)
            setattr(self, attr, None)
            if job is None:
                continue
            try:
                self.root.after_cancel(job)
            except Exception:
                pass

    def _on_close(self) -> None:
        """关窗口就只是关窗口 —— 绝不顺手停掉正在跑的 DSH。

        以前这里会弹「是＝顺手停掉它们并退出」，而 Windows 的 askyesnocancel 默认焦点在"是"，
        于是点 X 再按一下回车就把 `global`（你正在用的那套）给停了。想停请用顶部「全部停止」。
        """
        running = [n for n, v in (self._snapshot(force=True)["states"] or {}).items() if v]
        if running:
            self._log_lines(
                [
                    (
                        f"已关闭启动器窗口。以下实例仍在后台运行（下次打开会自动认回来）："
                        f"{'、'.join(running)}",
                        "muted",
                    )
                ]
            )
        self._save_geometry()
        self._stop_jobs()
        self.root.destroy()


def _safe_std_streams() -> None:
    """pythonw（无控制台）下 sys.stdout/stderr 是 None，任何 print 都会抛异常。
    核心模块是靠 print 输出日志的，所以必须先补上兜底流。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass


def _set_window_icon(root: tk.Tk) -> None:
    """窗口标题栏 / 任务栏图标（`assets\\app.ico`）。

    为什么还得单独设一次：PyInstaller 的 `--icon` 只换 **exe 文件本身**的图标
    （资源管理器里看到的那个），窗口运行时用的是 Tk 默认的羽毛 —— 任务栏上就是一根羽毛。
    图标放在 `assets\\`（源码目录和 exe 目录都认），没有就静默退回默认图标。
    """
    for base in (getattr(core, "SCRIPT_DIR", None), HERE):
        if not base:
            continue
        ico = Path(base) / "assets" / "app.ico"
        try:
            if ico.exists():
                root.iconbitmap(default=str(ico))
                return
        except Exception:
            continue


# 界面上的每个入口都必须真的挂在 LauncherApp 上。
# 为什么专门查这个：缩进 / 注释写错时，一个 def 可能被"吃掉"变成上一个函数的**局部**函数，
# 语法照样合法、窗口照样能建出来，只有点那个按钮时才炸（真的踩过一次：
# `def delete_lane` 被并进注释，整段落进 sync_key 里，而 --selfcheck 报的是 OK）。
_REQUIRED_ACTIONS = (
    "refresh_lanes",
    "manual_refresh",
    "refresh_remote",
    "open_repo",
    "open_lane",
    "stop_lane",
    "stop_all",
    "show_lane_log",
    "set_primary_lane",
    "sync_key",
    "delete_lane",
    "clone_lane",
    "upgrade_dialog",
    "upgrade_in_terminal",
    "verify_lane",
    "market_dialog",
    "chat_dialog",
    "sync_chat_dialog",
    "new_version_dialog",
    "registry_row",
    "_save_registry_choice",
    "no_inherit_row",
    "adopt_install",
    "kill_instance",
    "open_url",
    "copy_text",
    "_snapshot",
    "_invalidate",
    "_on_close",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH 多版本启动器（图形界面版）")
    parser.add_argument("--selfcheck", action="store_true", help="构建窗口并刷新一次后退出（自动化自检）")
    parser.add_argument("--theme", choices=("light", "dark"), help="临时指定主题（不写进配置）")
    args = parser.parse_args()

    _safe_std_streams()
    enable_dpi_awareness()
    apply_theme(args.theme or _saved_theme())

    # 窗口只建一次。换主题不再重启进程，而是 LauncherApp._rebuild_ui 原地重建
    # （旧写法是 destroy 之后 while 循环再建一个 —— 一次 1.2 秒，还会露出半成品的一帧）。
    root = tk.Tk()
    _set_window_icon(root)
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    set_dark_titlebar(root, ui.theme_name() == "dark")
    try:
        app = LauncherApp(root)
    except Exception as exc:  # noqa: BLE001
        # 绝不留一个"没有 mainloop 的半成品窗口"：那种窗口既不能正常用、也关不掉
        # （2026-09-27 的故障就是这样：__init__ 里写运行态被拒 → 弹错误框 → 窗口挂着不动）。
        detail = traceback.format_exc()
        try:
            messagebox.showerror(
                "DSH 多版本启动器 · 启动失败",
                f"{type(exc).__name__}: {exc}\n\n"
                f"{detail[-1200:]}\n"
                "窗口已关闭；完整信息也写到了 stderr（命令行里跑能看到）。",
            )
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        print(detail, file=sys.stderr, flush=True)
        return 1

    if args.selfcheck:
        root.withdraw()
        root.update()
        root.update_idletasks()
        missing = [m for m in _REQUIRED_ACTIONS if not callable(getattr(app, m, None))]
        lanes = core.load_config().get("lanes", {})
        if missing:
            print(f"[XX] selfcheck 失败：LauncherApp 上找不到这些方法：{'、'.join(missing)}")
            root.destroy()
            return 1
        print(
            f"selfcheck OK：窗口构建成功，{len(_REQUIRED_ACTIONS)} 个界面入口齐全，"
            f"lane 数量 {len(lanes)}：{', '.join(lanes) or '(无)'}"
            f"，主题 {ui.theme_name()}"
        )
        root.destroy()
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
