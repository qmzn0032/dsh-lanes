# 更新记录（CHANGELOG）

版本号 = git tag。每个 tag 都指向一个能直接跑起来的源码状态（`py dsh_lanes.py --help` / `py dsh_lanes_gui.py`）。
exe 不进仓库（`.gitignore` 排除了 `dist/`），要分享请把 `dist\*-onedir.zip` 挂到 Release 附件。

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
