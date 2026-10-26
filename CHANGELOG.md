# 更新记录（CHANGELOG）

版本号 = git tag。每个 tag 都指向一个能直接跑起来的源码状态（`py dsh_lanes.py --help` / `py dsh_lanes_gui.py`）。
exe 不进仓库（`.gitignore` 排除了 `dist/`、`_build/` 和 `*.exe`），要分享请把 `dist\*-onedir.zip`
挂到 Release 附件。

---

## v0.3.6 — 2026-10-26

### 「一闪一闪」是自己上一版补出来的

用户的话很准：**滑动和拖大小时会一闪一闪**。这次没有先改代码，而是先查了三件事，
再回来改 —— 结论是上一版 v0.3.5 为修「滚一下卡片变白」加的那行 `RedrawWindow`,
恰好就是闪的来源。

1. `RDW_ERASE` 到底做了什么。官方文档写得很直白：它会让窗口在重画前先收到
   `WM_ERASEBKGND`。也就是说「擦一遍底色 → 再画内容」，擦和画之间那一帧，
   屏幕上就是一块**没有内容的底色**。我们的列表底是一个 `Label`（底色是纯蓝 `BG`），
   所以每一擦就是一次"蓝底一闪"。
   （[RedrawWindow 文档](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-redrawwindow)）
2. `RDW_UPDATENOW` 让重画**在处理滚轮事件的当下同步完成**。滚动是"先搬像素、再挪窗口项"，
   在中间插一次同步重画，屏幕上就出现"一半新一半旧"的一帧。而且 v0.3.5 还连补三次
   （30/90/180ms），8 格滚轮实测擦了 **13 次**。
