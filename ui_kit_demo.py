#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ui_kit_demo —— 「蓝色大肥鱼」控件库预览

跑法
----
    py ui_kit_demo.py                    # 打开窗口（浅海主题，右上角可切深海）
    py ui_kit_demo.py --theme dark       # 直接开深海
    py ui_kit_demo.py --smoke            # 无窗口自检：建控件、点一遍、销毁
    py ui_kit_demo.py --shot _ui-preview # 不弹窗，直接把两套主题导出成 PNG

预览里能看到什么
----------------
    按钮 5 变体 × 4 尺寸 × 5 状态（含禁用与键盘焦点环）
    胶囊 5 种语义（运行/停止/警告/错误/进行中）
    芯片 6 种（版本号 / 主版本 / 软标签 / 键位 / 已安装 / 警告）
    卡片三级投影 s1/s2/s3（悬停升一级）
    日志面板：fill 模式圆角面板 + 极简滚动条
    海面光晕背景：品牌蓝 30% / 青 26% / 薄荷 14%，同源 dafy-whale-theme
"""

from __future__ import annotations

import os
import sys
import time
import tkinter as tk

import theme_whale as tw
import ui_kit as ui

HERE = os.path.dirname(os.path.abspath(__file__))


def enable_dpi_awareness() -> None:
    """先声明 DPI 感知，否则高分屏上自绘图形发虚。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_whale(size: int = 44):
    """取品牌鲸鱼图。有 Pillow 就等比缩放，没有就整数降采样。"""
    path = os.path.join(HERE, "assets", "app.png")
    if not os.path.exists(path):
        return None
    try:
        from PIL import Image, ImageTk
        img = Image.open(path).convert("RGBA")
        img.thumbnail((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        try:
            ph = tk.PhotoImage(file=path)
            k = max(1, ph.width() // size)
            return ph.subsample(k) if k > 1 else ph
        except Exception:
            return None


class Demo:
    def __init__(self, theme: str = "light") -> None:
        self.theme = "dark" if str(theme).lower().startswith("d") else "light"
        ui.set_theme(self.theme)
        self.t = tw.palette(self.theme)
        self._hits: list = []
        self._refs: list = []

        self.root = tk.Tk()
        self.root.title("蓝色大肥鱼 · 控件库预览")
        self.root.configure(bg=self.t.bg_base)
        self.root.geometry("1180x704")
        self.root.minsize(980, 640)

        self._build()

    # ── 布局 ──
    def _build(self) -> None:
        t = self.t
        self._header()
        body = tk.Frame(self.root, bg=t.bg_base)
        body.pack(fill="both", expand=True, padx=tw.SP["lg"], pady=(tw.SP["md"], 0))
        body.columnconfigure(0, weight=1, uniform="c")
        body.columnconfigure(1, weight=1, uniform="c")

        left = tk.Frame(body, bg=t.bg_base)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, tw.SP["sm"]))
        right = tk.Frame(body, bg=t.bg_base)
        right.grid(row=0, column=1, sticky="nsew", padx=(tw.SP["sm"], 0))

        self._sec_buttons(left)
        self._sec_sizes(left)
        self._sec_states(left)
        self._sec_tokens(left)
        self._sec_pills(right)
        self._sec_chips(right)
        self._sec_cards(right)
        self._sec_log(right)

        foot = tk.Frame(self.root, bg=t.bg_base)
        foot.pack(fill="x", padx=tw.SP["lg"], pady=(tw.SP["sm"], tw.SP["md"]))
        self.status = tk.Label(
            foot, text="点一下任意按钮看看反馈 →", bg=t.bg_base, fg=t.label2,
            font=(ui.FONT_UI, tw.FS["sm"]), anchor="w",
        )
        self.status.pack(side="left")
        tk.Label(
            foot, text="键盘 Tab 可聚焦按钮，焦点环是品牌蓝 2px",
            bg=t.bg_base, fg=t.label2, font=(ui.FONT_UI, tw.FS["xs"]),
        ).pack(side="right")

    def _header(self) -> None:
        t = self.t
        h = 104
        cv = tk.Canvas(self.root, height=h, highlightthickness=0, bd=0, bg=t.bg_base)
        cv.pack(fill="x")
        self._head = cv
        self._head_done = None
        self._head_img_w = 0
        self._head_job = None
        cv.bind("<Configure>", lambda _e: self._schedule_head())

        whale = load_whale(46)
        x = tw.SP["xl"]
        if whale is not None:
            self._refs.append(whale)
            cv.create_image(x, h / 2 - 6, image=whale, anchor="w")
            x += 58
        cv.create_text(x, h / 2 - 12, text="蓝色大肥鱼", anchor="w", fill=t.label1,
                       font=(ui.FONT_UI, tw.FS["lg"], "bold"))
        cv.create_text(x, h / 2 + 12, text="DSH 多版本启动器 · 界面控件预览",
                       anchor="w", fill=t.label2, font=(ui.FONT_UI, tw.FS["sm"]))
        cv.create_text(980, h / 2, text=f"{self.theme} / {'浅海' if self.theme == 'light' else '深海'}",
                       anchor="e", fill=t.brand, font=(ui.FONT_UI, tw.FS["sm"], "bold"))

        holder = tk.Frame(cv, bg=t.bg_base)
        btns = [
            ui.RoundedButton(holder, text="浅海", variant=("primary" if self.theme == "light" else "secondary"),
                             size="sm", command=lambda: self._switch("light")),
            ui.RoundedButton(holder, text="深海", variant=("primary" if self.theme == "dark" else "secondary"),
                             size="sm", command=lambda: self._switch("dark")),
        ]
        for i, b in enumerate(btns):
            b.pack(side="left", padx=(0 if i == 0 else tw.SP["sm"], 0))
        cv.create_window(1130, h / 2, window=holder, anchor="e")
        self._head_holder = holder
        self.root.after(60, self._paint_head)

    def _schedule_head(self) -> None:
        if self._head_job is not None:
            try:
                self.root.after_cancel(self._head_job)
            except Exception:
                pass
        self._head_job = self.root.after(60, self._paint_head)

    def _paint_head(self) -> None:
        """铺海面图。

        <Configure> 常常先给一个偏小的宽度（窗口还没定下来），照那个宽度画就会出现
        「左上角一块、右边一条缝」。所以这里一次画到 1600px，只在必要时才补宽。
        """
        self._head_job = None
        win_w, h = self._head.winfo_width(), self._head.winfo_height()
        if win_w < 8 or h < 8:
            return
        need = max(win_w, 1600)
        if self._head_img_w and self._head_img_w >= need:
            return
        photo = ui.ocean_swatch(need, h, self.theme)
        if photo is None:
            return
        self._refs = self._refs[-2:] + [photo]
        self._head.delete("sea")
        self._head.create_image(0, 0, image=photo, anchor="nw", tags="sea")
        self._head.tag_lower("sea")
        self._head_img_w = need

    def _switch(self, name: str) -> None:
        """换主题就重建窗口：Tk 没有全局样式层，重建比逐个改色可靠。"""
        if getattr(self, "_smoke", False):
            return
        self.root.destroy()
        Demo(name)._run_inner()

    def _run_inner(self) -> None:
        self.root.mainloop()

    # ── 分区外壳 ──
    def _section(self, parent, title: str, note: str = "") -> tk.Frame:
        t = self.t
        card = ui.RoundedCard(parent, owner_bg=t.bg_base, shadow="s1")
        card.pack(fill="x", pady=(0, tw.SP["md"]))
        box = card.inner
        tk.Label(box, text=title, bg=t.layer1, fg=t.label1,
                 font=(ui.FONT_UI, tw.FS["sm"], "bold")).pack(anchor="w")
        if note:
            tk.Label(box, text=note, bg=t.layer1, fg=t.label2,
                     font=(ui.FONT_UI, tw.FS["xs"]), justify="left").pack(anchor="w", pady=(2, 0))
        row = tk.Frame(box, bg=t.layer1)
        row.pack(anchor="w", fill="x", pady=(tw.SP["sm"], 0))
        return row

    # ── 各分区 ──
    def _sec_buttons(self, parent) -> None:
        row = self._section(parent, "按钮 · 5 变体", "悬停加深 / 按下下沉 1px / 焦点环 2px")
        for name, text, kw in (
            ("主要", "启动", {"variant": "primary"}),
            ("次要", "打开目录", {"variant": "secondary"}),
            ("软", "设为默认", {"variant": "soft"}),
            ("幽灵", "全部停止", {"variant": "ghost"}),
            ("危险", "删除", {"variant": "danger"}),
        ):
            ui.RoundedButton(row, text=text, size="sm", command=lambda n=name: self._hit(n),
                             **kw).pack(side="left", padx=(0, tw.SP["sm"]))
            _ = text

    def _sec_sizes(self, parent) -> None:
        row = self._section(parent, "尺寸 · 4 档", "24 / 28 / 32 / 40 px，全部是 4 的倍数")
        for name, size, text in (("XS", "xs", "24"), ("S", "sm", "28"), ("M", "md", "32"), ("L", "lg", "40")):
            ui.RoundedButton(row, text=f"{name} {text}px", size=size, variant="secondary",
                             command=lambda n=name: self._hit(f"尺寸{n}")).pack(side="left", padx=(0, tw.SP["sm"]))

    def _sec_states(self, parent) -> None:
        t = self.t
        row = self._section(parent, "状态 · 正常 / 禁用 / 焦点", "禁用态仍保持 3:1，不用灰到看不见")
        ui.RoundedButton(row, text="正常", size="sm", variant="primary",
                         command=lambda: self._hit("正常")).pack(side="left", padx=(0, tw.SP["sm"]))
        d = ui.RoundedButton(row, text="禁用", size="sm", variant="primary",
                             command=lambda: self._hit("禁用"))
        d.configure(state="disabled")
        d.pack(side="left", padx=(0, tw.SP["sm"]))
        f = ui.RoundedButton(row, text="焦点", size="sm", variant="secondary",
                             command=lambda: self._hit("焦点"))
        f.pack(side="left", padx=(0, tw.SP["sm"]))
        f.focus_set()
        tk.Label(row, text="← Tab 键可在这些按钮间跳", bg=t.layer1, fg=t.label2,
                 font=(ui.FONT_UI, tw.FS["xs"])).pack(side="left")

    def _sec_tokens(self, parent) -> None:
        """把令牌表摆出来：配色不是感觉，是一张能被核对的表。"""
        t = self.t
        row = self._section(parent, "色彩令牌 · 8 个语义色", "全部取自 dafy-whale-theme，对比度已验")
        pairs = [
            ("brand", t.brand), ("line1", t.line1),
            ("label1", t.label1), ("label2", t.label2),
            ("success", t.success), ("warn", t.warn),
            ("error", t.error), ("layer2", t.layer2),
        ]
        grid = tk.Frame(row, bg=t.layer1)
        grid.pack(anchor="w")
        for i, (name, color) in enumerate(pairs):
            cell = tk.Frame(grid, bg=t.layer1)
            cell.grid(row=i // 4, column=i % 4, sticky="w", padx=(0, tw.SP["sm"]), pady=(0, tw.SP["xs"]))
            sw = tk.Canvas(cell, width=76, height=22, bg=t.layer1, highlightthickness=0, bd=0)
            sw.pack()
            ui.round_box(sw, 0, 0, 75, 21, tw.RAD["xs"], fill=color,
                         outline=(t.line1 if name in ("layer2",) else ""), width=1)
            tk.Label(cell, text=f"{name} {color}", bg=t.layer1, fg=t.label2,
                     font=(ui.FONT_MONO, tw.FS["xs"])).pack(anchor="w")

    def _sec_pills(self, parent) -> None:
        row = self._section(parent, "状态胶囊 · 5 种语义", "on / off / warn / error / busy")
        for kind, text in (("on", "运行中"), ("off", "已停止"), ("warn", "端口占用"),
                           ("error", "启动失败"), ("busy", "正在拉起")):
            ui.Pill(row, text=text, kind=kind).pack(side="left", padx=(0, tw.SP["sm"]))

    def _sec_chips(self, parent) -> None:
        row = self._section(parent, "标签芯片 · 6 种", "版本号用等宽字，形态别和按钮撞")
        for kind, text in (("ver", "v0.3.0"), ("primary", "★ 主要版本"), ("soft", "已接管"),
                           ("key", "Ctrl+K"), ("ok", "已安装"), ("warn", "需升级")):
            ui.Chip(row, text=text, kind=kind).pack(side="left", padx=(0, tw.SP["sm"]))

    def _sec_cards(self, parent) -> None:
        t = self.t
        row = self._section(parent, "卡片投影 · 三级", "s1 静置 / s2 悬停 / s3 浮层；鼠标移上去升一级")
        for level in ("s1", "s2", "s3"):
            box = ui.RoundedCard(row, owner_bg=t.layer1, shadow=level, radius=tw.RAD["sm"],
                                 width=112, padx=tw.SP["sm"], pady=tw.SP["xs"])
            box.pack(side="left", padx=(0, tw.SP["sm"]))
            tk.Label(box.inner, text=level.upper(), bg=t.layer1, fg=t.label2,
                     font=(ui.FONT_UI, tw.FS["xs"], "bold")).pack()

    def _sec_log(self, parent) -> None:
        t = self.t
        card = ui.RoundedCard(parent, owner_bg=t.bg_base, fill=True, width=1, height=150,
                              shadow="s1", padx=tw.SP["sm"], pady=tw.SP["xs"])
        card.pack(fill="x")
        card.inner.rowconfigure(0, weight=1)
        card.inner.columnconfigure(0, weight=1)
        txt = tk.Text(card.inner, width=1, height=1, bd=0, highlightthickness=0,
                      bg=t.layer1, fg=t.label1, insertbackground=t.label1,
                      font=(ui.FONT_MONO, tw.FS["sm"]), wrap="none", padx=2)
        txt.grid(row=0, column=0, sticky="nsew")
        sb = ui.MiniScrollbar(card.inner, command=txt.yview, bg=t.layer1)
        sb.grid(row=0, column=1, sticky="ns")
        txt.configure(yscrollcommand=sb.set)
        for i in range(1, 25):
            txt.insert("end", f"[12:0{i % 10}:00] lane-{i % 3} · 端口 {7080 + i} 已就绪\n")
        self._log, self._log_sb = txt, sb
        tk.Label(card.inner, text="日志面板：fill 模式圆角容器 + 极简滚动条",
                 bg=t.layer1, fg=t.label2, font=(ui.FONT_UI, tw.FS["xs"])).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(tw.SP["xs"], 0))

    # ── 交互 ──
    def _hit(self, name: str) -> None:
        self._hits.append(name)
        tail = " · ".join(self._hits[-3:])
        self.status.configure(text=f"点了：{tail}（共 {len(self._hits)} 次）")

    def run(self) -> None:
        self.root.mainloop()


def smoke() -> int:
    """无窗口自检：建全部控件、点一遍、销毁。改完代码先跑这个。"""
    enable_dpi_awareness()
    app = Demo()
    app._smoke = True          # 自检时不要真的切主题（会销毁窗口）
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
    bad = tw.check_contrast(verbose=False)
    print(f"SMOKE OK / buttons={n} / callbacks_fired={len(app._hits)}"
          f" / panel_scroll={'OK' if panel_ok else 'FAIL'}"
          f" / contrast_issues={len(bad)}")
    return 0 if (panel_ok and not bad) else 1


def _smoke_panel(app) -> bool:
    """日志面板那套：fill 模式圆角面板 + 极简滚动条（不在主预览里单独自检）。"""
    t = app.t
    holder = tk.Frame(app.root, bg=t.bg_base)
    holder.place(x=0, y=0, width=220, height=140)
    panel = ui.RoundedCard(holder, fill=True, width=1, height=1, padx=4, pady=4,
                           owner_bg=t.bg_base)
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


def shots(outdir: str) -> int:
    """不弹窗截图：两套主题各导一张 PNG（用于和旧版对比）。"""
    from PIL import ImageGrab
    os.makedirs(outdir, exist_ok=True)
    made = []
    for name in ("light", "dark"):
        enable_dpi_awareness()
        app = Demo(name)
        r = app.root
        r.attributes("-topmost", True)
        r.deiconify()
        r.update_idletasks()
        r.update()
        time.sleep(0.45)
        r.update()
        x, y = r.winfo_rootx(), r.winfo_rooty()
        w, h = r.winfo_width(), r.winfo_height()
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
        path = os.path.join(outdir, f"ui-kit-{name}.png")
        img.save(path)
        made.append(path)
        r.destroy()
        print("saved", path, img.size)
    return 0 if len(made) == 2 else 1


if __name__ == "__main__":
    argv = sys.argv[1:]
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--smoke" in argv:
        raise SystemExit(smoke())
    if "--shot" in argv:
        i = argv.index("--shot")
        out = argv[i + 1] if len(argv) > i + 1 else os.path.join(HERE, "_ui-preview")
        raise SystemExit(shots(out))
    theme = "dark" if ("dark" in argv or "--theme" in argv and "dark" in argv) else "light"
    enable_dpi_awareness()
    Demo(theme).run()
