# 更新记录（CHANGELOG）

版本号 = git tag。每个 tag 都指向一个能直接跑起来的源码状态（`py dsh_lanes.py --help` / `py dsh_lanes_gui.py`）。
exe 不进仓库（`.gitignore` 排除了 `dist/`），要分享请把 `dist\*-onedir.zip` 挂到 Release 附件。

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