3. 顺手评估过 Win32 的经典解法 `WS_EX_COMPOSITED`（整窗双缓冲，
   [扩展窗口样式文档](https://learn.microsoft.com/en-us/windows/win32/winmsg/extended-window-styles)），
   但它只对顶层窗口生效，而且会让每一次子控件重画都触发整窗重新合成 ——
   在 1971×1128、145 个控件的窗口上风险大于收益。**没有采用**。

### 改法：只标「脏」，不擦不画

`redraw_windows` 现在只用 `RDW_INVALIDATE | RDW_ALLCHILDREN`：

- 不发 `ERASE` → 不会出现"没有内容的那一帧"；
- 不发 `UPDATENOW` → 不当场画，Windows 会把这一轮消息里攒下的所有脏区域
  **合并成一次** `WM_PAINT`。一次滚动从 13 次重画变成 1 次；
- 只标 `list_frame` 一张：`ALLCHILDREN` 会递归到里面每一个卡片子窗口；
- 原来"连补三次"也就不需要了（补画已经免费），改成一发 40ms 去抖。

同时撤掉了 `update_idletasks()`：它是"当场同步画"的必需品（Tk 把窗口项挪位置排在 idle 里，
不先推完，补画就补在挪之前的位置）。现在不当场画了，等 `WM_PAINT` 到来时窗口早就在正确位置。

### 顺手修：卡片自检在最需要它的场景里整队跳过

`_heal_cards` 用 `winfo_ismapped()` 筛"看得见的卡片"。但 canvas 上的窗口项**一旦滚出可视区，
Tk 就把它标成"未映射"**（滚回来才恢复）—— 而"滚过之后卡片白了"正是自检要处理的场景，
等于这个兜底在最关键的时候一个都不查。改成看实际尺寸（`winfo_width/height >= 4`）。
`_align_ocean_cards` 同一个毛病，一起改。

### 浅海退回 v0.3.4 的配色

用户原话：「深海模式颜色还可以，浅海模式还是不好看改回原来到的颜色」。

v0.3.5 把浅海压深了一档（想解决"白卡片浮在雾里"），结果整体太蓝、抢戏。现在
`water_*` / `line1` / `line2` / `bg_base` / `layer2` / `brand` / `label*` / `shadow`
**全部退回 v0.3.4 的原值**，深海一个像素没动。

连带撤掉 v0.3.5 新加的那条对比度约束「`line1` 与 `water_bottom` 可区分」——
浅海这套值里两者本来就是同一个色，描边融进海里、卡片像浮着，**这正是要的效果**。
留着这条只会一直报红，等于把偏好当成 bug。自检项 34 → 32（16 × 2 套主题）。

### 新增回归 `_ui-preview/_t_flicker.py`

「闪」没法用截图对比（一帧的事），所以数**擦除次数** —— 擦一次＝闪一次，这是它的机制。
三个场景各量一遍，顺带量卡片底图重画次数、海面重算次数、每格滚轮耗时：

| 场景 | 擦除（次） | 卡片重画 | 每格耗时 |
| --- | --- | --- | --- |
| 滚 8 格轮子 | **13 → 0** | 8 → 2 | 15 → 18ms（含一次合并后的重画） |
| 拖窗口边 9 个尺寸 | **33 → 0** | 102 → 55~72 | — |
| 拖分栏来回 9 次 | **13 → 0** | 32 → 25 | — |

（`_t_flicker.py` 里还记了一条环境坑：这台机器上连续抓屏几十次之后窗口会自己变最小化，
跟本程序无关，测试里先 `deiconify()` 再抓。）

---

## v0.3.5 — 2026-10-26

### 「滚一下，卡片就白了」——Windows 上 canvas 里塞窗口项的坑

用户的截图很具体：左列两张 lane 卡片只剩一圈白框，框里是海面，卡片里的标题、按钮、
说明全都不见了；他的描述是「使用滑轮就会出现渲染丢失」。

复现之后根因很清楚，而且**只在 Windows 上有**：

- 左列是 `Canvas`，卡片是 canvas 上的「窗口项」。在 Windows 上那是一个**真的子窗口**
  （`winfo_id()` 拿得到 HWND）。滚动时 Tk 用 `ScrollWindowEx` 搬像素，再把窗口项挪过去 ——
  子窗口搬到新位置**不会收到重画**，屏幕上就留成父窗口的底色（海面）。
  X11 上没有真子窗口，所以这个坑在那边根本不存在。
- 修法：滚动/改尺寸/改滚区之后，按 HWND 调 `RedrawWindow(INVALIDATE|ERASE|ALLCHILDREN|
  UPDATENOW)` 强制同步重画。为什么连补三次（30/90/180ms）：左列改高度 → 卡片重排 →
  里面折行又变 → 再改高度，这个收敛过程要走一两轮，只补一次容易补在中间。
- 顺带查出**第二个**坑：`_layout_list` 用 `list_frame.winfo_reqheight()` 当内容高度，
  而列表底图是画在这个 `Label` 上的 —— 设图之后 `Label` 的 reqheight 就等于**上一张底图的
  高度**，等于让底图自己往上反馈。表现是滚区永远比内容多一截（滚到底之后一大片空海），
  或者反过来少一截（最后一张卡被 pack 压扁、底下几行字滚不到）。
  现在改成「每张卡 `max(reqheight, 实际高度)` + 间距」，并在卡片高度变化后 120ms 重算一次。

### 滚轮本身也有两个毛病

- `bind_all("<MouseWheel>")` + `winfo_containing` 反推"指针下面是不是列表"：指针压在卡片
  里的按钮/胶囊/芯片上时就判断错，甚至整次滚轮被丢掉。改成直接绑在列表那棵控件树上。
- 高精度滚轮/触控板一格只报 30~40，`int(-delta/120)` 直接算成 0 —— 表现为"滚了没反应"。
  改成累积 delta，攒够 120 再滚一行。

### 浅海还是一眼"割裂"

上一版把两套主题的**上下关系**统一了，但浅海的海水太淡（和卡片只差 4% 亮度），整窗读起来是
「白卡片浮在雾里」：蓝色既不像水也不像底，自然显得割裂。这一版：

- 海水整体压深一档、提一点饱和：`water_top` #D2E5F9 / `water_bottom` #AECDEB /
  `water_edge` #88AFD8。卡片与海水的对比从 1.09:1 提到 **1.27:1**。
- `line1`（卡片描边）以前和 `water_bottom` 是**同一个色值** `#CFE1F3` —— 贴着海的卡片边缘
  等于没有边。改成 #C2D8EE，并把这条写成第 4 条硬约束：「line1 与 water_bottom 可区分 ≥1.10」。
- 右栏日志区改成**凹进去的水槽**（`Text` / 滚动条 / 空状态都用 `layer2` 垫底）：原来整块
  卡片全白，右栏就是一大片白纸，和左边的海/白卡片是两种材质 —— 这也是"割裂"的一半。
- `theme_whale.py --check` 现在 **34 项**（17 × 2 套主题）全达标。

### 自检（对账 + 补画）

界面上任何一块"图过期了"都不该只能靠重启解决。`_tick` 每 2.5 秒对一次账：卡片画的尺寸和
现在对不上就补画一次，并写一行日志（10 秒最多一条）。注意**不能**在这里按内容高度去改卡片
高度 —— 卡片自己的 `_on_inner` 也在管这件事，两边各按各的时刻算会互相改来改去（高度在
250↔292 之间来回跳，卡片永远画不完）。

### 验证

```
py theme_whale.py --check                 34 项全达标
py dsh_lanes_gui.py --selfcheck          29 个界面入口齐全（light / dark）
py ui_kit_demo.py --smoke                contrast_issues=0
py _ui-preview\_t_split.py               分栏记忆/缩放五项全过
py _ui-preview\_t_theme.py               切主题 427ms（首次）/ 200ms，控件 145→146，日志 550 字符不丢
py _ui-preview\_t_wheel.py               8 格滚轮重画 1 次；滚完左列 72.3% 白、2583 个深色像素；4/4 卡片无过期
```

生产副本 `启动器\dsh_lanes_gui.py` 一个字节没动（本项目只在 `启动器\UI大肥鱼化\` 里改）。

---

## v0.3.4 — 2026-10-26

### 两种模式不再是"两套皮肤"

用户的原话是「两种模式颜色搭配割裂感很强，切换模式时还有渲染卡顿」。查下来是三个独立的
问题叠在一起，各自都会让人一眼看出"这不是一个东西"：

- **深海的海比卡片亮**（关系反了）。海面渐变以前是写死往青蓝推 42%，到窗口底部已经比卡片
  还亮（实测 #234B74 vs 卡片 #0F1E38）。浅海是"白卡片浮在浅蓝水上"，深海成了"深色洞凿在
  发亮的蓝水里" —— 同一个界面在两种天色下读出来的上下关系是反的。
  现在海水的三档颜色（`water_top` / `water_bottom` / `water_edge`）和光斑强度都进了主题表，
  两种主题只换明度、不换色相；并且把这条关系写成了**可验证约束**：
  `theme_whale.py --check` 现在会验「卡片必须比海水最亮处还亮」和「窗边那圈必须比卡片深」，
  不达标直接退出码 1。
- **海面根本不跟着主题走**：`OceanBackdrop` 把主题名存在 `self._name` 上，而
  `tkinter.Misc.__init__` 里的 `self._name` 是**控件路径名**（`!oceanbackdrop`）——
  于是 `palette("!oceanbackdrop")` 一路回落到 **light**：深海窗口的海面一直是按浅海渲染的
  （那圈发亮的浅蓝边就是这么来的）。改名 `self._theme`。
- **Windows 标题栏还是浅灰的**：窗口内容已经是深海，顶上还压着一条 #F3F3F3，
  整块界面像被横着切一刀。新增 `set_dark_titlebar()`（`DwmSetWindowAttribute`，
  Win10 1809+ / Win11，属性 19/20 都发一遍，`SWP_FRAMECHANGED` 让它立刻重绘）。
  注意它只能在窗口**已经映射**之后设 —— 映射前 `GetParent(winfo_id())` 拿不到外框，
  调用会静默失败，所以是"没映射就 60ms 后再试，最多 10 次"。
- **四周压深（渐晕）**：只做纵向渐变的话，最上面那条 16px 的缝正好是最浅的一端（≈ 卡片色），
  看上去还是"白边"。改用 `Image.radial_gradient` 当蒙版把窗边一圈压深，卡片才真的像浮着。

### 换主题不再重启进程

以前是"写配置 → `root.destroy()` → `main()` 的 while 循环再起一套"：实测 ~1.2 秒，
而且重建过程中那一帧会被看到（海面铺好了、卡片还没长出来）。现在：

- **原地重建 + 双缓冲**：新的一套建在一个临时 `Frame` 里，先 `lower()` 到海面画布**底下**
  （尺寸/布局/绘制全都照算，但被不透明的海面盖住），布局稳定之后再 `lift()` 一次性翻上来，
  然后销毁老的那套。中间那几次 `update_idletasks`（量尺寸要用）不会再被看见。
  踩到的一个坑：一开始想"连卡片底色也提前画好，第一帧就是成品"，于是在几何计算过程中
  同步调了 `itemconfigure` —— 卡片的内嵌 Frame 被挪出可视区，换完主题左列整片空白。
  现在不强制画，交给 60ms 去抖（肉眼看不出来）。
- **现场全部带过去**：日志用 `Text.dump()` 原样搬（时间戳、颜色都不重新生成）、
  分栏比例、状态栏文字、底部计数、顶部「npm 上：…」那一行、滚动位置、自动滚动开关。
- 实测：首次 ~0.65 秒，来回切第二次 ~0.3 秒（图片缓存复用）；控件总数 145 → 145（不膨胀），
  来回切三次不越切越慢。回归测试：`py _ui-preview\_t_theme.py`（会在重建过程中连拍 4 张图，
  断言中途屏幕上是**老界面**，不是半成品）。

### 顺手修掉

- **根目录可写性探测挪到后台线程**：失败时会重试 3 次 × `sleep(150ms)` = 0.45 秒，
  原来就压在 `LauncherApp.__init__` 的同步路径上（受限沙箱 / 网络盘里每开一次窗卡半秒，
  正好也是换主题时"卡一下"的来源之一）。
- **被销毁的卡片会取消排队中的重绘**（`<Destroy>` → `after_cancel`），
  控制台那串 `invalid command name "…<lambda>"` 也一起清了。

---

## v0.3.3 — 2026-10-26

### 整窗海面（②）＋ 可拖拽分栏（③）

窗口的底色不再是一块纯色，而是一张**按窗口尺寸渲染的海面图**；左右两栏变成能拖的分栏。

- **`ui_kit.ocean_slice()` / `ocean_crop()`**：从"整窗那张海面"里按**窗口坐标**裁出控件所在位置
  的一块。位置按"屏幕坐标 − 窗口原点"算，所以嵌在滚动区里的卡片也适用（滚到哪算哪），
  几块海面面板拼起来接缝对得上 —— 这一点是整窗连续的前提。
- **`ui_kit.OceanBackdrop(crop=True)`**：同一个控件两种用法 —— 根背景渲染整张（`crop=False`），
  右栏背景裁自己那一块（`crop=True`）。`place(relwidth=1, relheight=1)` 才铺得满整窗，
  `pack` 只会分到"剩下那条缝"。
- **左列列表底改成 `tk.Label` + 海面切片**：Tk 的容器**全都不透明**，`Frame` 会整片盖住背后的海；
  `Label` 的 `image` 画在**子控件下面**（实测），所以卡片之间 12px 的缝里看见的是真海面。
  Label 的请求高度仍跟着子控件走（`packPropagate`），滚动区高度不受影响。
- **布局改为"卡片浮在水上"**：顶栏 / 状态栏改成浮在海水上的圆角白卡（四周留 16px），
  左列卡片之间留 12px，中间分栏缝 12px。缝隙给得比一般界面慷慨 —— 缝太窄，海就只剩
  一圈描边，等于白铺。
- **海面渐变按"看得见"重做**：起始色 `bg_base` → 结束色往青蓝推（浅海 44% / 深海 42%）。
  一开始只往 `layer2` 推 75%，两个色只差 9 个数，渲出来跟纯色没区别，窗口四边成了白边。
- **卡片圆角的缺口**：卡片 canvas 的底色会把圆角外那点海盖掉，露出四个色块。
  `RoundedCard(ocean=True)` 在卡片底下垫一张同位置的海面切片解决（Tk 的 canvas 图形项之间
  是真叠加 —— 先画海面再画带透明通道的圆角面，圆角外露的就是海面，已用像素采样验证）。
- **可拖拽分栏**：`tk.PanedWindow`（`sashwidth=12`、`sashcursor=sb_h_double_arrow`、
  `opaqueresize=True`），鼠标靠近时缝会亮成品牌蓝（淡色在整窗海面上根本看不见，
  所以悬停用的是 `brand` 而不是 `brand_wash`）。比例在松手时立刻写 `lanes.json → ui.split`，
  下次打开按比例还原；两条边都有最小宽度（420 / 330），拖不过去。

### 顺手修掉

- **切换主题 / 关窗时的控制台噪音**：销毁窗口前先 `after_cancel` 排队中的任务
  （`_poll_queue` / `_tick` / `_apply_split` / 底图 / 远端查询），否则 Tcl 会在窗口没了之后
  再去调那串命令，控制台上就是 `invalid command name "…_poll_queue"`。
- **`_save_geometry` 记住了分栏比例**：它是"读出来 → 改几个键 → 写回去"，所以 `ui.split`
  和 `ui.theme` 不会被整体覆盖抹掉（v0.3.2 修过一次同类问题）。

---

## v0.3.2 — 2026-10-26

### 浅海 / 深海切换

- **顶栏 `◐` 按钮**：标签写的是"将要去的那一套"（当前浅海就显示「深海」），点一下整窗切换；
  选择存进 `lanes.json → ui.theme`，下次打开还是这套。命令行也能临时指定：`--theme dark`。
- **为什么是重建而不是就地改色**：Tk 没有样式层，颜色在控件创建时就落进默认参数里了。
  逐个 `configure` 既容易漏、又会把状态搞乱；重建一整窗不到 1 秒。所以 `main()` 套了一层
  "关窗 → 需要就再来一次"的循环（`app.restart`）。

### 顺手修掉（都只在"重建窗口"这条路径上暴露）

- **`_save_geometry()` 会把 `ui.theme` 抹掉**：它原来是 `cfg["ui"] = {w,h,x,y}` 整体覆盖。
  改成先 `dict(cfg["ui"])` 再 `update`。同一个坑 v0.3.3 又用在 `ui.split` 上。
- **重建后必崩 `image "pyimage1" doesn't exist`**：新的 Tk 根起来后，上一轮缓存的
  `PhotoImage` / `Font` 还绑在**已销毁的那个解释器**上。`ui_kit._ensure_root()` 检测
  `tk._default_root` 换了就清空所有渲染缓存。
- **主题切换后窗口会跳**：切主题时顺带把当前几何一起存进 `lanes.json`，重建后原地开。

---

## v0.3.1 — 2026-10-26

### 配色令牌化 + 可访问性

- **新增 `theme_whale.py`**：颜色 / 间距 / 字号 / 圆角 / 投影 / 动效全部收成令牌，控件只认语义名。
  浅海与深海各一套原始色表（`brand`、`bg_base`、`layer1/2`、`line1/2`、`label1/2`、
  `success/warn/error` 及各自的 `_bg/_line/_fg`），派生色（hover / press / wash / 禁用 / 滚动条）
  由 `mix()` 算出来，不再手写。
- **对比度自检**：`py theme_whale.py --check` 逐对验收 26 组（正文 ≥4.5:1、非文字 ≥3:1），
  不达标退出码 1。旧值 `#8A8F98` on `#F4F5F7` 只有 **2.96:1**、`#6B7280` 只有 **4.40:1**，
  都低于 WCAG AA；新的 `label2` 是浅海 **5.51:1** / 深海 **7.45:1**。深海的品牌按钮用深色字
  （5.97:1），因为白字压 `#6C8CFF` 只有 3.07:1。
- **`ui_kit.py` 重写**：`RoundedButton`（primary/secondary/soft/ghost/danger × 4 尺寸 ×
  hover/press/focus/disabled）、`RoundedCard`（三级投影、悬停升一级）、`Pill`、`Chip`、
  `MiniScrollbar` 全部改由令牌驱动；焦点环 2px、Enter/Space 可触发。旧的模块级常量
  （`BG` / `CARD` / `ACCENT` / `SUBTLE` …）继续导出，调用点不用改。
- **`dsh_lanes_gui.py` 里写死的十六进制清零**：常量改为从 `_T`（当前令牌）派生。

---

## v0.3.0 — 2026-10-25

### 界面：「大肥鱼化」

**为什么重做**：原版界面是纯 `tk` 默认控件 —— 方块按钮、到处同色、没有层级；卡片和日志面板
挤在一起，日志就是一块直接往里灌的 `Text`。这一版把外观换成一套自绘控件（品牌蓝 `#4D6BFE`），
**功能一行没减**，改动集中在新增的 `ui_kit.py` 与 `dsh_lanes_gui.py`。

- **`ui_kit.py`（新增）**：`RoundedButton`（4 变体 × 3 尺寸 × 3 态）/ `RoundedCard` /
  `Pill` / `Chip` / `MiniScrollbar` / `divider()`，纯 `tkinter` + `math`，零第三方依赖。
  刻意让 API 向 `tk.Button` 靠拢（`.pack()` / `.configure(text=…)` / `.cget("text")` / `.invoke()`），
  所以把老的 `tk.Button` 换掉时，调用点一行都不用改。
- **卡片**：`RoundedCard` + 悬停高亮 + **左侧状态色条** + 标题加大；版本号与 `★ 主要版本` 改芯片，
  运行状态改状态胶囊。
- **日志面板**：时间戳列 + 级别字形 + 小节留白 + 空状态 + 「自动滚动已暂停」提示 + 极简滚动条；
  批量插入，上限 4000 行。
- **顶栏**：三行文字压成两行，dist-tag 从一串字改成芯片，窄窗口自动少显示几个。
- **底部**：蓝色文字链接改幽灵按钮，底部说明不再被挤成竖条。
- **图标**：`assets/app.ico` 换成官方鲸鱼（从 dsh 前端自带的 `favicon.svg` 光栅化，
  16/24/32/48/64/128/256 七档、透明底、品牌蓝），窗口标题栏与 exe 共用同一份。
- **46 KB 轻量壳**：`tools/dsh_stub.cs` + `tools/build_stub.bat`。`/target:winexe` 的 C# 壳去找本机的
  `pythonw.exe`（`DSH_LANES_PY` → `pyw.exe -3` → 注册表 `PythonCore\3.x\InstallPath` → PATH），
  再拉起同目录的 `dsh_lanes_gui.py`。对比 PyInstaller 文件夹版（exe 2.3 MB + `_internal\` 22.3 MB）
  小两个数量级，而且改完 `.py` 不用重新打包。

### 顺手修掉的四个坑

- **不弹黑窗口**：所有 `subprocess` 调用统一带 `creationflags=CREATE_NO_WINDOW`
  （`_no_window()` / `_new_group_flags()`）。GUI 壳是 `pythonw`，但从它里面起的 `npm` / `node`
  默认仍会闪一下控制台。
- **受限令牌**：新增 `token_state()`（`IsTokenRestricted` + 进程完整性级别）。进程被关在受限沙箱里时，
  写文件会报 `PermissionError [Errno 13]`、`taskkill` 也杀不掉别人的进程 —— 这**不是**目标目录的权限
  问题（`icacls` 看着完全正常）。检测到受限时界面横幅直接点名，别让人跑去查 D 盘权限。
- **日志文件被占用**：`open_log_append()` 先重试 → 换带时间戳的文件名 → 换目录
  （程序目录 → `%LOCALAPPDATA%` → `%TEMP%`）→ 全都不行就不记日志，但**照常启动 lane**。
- **端口被占但读不到 PID**：`probe_dsh_like()` 判断应答方像不像 dsh，像就直接接管，
  不再白起一个 `node` 去撞 `EADDRINUSE`（退出码 1，界面上只显示"启动超时"）。

细节见 [docs/界面美化.md](docs/界面美化.md)。

---

## v0.2.3 — 2026-10-24

### 新增：「默认路径那份」也能升级 —— 只铺命令，交给新终端

**为什么不是"再写一个 upgrade 分支"**：接管来的 `global` 装在 npm 全局前缀里
（`%APPDATA%\npm\node_modules\@deepseek-ai\dsh`），旁边一层就是入口 shim（`npm.cmd` / `dsh.cmd`）。
两条路都走不通：在本进程里 `npm install --prefix` 改的是全局前缀；在本进程里 `subprocess`
跑 `npm i -g` 又只能把输出攒进日志面板——而这恰恰是装两三分钟、你最需要盯着看的动作。

做法：**工具只写脚本，不执行 npm。**

- **GUI**：卡片「升级版本…」→ 外部安装时窗口标题变成「升级默认路径的 DSH」，
  按钮是「在终端里升级」（以前这里只有一句红字「本窗口不动它」）。
- **CLI**：`upgrade <lane> <版本或tag> --in-terminal`；不加这个旗标仍然拒绝，
  并把"该加什么"打在屏幕上。
- **预览**：两边都能先看会做什么 —— GUI 的「先看会做什么」/ CLI 的 `--dry-run`
  只打印那条命令，一个文件都不写。

细节：

- 命令形如 `npm install -g @deepseek-ai/dsh@<版本> --no-audit --no-fund`。
  在对话框里**显式选**官方源 / 镜像源时把 `--registry <URL>` 钉进命令，选 auto 则不钉
  ——一条终端命令只有一个源，做不到本工具"先官方再镜像"的自动换源，所以 auto 交给你 npm 自己的配置。
- 脚本落在 `<root>\logs\upgrade-<lane>-<版本>-terminal.bat`，用 mbcs 写（cmd 的默认码页，
  中文不会花；写不了时退回 UTF-8 并加一行 `chcp 65001`）。内容含：那条命令、安装树、
  `[exit code] %ERRORLEVEL%`、`pause`（窗口不会一闪就没）。
- 它正在运行时默认**先停掉再开终端**（Windows 上 node 占着的文件 npm 覆盖不了）；
  对话框里的勾选框可以关掉这个行为，装完从卡片上重新打开即可。
- 版本列表里外部安装的"已是这一版"改按**它自己那棵树**核对（`package_version_at`）；
  以前拿启动器的 `versions\<版本>` 去比，所以那个标记从来不会出现。
- `--rollback` 对外部安装明确拒绝：那棵树从没被本工具升过，没有"上一版"可退。
- 装完回启动器刷新，版本号按安装树自愈（v0.2.1 的 `sync_lane_versions`），`lanes.json` 不用手改。

---

## v0.2.2 — 2026-10-13

### 新增：新建 / 复制可以选择「不继承密钥和设置」（`--no-inherit`）

**为什么做这个**：以前 `clone` 是**整套**搬走 —— 连 API key、匿名身份、DSH 设置、agent 预设
一起带进副本。可有时候要的恰恰相反：一个"没有你的凭据、没有你的设置"的干净环境
（比如把副本当成另一台机器来验证首次启动流程）。所以加一个开关，默认**不勾**（老行为一字不变）。

勾上（或加 `--no-inherit`）之后：

| 照旧带 | 不带 |
| --- | --- |
| 安装树、对话与附件（`sessions\`、`attachments\`）、已装插件（`profiles\` 连同 `node_modules`）、插件自己的数据（如语音模型） | `.credentials.yaml`（API key）、`.anonymous-user-id`（匿名身份）、`settings.yaml*`（DSH 设置）、`.agent-presets\`（agent 预设 / 人格） |

- **CLI**：`clone` 与 `create` 新增 `--no-inherit`。
- **GUI**：「复制这套 DSH」「新建版本」两个对话框里各有一个勾选框；新建那个勾上后，
  上面那条"将从主要版本复制 key"的说明会**同步改写**成"不会复制"，不会一边说复制一边不复制。
- 跳过范围只有**一个出处**：`dsh_lanes.py` 的 `NO_INHERIT_HOME_ITEMS`，GUI 与 CLI 共用它。
- `copy_tree()` 新增 `skip_top`，**只匹配根目录那一层**的名字：`profiles\web\settings.yaml`
  这类嵌套同名文件不会被误伤（实测验证过）。
- 跳过的名字会打印出来（`未继承 : …`），lane 登记里记 `noInherit: true`。
- 副本因此没有 API key —— 卡片上会出现「补齐 API key…」，想补随时点。

**遗留**：`adopt`（接管已有安装）不加这个开关 —— 它的语义是"沿用现状"，不存在继承这一说。

### 新增：与主 DSH 同步对话（`chat-sync`）

**为什么做这个**：副本是某个时刻的快照，你在主 DSH 里继续聊出来的新对话，副本里看不见；
而"把主那套重新复制一遍"会连密钥、设置、插件一起灌回来。所以要一个**只搬对话**的动作。

```
py dsh_lanes.py chat-sync <目标 lane> [--from LANE] [--dry-run] [--backup] [--stop] [--yes]
```

| 搬 | 不搬 |
| --- | --- |
| `sessions\**`（会话正文）、`attachments\**`（附件）、`storages\session_projcache\**`（会话列表用的投影记录）、`storages\workspace.json`（工作区登记，并集合并） | `.credentials.yaml`、`.anonymous-user-id`、`settings.yaml*`、`profiles\`（插件与插件包）、各插件数据、`archivedSessionIds` / `pinnedSessionIds` |

三条实现上的关键判断：

1. **同名按 mtime 较新的赢**（用户 2026-10-13 定的规则），覆盖几条会打出来；复制时用 `copystat`
   保留 mtime —— 不保留的话副本那份看起来永远比源新，下次同步就再也不动了。
2. **投影缓存只跟"这次真动过的会话"走**。`session_projcache\sessions\<id>.json` 里存的是按**源**
   那份会话文件算出来的检查点；如果只按 mtime 独立比，就会给一个"目标更新、刚被跳过没动"的会话
   塞进源记录（记录说 5000 轮、文件只有 3000 轮）。实测这一步把真实数据的搬运量从 201 个文件 /
   129 MB 收敛到 84 个 / 63.6 MB。
3. **不要 `storages\session_projcache.json`**：那是 0.1.x 的单文件布局。本机这个文件停在
   2026-09-10 就没再动过（94.7 MB 死重），0.2.0-rc.2 用的是 `session_projcache\` 目录
   （per-record 布局，包说明原话："durable per-session checkpoint records … the cached listing read"）。

- **CLI**：新增 `chat-sync`（`--dry-run` 预演、`--backup` 先存快照、`--stop` 允许替你停目标）。
- **GUI**：非主要版本的卡片上多一个「同步对话…」：先算账（新增/覆盖几个会话、要搬多少），
  勾着"先存快照"默认开启，可以「先预演一次」再「开始同步」。
- 安全线：目标是主要版本不给同步；目标正在运行时**拒绝**（要 `--stop`）；写 `workspace.json`
  之前留 `.bak`。

---

## v0.2.1 — 2026-10-01

### 修复：接管型 lane 的版本号不再「停在登记那一刻」

现象：主要版本 `global` 是**接管的** npm 全局安装（`installDir` 指向
`%APPDATA%\npm\node_modules\@deepseek-ai\dsh`）。自己 `npm i -g @deepseek-ai/dsh@0.2.0-rc.2`
升级完，启动器卡片上还写着 `0.1.7-rc.2` —— 因为台账（`lanes.json` 里的 `version`）只在
create / adopt 那一刻写过，之后没人再读安装树。

现在：GUI 每轮刷新、每条 CLI 命令启动时，都会**从安装树里读一次真实版本**，与台账不一致就
自动更正并写回 `lanes.json`（日志/终端里留一行说明）。接管型 lane 的树在启动器之外，
所以只更正台账、**不碰安装树**。

判定谁是事实的规矩：以安装树里的 `package.json` 为准 ——
常规 lane 是 `<root>\versions\<版本>\node_modules\@deepseek-ai\dsh\package.json`，
副本是 `<root>\clones\<lane>\node_modules\...`，接管型直接是 `installDir\package.json`
（并核对包名，免得把副本自己的 `dsh-lane-<版本>` 当成真实版本）。
台账写不进去（盘不可写）时，界面这一轮仍然显示事实，下次刷新再试。

---

## v0.2.0 — 2026-09-28

### 新增：新建 / 升级可以自己选下载源（官方源 / 镜像源）

**为什么做这个**：官方当天发新版时，npmmirror 常常只同步了一半。实测 `@deepseek-ai/dsh@0.2.0-rc.1`
镜像已经有了，可它的 **12 个** `@deepseek-ai/dsh-*` 子包还没到（把 dsh 本体 + 两层依赖共 259 个包
逐个核对了一遍）。而旧版是"查版本走官方、`npm install` 走你 `.npmrc` 里的镜像"，两边不一致，
于是 npm 在第一个缺的子包上报 `ETARGET`（`No matching version found for
@deepseek-ai/dsh-cordis-client-runner@0.2.0-rc.1`）直接装不上。

现在的规矩：

1. **查版本用哪个源，装就用哪个源**（每次都显式给 npm `--registry`）；
2. 装的时候那个源要是还没同步全，**自动换另一个候选源重试一次**，并把"换了源"打进日志和界面。
   换源是安全的：这类失败发生在 npm 写盘之前（日志停在 `reify:loadTrees`），安装树不会被动过。

- **CLI**：`versions` / `create` / `upgrade` 新增 `--registry auto|official|mirror|URL`；
  `doctor` 会打印当前生效的源（含"装不上会退到哪个源"）。
- **GUI**：「新建版本」「升级版本」对话框里新增「下载源」三选一（自动 / 官方源 / 镜像源）＋
  「记住这个选择」勾选框（写进 `lanes.json`）；切换源会立刻重查一次版本列表；
  确认框会写明"从哪个源下载"；标题下面那一行也标明版本数字来自哪个源。
- **配置**：`lanes.json` 的 `registry` 语义扩展成"**首选源**"（查询与下载都用它），
  留空或 `auto` = 先官方、不行再镜像；填一个 http 地址（内网源）则不自动换源。

### 打包：改成 `--onedir`（`--onefile` 在本机起不来）

`--onefile` 每次启动都要解包到 `%TEMP%`，这一步在开发这台机器上被拦
（`Could not create temporary directory!` / `Failed to extract VCRUNTIME140.dll`）；
`--windowed` 的 GUI 版只弹一个标题为 "Error" 的对话框，双击看起来就是"什么都没发生"。
`--onedir` 不需要解包，CLI 与 GUI 都正常。**分享时请压缩整个文件夹**，不要只发 `.exe`。

`build_exe.bat` 也加固了：打包前先检查有没有正在运行的 exe（它锁着 `_internal\*.dll`，会让打包中途失败）。

### 修复：运行态写不进去不再把窗口搞成"关不掉的半成品"

- 症状：升级/刷新时弹 `PermissionError: ... run\<lane>.json`，PyInstaller 错误框 + Tk 窗口既不能用
  也关不掉，强行关掉后再启动又报一次。
- 根因：GUI 每轮刷新都会写"运行态"（自愈逻辑：端口上有实例就记下来），那条链路上有会抛异常的
  `write_text`，异常没人接。
- 修法：`write_run` 绝不抛异常（`OSError` → 只警告一次 + 返回 `False`）、`clear_run` 同样放宽；
  启动时检查 lane 根目录可写并说明后果；首次刷新包 `try/except`（失败也把窗口开出来）；
  `main()` 构造失败时弹一次框 + 关窗口 + 打 traceback，**绝不留半成品窗口**；
  CLI 双击（无参数）打印帮助后等一下回车，不再"一闪而过"。

### 其他

- GUI 标题行新增：npm 上各 dist-tag 的版本 + 「官方代码库 ↗」链接。
- 修掉一个隐藏问题：后台线程里读 Tk 变量（Tk 变量只能在主线程读）。

---

## v0.1.0 — 2026-09-27

首个版本。每套 DSH 独立安装树 + 独立 `DSH_HOME` + 独立端口，互不影响：

`create` / `open` / `list` / `instances` / `adopt` / `remove` / `primary` / `sync-key` / `delete` /
`clone`（整套复制，用来试新版/插件冲突）/ `verify`（核对隔离，`--fix` 就地修）/ `upgrade`
（核对 + 启动冒烟，起不来自动退回）/ `plugins`（只读清单）/ `market-*` / `chat-*` / `stop` / `logs` /
`doctor` / `versions`，以及同功能的图形界面（卡片式、5 秒刷新、日志面板、双击卡片即打开）。
