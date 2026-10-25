#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_kit 控件预览 —— 不启动启动器，单独看按钮长什么样。

用法
----
    py ui_kit_demo.py             # 打开预览窗口
    py ui_kit_demo.py --smoke     # 无窗口自检：建控件、点一遍回调、销毁

这个文件**只是预览**，不参与启动器运行；改 ui_kit.py 后重开就能看到变化。
点任意按钮，底部状态栏会显示点了哪个（验证 command 接线正确）。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import tkinter as tk

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import ui_kit as ui  # noqa: E402


def enable_dpi_awareness() -> None:
    """与 dsh_lanes_gui.enable_dpi_awareness 一致：高分屏下不发虚。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def section(parent, title: str, note: str = "") -> tk.Frame:
    """一个预览分组：小标题 + 说明 + 内容容器。"""
    wrap = tk.Frame(parent, bg=ui.CARD)
    wrap.pack(fill="x", padx=14, pady=(12, 0))
    head = tk.Frame(wrap, bg=ui.CARD)
    head.pack(fill="x")
    tk.Label(head, text=title, bg=ui.CARD, fg=ui.TEXT,
             font=(ui.FONT_UI, 10, "bold")).pack(side="left")
    if note:
        tk.Label(head, text="  " + note, bg=ui.CARD, fg=ui.MUTED,
                 font=(ui.FONT_UI, 9)).pack(side="left")
    body = tk.Frame(wrap, bg=ui.CARD)
    body.pack(fill="x", pady=(7, 0))
    return body


class Demo:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("ui_kit 控件预览")
        self.root.configure(bg=ui.BG)
        self.root.geometry("560x690+120+80")
        self.root.minsize(460, 600)
        self._hits: list[str] = []
        self._build()

    # ── 布局 ──
    def _build(self) -> None:
        shell = tk.Frame(self.root, bg=ui.BG)
        shell.pack(fill="both", expand=True, padx=16, pady=14)

        head = tk.Frame(shell, bg=ui.BG)
        head.pack(fill="x")
        tk.Label(head, text="ui_kit 控件预览", bg=ui.BG, fg=ui.TEXT,
                 font=(ui.FONT_UI, 15, "bold")).pack(side="left")
        ui.RoundedButton(head, "清空计数", self._reset, "ghost", "sm",
                         bg=ui.BG).pack(side="right")
        tk.Label(shell,
                 text="替换掉 tk.Button 之后的样子：圆角、悬停变色、按下下沉 1px 都是 Canvas 画的",
                 bg=ui.BG, fg=ui.MUTED, font=(ui.FONT_UI, 9)).pack(anchor="w", pady=(2, 8))

        card = tk.Frame(shell, bg=ui.CARD, highlightbackground=ui.BORDER,
                        highlightthickness=1)
        card.pack(fill="x")
        self._fill(card)

        self.status = tk.Label(shell, text="点一下任意按钮看看反馈 →", bg=ui.BG,
                               fg=ui.MUTED, font=(ui.FONT_UI, 9), anchor="w")
        self.status.pack(fill="x", pady=(8, 0))

    def _fill(self, box: tk.Frame) -> None:
        card = ui.CARD

        # ① 四种变体
        row = section(box, "四种变体", "primary / secondary / ghost / danger")
        ui.RoundedButton(row, "＋ 新建版本", lambda: self._hit("primary"),
                         "primary", "md", bg=card).pack(side="left", padx=(0, 8))
        ui.RoundedButton(row, "打开根目录", lambda: self._hit("secondary"),
                         "secondary", "md", bg=card).pack(side="left", padx=(0, 8))
        ui.RoundedButton(row, "环境自检", lambda: self._hit("ghost"),
                         "ghost", "md", bg=card).pack(side="left", padx=(0, 8))
        ui.RoundedButton(row, "■ 全部停止", lambda: self._hit("danger"),
                         "danger", "md", bg=card).pack(side="left")

        # ② 三种尺寸
        row = section(box, "三种尺寸", "md 30px / sm 26px / xs 22px")
        for sz in ("md", "sm", "xs"):
            ui.RoundedButton(row, "打开", lambda s=sz: self._hit(f"primary-{s}"),
                             "primary", sz, bg=card).pack(side="left", padx=(0, 8))
        for sz in ("md", "sm", "xs"):
            ui.RoundedButton(row, "日志", lambda s=sz: self._hit(f"secondary-{s}"),
                             "secondary", sz, bg=card).pack(side="left", padx=(0, 8))

        # ③ 图标 + 禁用
        row = section(box, "图标与禁用态", "图标是普通字符，跟文字同一字体测量宽度")
        ui.RoundedButton(row, "刷新", lambda: self._hit("icon-refresh"), "ghost", "md",
                         icon="⟳", bg=card).pack(side="left", padx=(0, 8))
        ui.RoundedButton(row, "复制", lambda: self._hit("icon-copy"), "ghost", "xs",
                         icon="⧉", bg=card).pack(side="left", padx=(0, 8))
        ui.RoundedButton(row, "停止", None, "danger", "sm", icon="■",
                         bg=card).pack(side="left", padx=(0, 8))
        b = ui.RoundedButton(row, "已禁用", None, "secondary", "sm", bg=card)
        b.configure(state="disabled")
        b.pack(side="left")

        # ④ 胶囊 + 芯片
        row = section(box, "状态胶囊", "on / off / warn")
        ui.Pill(row, "运行中", "on", bg=card).pack(side="left", padx=(0, 8))
        ui.Pill(row, "已停止", "off", bg=card).pack(side="left", padx=(0, 8))
        ui.Pill(row, "待核对", "warn", bg=card).pack(side="left")

        row = section(box, "标签芯片", "版本号 / ★主要版本 / 来源")
        ui.Chip(row, "0.2.1-rc.2", "ver", bg=card).pack(side="left", padx=(0, 6))
        ui.Chip(row, "★ 主要版本", "primary", bg=card).pack(side="left", padx=(0, 6))
        ui.Chip(row, "adopted", "soft", bg=card).pack(side="left")

        # ⑤ 真实卡片模拟
        tk.Frame(box, height=10, bg=card).pack(fill="x")
        tk.Label(box, text="真实场景：把上面这些拼成一张 lane 卡片",
                 bg=card, fg=ui.MUTED, font=(ui.FONT_UI, 9)).pack(anchor="w", padx=14)

        mock = tk.Frame(box, bg=card, highlightbackground=ui.ACCENT, highlightthickness=2)
        mock.pack(fill="x", padx=14, pady=(6, 14))

        r1 = tk.Frame(mock, bg=card)
        r1.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(r1, text="global", bg=card, fg=ui.TEXT,
                 font=(ui.FONT_UI, 13, "bold")).pack(side="left")
        ui.Chip(r1, "0.2.1-rc.2", "ver", bg=card).pack(side="left", padx=7)
        ui.Chip(r1, "★ 主要版本", "primary", bg=card).pack(side="left")
        ui.Pill(r1, "运行中", "on", bg=card).pack(side="right")

        r2 = tk.Frame(mock, bg=card)
        r2.pack(fill="x", padx=12, pady=(6, 0))
        tk.Label(r2, text="端口 3080   PID 7588", bg=card, fg=ui.SUBTLE,
                 font=(ui.FONT_MONO, 9)).pack(side="left")

        r3 = tk.Frame(mock, bg=card)
        r3.pack(fill="x", padx=12, pady=(8, 12))
        tk.Label(r3, text="http://127.0.0.1:3080/?token=cw6eqhrdCCDxxdr…Lf",
                 bg="#F7F9FF", fg="#3F5BD9", font=(ui.FONT_MONO, 9),
                 padx=8, pady=5).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ui.RoundedButton(r3, "复制", lambda: self._hit("card-复制"), "ghost", "xs",
                         icon="⧉", bg=card).pack(side="right")

        jb = tk.Frame(r2, bg=card)
        jb.pack(side="right")
        ui.RoundedButton(jb, "打开", lambda: self._hit("card-打开"), "primary", "sm",
                         bg=card).pack(side="left", padx=(6, 0))
        ui.RoundedButton(jb, "停止", lambda: self._hit("card-停止"), "danger", "sm",
                         bg=card).pack(side="left", padx=(6, 0))
        ui.RoundedButton(jb, "日志", lambda: self._hit("card-日志"), "secondary", "sm",
                         bg=card).pack(side="left", padx=(6, 0))

        # ⑥ 说明
        tk.Label(box, text="圆角：按钮 6~8px · 胶囊全圆 · 芯片 5px",
                 bg=card, fg=ui.SUBTLE, font=(ui.FONT_UI, 9)).pack(anchor="w", padx=14)
        tk.Label(box, text="悬停：次要/幽灵变淡灰底，主色加深；按下整体下沉 1px",
                 bg=card, fg=ui.SUBTLE, font=(ui.FONT_UI, 9)).pack(anchor="w", padx=14, pady=(0, 12))

    # ── 交互 ──
    def _hit(self, name: str) -> None:
        self._hits.append(name)
        tail = " · ".join(self._hits[-3:])
        self.status.configure(text=f"点了：{tail}（共 {len(self._hits)} 次）")

    def _reset(self) -> None:
        self._hits.clear()
        self.status.configure(text="点一下任意按钮看看反馈 →")

    def run(self) -> None:
        self.root.mainloop()


def smoke() -> int:
    """无窗口自检：建全部控件、点一遍、销毁。改完代码先跑这个。"""
    enable_dpi_awareness()
    app = Demo()
    app.root.update_idletasks()
    app.root.update()
    n = 0
    for w in _walk(app.root):
        if isinstance(w, ui.RoundedButton):
            w.invoke()
            n += 1
    app.root.update_idletasks()
    panel_ok = _smoke_panel(app)
    app.root.update_idletasks()
    app.root.destroy()
    print(f"SMOKE OK / buttons={n} / callbacks_fired={len(app._hits)}"
          f" / panel_scroll={'OK' if panel_ok else 'FAIL'}")
    return 0 if panel_ok else 1


def _smoke_panel(app) -> bool:
    """日志面板那套：fill 模式的圆角面板 + 极简滚动条（这俩不在预览窗口里，单独自检）。"""
    holder = tk.Frame(app.root, bg=ui.BG)
    holder.place(x=0, y=0, width=220, height=140)
    panel = ui.RoundedCard(holder, fill=True, width=1, height=1, padx=4, pady=4)
    panel.place(relwidth=1.0, relheight=1.0)
    panel.inner.rowconfigure(0, weight=1)
    panel.inner.columnconfigure(0, weight=1)
    txt = tk.Text(panel.inner, width=1, height=1, bd=0, highlightthickness=0)
    txt.grid(row=0, column=0, sticky="nsew")
    sb = ui.MiniScrollbar(panel.inner, command=txt.yview)
    sb.grid(row=0, column=1, sticky="ns")
    txt.configure(yscrollcommand=sb.set)
    app.root.update_idletasks()
    app.root.update()
    sb.set(0.2, 0.6)
    app.root.update_idletasks()
    app.root.update()
    ok = bool(sb.find_all()) and sb.get() == (0.2, 0.6)
    holder.destroy()
    return ok


def _walk(w):
    yield w
    for ch in w.winfo_children():
        yield from _walk(ch)


if __name__ == "__main__":
    if "--smoke" in sys.argv:
        raise SystemExit(smoke())
    enable_dpi_awareness()
    Demo().run()
