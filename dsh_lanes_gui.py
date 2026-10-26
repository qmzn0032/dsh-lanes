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

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

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
        self.restart = False      # 主题切换置 True：main() 见它就把窗口重建成新主题
        self.cfg = core.load_config()

        root.title("DSH 多版本启动器")
        root.configure(bg=BG)
        ui = self.cfg.get("ui") or {}
        if ui.get("w") and ui.get("h"):
            root.geometry(f"{int(ui['w'])}x{int(ui['h'])}+{int(ui.get('x', 80))}+{int(ui.get('y', 60))}")
        else:
            self._center(1060, 720)
        root.minsize(880, 560)
        self._style()

        self._build_header()
        self._build_body()
        self._build_status()

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
        self.root.after(120, self._poll_queue)
        self.root.after(200, lambda: self.refresh_remote(force=True))  # 不挡开窗，查完再贴上去
        self.root.after(2000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", lambda _e: self.manual_refresh())
        self.root.bind("<Control-l>", lambda _e: self.clear_log())
        self.root.bind("<Control-n>", lambda _e: self.new_version_dialog())
        self.log("欢迎使用 DSH 多版本启动器。左边是各版本 lane，右边是实时日志。", "head")
        self.log("快捷键：F5 刷新　Ctrl+L 清空日志　Ctrl+N 新建版本　双击卡片＝打开　点地址＝复制", "muted")
        self.log("标题下面那行的「√」＝本机已经装了这个版本；点「官方代码库 ↗」直接去 dsh 的仓库。", "muted")

        # 根目录不可写时说清楚（不是不能用，而是"记不住 + 部分命令会失败"），
        # 别让它以"某个操作突然报错"的形式冒出来。
        root_ok, root_detail = core.lane_root_writable(self.cfg)
        if not root_ok:
            self.log(f"[!!] lane 根目录不可写：{root_detail}", "warn")
            self.log(
                "     卡片和打开/停止仍可用（状态现场探测）；但运行态记不住，"
                "克隆 / 删除 / 升级这些要写目录的命令会失败。",
                "warn",
            )

        # 受限令牌（沙箱）是最容易看错的一种失败：文件权限、icacls 全都正常，
        # 但写文件/结束进程一律被拒。直接点名，省得用户去查 D 盘权限。
        tok = core.token_state()
        if tok.get("restricted"):
            self.log("[!!] 这个窗口被关在受限沙箱里（restricted token）——不是 D 盘权限问题", "warn")
            self.log(
                "     表现：写不了 D:\\dsh-lanes、停不掉别的进程、打开 lane 也起不来。",
                "warn",
            )
            self.log(
                "     原因：它是从 DSH 的终端 / 工具 / 沙箱里拉起来的，令牌被裁剪过。",
                "warn",
            )
            self.log(
                "     解法：关掉这个窗口，去资源管理器双击 exe（或桌面快捷方式）。",
                "warn",
            )

    # ── 基础 ────────────────────────────────────────────────────────────────
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
        shell = tk.Frame(self.root, bg=CARD)
        shell.pack(fill="x")
        tk.Frame(shell, bg=BORDER, height=1).pack(side="bottom", fill="x")
        head = tk.Frame(shell, bg=CARD)
        head.pack(fill="x", padx=18, pady=(12, 10))

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

    # ── 主体 ────────────────────────────────────────────────────────────────
    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=18, pady=(12, 8))
        body.columnconfigure(0, weight=3, minsize=430)
        body.columnconfigure(1, weight=2, minsize=330)
        body.rowconfigure(0, weight=1)

        # 左：lane 列表（可滚动）
        left_wrap = tk.Frame(body, bg=BG)
        left_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_wrap.rowconfigure(0, weight=1)
        left_wrap.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(left_wrap, bg=BG, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb = ui.MiniScrollbar(left_wrap, command=self.canvas.yview, bg=BG)
        sb.grid(row=0, column=1, sticky="ns", padx=(2, 0))
        self.canvas.configure(yscrollcommand=sb.set)

        self.list_frame = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind(
            "<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind(
            "<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width)
        )
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        # 右：日志 —— 圆角面板 + 时间戳列 + 级别标记 + 空状态 + 自动滚动
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)

        bar = tk.Frame(right, bg=BG)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        tk.Label(bar, text="实时日志", bg=BG, fg=TEXT, font=(FONT_UI, 11, "bold")).pack(side="left")
        self.log_count = tk.Label(bar, text="", bg=BG, fg=ui.SUBTLE, font=(FONT_UI, 9))
        self.log_count.pack(side="left", padx=(8, 0))
        self.button(bar, "清空", self.clear_log, variant="ghost", size="sm").pack(side="right")
        self.button(bar, "复制全部", self.copy_log, variant="ghost", size="sm").pack(
            side="right", padx=4
        )

        panel = ui.RoundedCard(
            right, bg=CARD, border=BORDER, radius=10,
            padx=4, pady=4, fill=True, width=1, height=1,
        )
        panel.grid(row=1, column=0, sticky="nsew")
        wrap = panel.inner
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        # 日志排版：时间戳小一号、级别字形一列、正文 10pt；折行用 lmargin2 缩进到正文起点。
        self._f_log = tkfont.Font(family=FONT_MONO, size=10)
        self._f_ts = tkfont.Font(family=FONT_MONO, size=9)
        self._f_log_b = tkfont.Font(family=FONT_MONO, size=10, weight="bold")
        self._log_indent = self._f_ts.measure("00:00:00 ") + self._f_log.measure(_LOG_GLYPH_W)

        self.log_text = tk.Text(
            wrap,
            width=1,  # 关键：默认 80 字符会撑爆右列、把左列压扁（grid 先满足请求宽度再分权重）
            height=1,
            bg=CARD,
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
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_sb = ui.MiniScrollbar(wrap, command=self.log_text.yview, bg=CARD)
        self.log_sb.grid(row=0, column=1, sticky="ns", padx=(0, 4))
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
        self._log_hint = tk.Frame(wrap, bg=CARD)
        tk.Label(self._log_hint, text="还没有日志", bg=CARD, fg=TEXT,
                 font=(FONT_UI, 11, "bold")).pack()
        tk.Label(
            self._log_hint,
            text="打开任意版本、刷新，或做一次操作，\n输出会实时出现在这里。",
            bg=CARD, fg=MUTED, font=(FONT_UI, 9), justify="center",
        ).pack(pady=(5, 11))
        keys = tk.Frame(self._log_hint, bg=CARD)
        keys.pack()
        for key_text in ("F5 刷新", "Ctrl+L 清空", "Ctrl+N 新建"):
            ui.Chip(keys, key_text, "key", bg=CARD).pack(side="left", padx=3)
        self._show_log_hint()

        # 自动滚动：手动往上翻就停住，右下角浮出「回到最新」，而不是每来一行把你拽回底部
        self._autoscroll = True
        self._back_btn = self.button(
            wrap, "↓ 回到最新", self._scroll_to_bottom, variant="primary", size="xs"
        )
        T.bind("<MouseWheel>", self._on_log_wheel, add="+")
        for seq in ("<ButtonRelease-1>", "<B1-Motion>", "<KeyRelease>"):
            T.bind(seq, lambda _e: self.root.after(20, self._sync_autoscroll), add="+")

    def _build_status(self) -> None:
        # 底部状态条：和顶部一样是一条白带 + 上边框，收住整个窗口
        shell = tk.Frame(self.root, bg=CARD)
        shell.pack(fill="x")
        tk.Frame(shell, bg=BORDER, height=1).pack(side="top", fill="x")
        bar = tk.Frame(shell, bg=CARD)
        bar.pack(fill="x", padx=18, pady=7)
        self.status = tk.Label(bar, text="就绪", bg=CARD, fg=MUTED, font=(FONT_UI, 9), anchor="w")
        self.status.pack(side="left")
        self.busy_label = tk.Label(bar, text="", bg=CARD, fg=ACCENT, font=(FONT_UI, 9, "bold"))
        self.busy_label.pack(side="right")
        self.counts = tk.Label(bar, text="", bg=CARD, fg=MUTED, font=(FONT_UI, 9))
        self.counts.pack(side="right", padx=14)

    def _on_wheel(self, event) -> None:
        # 这个绑定是 bind_all 的，日志面板里滚轮也会冒过来：只有指针真在列表上才滚列表
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        while w is not None:
            if w is self.canvas:
                break
            w = getattr(w, "master", None)
        else:
            return
        try:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
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
            box = tk.Frame(self.list_frame, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
            box.pack(fill="x", pady=4)
            tk.Label(
                box,
                text="还没有任何 lane",
                bg=CARD,
                fg=TEXT,
                font=(FONT_UI, 11, "bold"),
            ).pack(anchor="w", padx=16, pady=(14, 2))
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
            ).pack(anchor="w", padx=16, pady=(0, 14))
        else:
            top_refs = (
                core.read_credential_refs((self.cfg["lanes"].get(top) or {}).get("home") or "")
                if top
                else {}
            )
            # core.ordered_lanes：主要版本永远第一
            for name, data in core.ordered_lanes(self.cfg):
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
                card.pack(fill="x", pady=4)

        self._build_foreign_section(snap)
        self._update_counts(snap)

    # ── 未登记实例 / 系统全局安装 ───────────────────────────────────────────
    def _section(self, text: str, count: int | None = None, action=None) -> None:
        """段落标题：卡片区和"下面的杂项"之间的分隔，比正文重、比卡片名轻。"""
        bar = tk.Frame(self.list_frame, bg=BG)
        bar.pack(fill="x", pady=(16, 6))
        if action is not None:
            self.link_button(bar, action[0], action[1]).pack(side="right")
        tk.Label(bar, text=text, bg=BG, fg=TEXT,
                 font=(FONT_UI, 10, "bold")).pack(side="left")
        if count:
            ui.Chip(bar, str(count), "ver", bg=BG).pack(side="left", padx=(7, 0))

    def _build_foreign_section(self, snap: dict) -> None:
        instances = snap["foreign"]
        # lane 自己的安装树不在这里重复列（上面已经有卡片了）
        installs = [i for i in snap["installs"] if not i["source"].startswith("lane ")]
        if not instances and not installs:
            return

        # 没有未登记实例时整段省略：原来会留一个标题 +「（当前没有别的 DSH 实例在跑）」，
        # 两行字只说明"什么都没发生"，纯属噪音。
        if instances:
            self._section("未登记的实例（不是本启动器启动的）", len(instances))
            for item in instances:
                card = ui.RoundedCard(
                    self.list_frame, bg=CARD, border=BORDER, radius=10, padx=13, pady=10,
                )
                card.pack(fill="x", pady=4)
                pad = card.inner

                btns = tk.Frame(pad, bg=CARD)
                btns.pack(side="right")
                self.button(btns, "打开页面", lambda u=item["url"]: self.open_url(u),
                            primary=True, size="sm").pack(side="left", padx=3)
                self.button(btns, "结束进程", lambda i=item: self.kill_instance(i),
                            danger=True, size="sm").pack(side="left", padx=3)

                ui.Pill(pad, "像是 DSH" if item["dsh_like"] else "占用中",
                        "warn" if item["dsh_like"] else "off",
                        bg=CARD).pack(side="right", padx=(0, 12))

                left = tk.Frame(pad, bg=CARD)
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
            self._section(
                "本机各套安装（你平时在用的那份在这里）",
                len(installs),
                action=("打开根目录", self.open_root_dir),
            )
            card = ui.RoundedCard(
                self.list_frame, bg=CARD, border=BORDER, radius=10, padx=13, pady=10,
            )
            card.pack(fill="x", pady=4)
            pad = card.inner
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
        self.root.after(2500, self._tick)

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
        self.root.after(120, self._poll_queue)

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

    # ── 关闭 ────────────────────────────────────────────────────────────────
    def toggle_theme(self) -> None:
        """浅海 ↔ 深海：存进 lanes.json → ui.theme，然后重建窗口（见 main 的重启循环）。

        为什么是重建而不是就地改色：Tk 没有样式层，颜色在控件创建时就定死；
        逐个控件 configure 一遍既容易漏，又会把状态搞乱。重建一整窗不到 1 秒。
        """
        want = "dark" if ui.theme_name() == "light" else "light"
        try:
            cfg = core.load_config()
            u = dict(cfg.get("ui") or {})
            u["theme"] = want
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w >= 400 and h >= 300:            # 顺带记住当前窗口位置，重建后不跳
                u.update({"w": w, "h": h, "x": self.root.winfo_x(), "y": self.root.winfo_y()})
            cfg["ui"] = u
            core.save_config(cfg)
        except Exception:
            pass
        self.restart = True
        self.root.after(40, self.root.destroy)

    def _save_geometry(self) -> None:
        try:
            w, h = self.root.winfo_width(), self.root.winfo_height()
            if w < 400 or h < 300:  # 最小化/异常状态下不记
                return
            cfg = core.load_config()
            # 保留 ui 里的其它键（主题就是存在这里的）：整体覆盖会把 ui.theme 抹掉
            u = dict(cfg.get("ui") or {})
            u.update({"w": w, "h": h, "x": self.root.winfo_x(), "y": self.root.winfo_y()})
            cfg["ui"] = u
            core.save_config(cfg)
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

    # 主题切换要重建整窗（Tk 没有样式层），所以主循环套一层：关窗 → 需要就再来一次。
    while True:
        apply_theme(args.theme or _saved_theme())
        root = tk.Tk()
        _set_window_icon(root)
        try:
            root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
        except Exception:
            pass
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
        again = bool(getattr(app, "restart", False))
        try:
            root.destroy()
        except Exception:
            pass
        if not again:
            return 0


if __name__ == "__main__":
    sys.exit(main())
