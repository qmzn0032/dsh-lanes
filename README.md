# DSH 多版本启动器（dsh-lanes）

在 Windows 上**同时装、同时跑多套 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）**：
每套一个独立的安装目录、一个独立的 `DSH_HOME`、一个独立的端口。

它要回答的问题很具体：**「先在一个旁边的版本上把那套东西试一遍，再决定要不要动我日常用的那一套。」**

- 纯 Python 标准库 + Tkinter，**不依赖任何第三方包**，不需要 `npx` / `pnpm`
- **它不是 dsh 插件**：不注入 dsh 进程、不改 dsh 自己的源代码，只在外面读文件、探端口、起子进程

> 平台：目前只在 **Windows** 上实测过（用到 `GetExtendedTcpTable`、目录 junction、`os.startfile`）。
> 开发与实测环境：Windows + Python 3.13。

---

## 1. 它解决什么

| 能力 | 命令 | 说明 |
| --- | --- | --- |
| 查有哪些版本 | `versions` | 直接问 npm registry 要 dist-tag 与全部版本（纯 HTTP，不装包） |
| **创建**指定版本 | `create <lane> <版本\|tag>` | 用 `npm install --prefix` 装进独立目录，**不覆盖你正在用的 dsh** |
| **打开**指定版本 | `open <lane\|版本>` | 用它自己的 `DSH_HOME` 和端口起 `dsh web`，并打开带 token 的正确地址 |
| 看 / 停 / 排障 | `list` `stop` `logs` `doctor` | 运行状态、优雅停止、日志尾部、环境自检 |
| **看见"别的"实例** | `instances` | 列出**不是本启动器启动的** DSH 实例 + 本机所有安装树（npm 全局 / npx 缓存 / lane） |
| **接管在用的那套** | `adopt <lane>` | 把本机已有的安装注册成 lane，沿用它的 `DSH_HOME`，之后从这个窗口打开/停止它 |
| **标记主要版本** | `primary <lane>` | 常用版本置顶；新建 lane 时自动继承它的 API key |
| **复制一整套** | `clone <源> <新>` | 安装树 + `DSH_HOME`（会话、设置、凭据、已装插件）整套复制成副本，动副本不碰原件；`--no-inherit` 则不带密钥与设置 |
| **升到指定版本** | `upgrade <lane> <版本>` | 就地升级 + 核对 + 启动冒烟，起不来自动退回；`--rollback` 回上一版 |
| 核对隔离 | `verify <lane>` | 逐条比对链接、版本、pnpm 账本、本地依赖，并体检 profile 补丁层 |
| 备份对话 | `chat-backup` / `chat-restore` | 会话整棵备份（逐文件指纹）+ 恢复（先校验快照、再另存现状） |
| **与主 DSH 同步对话** | `chat-sync <lane>` | 把主 DSH（主要版本）的对话搬进这条 lane：只动对话与附件，密钥 / 设置 / 插件一律不动 |
| 删除一套 | `delete <lane>` | 登记 + 运行态 + 安装树 + `DSH_HOME`；主要版本要手打名称二次确认 |

`lane` ＝ 一条「版本通道」的名字，例如 `main` / `next` / `last`。

---

## 2. 环境要求

1. **Windows**（见上文）。
2. **Python 3.10+**，并确保 `py` 或 `python` 在 PATH 里（窗口版用 Tkinter，官方安装包自带）。
   开发与实测环境是 **Python 3.13**；更早的版本没实测过，代码里没有第三方依赖。
3. **Node.js + npm**：dsh 本体是 npm 包，`create` / `upgrade` 会调用它来安装。启动器自己不打包 node。

什么都不用 `pip install`。

---

## 3. 快速开始

**图形界面（推荐）**

1. 双击 `start_gui.bat`。
2. 点右上角「**＋ 新建版本**」：选一个版本（对话框会列出 npm 上现成的版本与 dist-tag），起个名字（就是 lane 名），端口留空自动挑。
3. 卡片上点「**打开**」——它会用这套 lane 自己的 `DSH_HOME` 和端口起一套 dsh web，并直接打开带 token 的地址。
4. 「停止」「日志」「升级版本…」「复制…」「删除这套 DSH」都在卡片上。

**命令行**

```bat
py dsh_lanes.py versions
py dsh_lanes.py create next 0.1.7-rc.2
py dsh_lanes.py open next
py dsh_lanes.py list
py dsh_lanes.py stop next
```

`start.bat open next` 是同一个入口（双击也能用，会 pause 住输出）。
完整命令见第 6 节，或 `py dsh_lanes.py --help`。

**不想装 Python？用打好的 exe**（自己打见第 10 节）：

| 目录里的 exe | 用途 |
| --- | --- |
| `dsh-lanes-gui\dsh-lanes-gui.exe` | 双击即用，和 `start_gui.bat` 一样 |
| `dsh-lanes\dsh-lanes.exe` | 命令行版，参数与 `py dsh_lanes.py` 完全一致（例如 `dsh-lanes.exe doctor`） |

exe **自带 Python 运行时**，目标机器不用装 Python；但**仍然需要 Node.js + npm**（dsh 本体是 npm 包）。
exe 没签名，首次运行 Windows SmartScreen 可能拦一下：点「更多信息 → 仍要运行」。
**注意 exe 要连它所在文件夹一起用**（旁边有 DLL 与 `_internal`），单独拷 exe 出去跑不起来。

---

## 4. 隔离是怎么做到的

三条互不干扰的边界，缺一条就会变成"升级测试是假的"：

| 边界 | 做法 |
| --- | --- |
| **安装树** | `<root>\versions\<版本>` 或 `<root>\clones\<lane>`，用 `npm install --prefix` 装进去；不碰全局 `node_modules` |
| **DSH_HOME** | `<root>\homes\<lane>`：会话、设置、凭据、已装插件各一套（接管来的 lane 例外，它沿用原有的 HOME） |
| **端口** | 在 `port_range`（默认 3080–3129）里挑第一个空闲的 |

复制一套时还有三个必须做对的细节，代码里都处理了（都是实测踩出来的）：

- `profiles\node_modules` 下的链接目标全是**绝对路径**，照抄会让副本与原件共享宿主代码（升副本连带改原件且不报错）→ 复制后按副本路径逐条重建。
- pnpm 账本里的 `virtualStoreDir` 记的是**原件**路径 → 不改就装不了插件，真按记录值来还会往原件里写。
- 本地 `file:` 依赖被记成相对路径，而**深度变了** → 副本要找绝对路径，`resolution.directory` 那处也必须一起换。

---

## 5. 写文件的边界（诚实说明）

启动器不注入 dsh 进程，但**确实会写下面这些地方**，都是你点什么才写什么：

- 自己的 `lanes.json`（登记表、主要版本、窗口几何）——**源码运行**时在脚本目录，
  **exe 运行**时在 exe 所在目录（便携，拷走就能用）；那个目录不可写时自动退到 `%APPDATA%\dsh-lanes`
- lane 的安装树（创建 / 升级 / 复制 / 删除）
- lane 的 `DSH_HOME`（复制过来的 API key、对话恢复、运行态文件 `<root>\run\<lane>.json`、备份目录 `<root>\backups\`）
- 「插件市场」单独开关（`market-off` / `market-on`）会往对应 profile 的补丁层里写一段**带起止标记**的块，用 `market-on` 可整段撤销——其它插件一个都不碰
- 升级**默认路径那份**（接管来的外部安装）时只写一个终端脚本
  `<root>\logs\upgrade-<lane>-<版本>-terminal.bat`，然后在新窗口里打开它；
  npm 由那个窗口执行，**本进程一行 npm 都不跑**（见第 8 节）

**它不做的事**：不碰 dsh 自己的源代码；不做通用插件开关（在真实 profile 上写坏过一次，已停用，
要用插件开关请去 dsh 自己的插件市场页面）；不给"这次升级安不安全"的判断结论。

> 顺带一个行为保证：**写运行态失败不会让界面崩**。`<root>\run\<lane>.json` 只是"记住上次是谁起的"，
> 写不进去（目录只读、盘符被限制等）时只在日志里警告一次，卡片改用现场探测显示状态——
> 这条是踩过坑之后专门加固的：以前它会以未捕获异常的形式打崩窗口（起不来、也关不掉）。

---

## 6. 命令速查

```
py dsh_lanes.py doctor                    环境自检
py dsh_lanes.py versions [--all] [--registry auto|official|mirror]
py dsh_lanes.py create <lane> <ver> [--port N] [--force] [--no-inherit] [--registry auto|official|mirror]
py dsh_lanes.py open <lane|ver> [--port N] [--cwd DIR] [--no-browser] [--detach] [--timeout N]
py dsh_lanes.py list
py dsh_lanes.py instances                 列出所有 DSH 实例（含不是本启动器启动的）
py dsh_lanes.py adopt <lane> [--install 路径] [--port N] [--home 目录]
py dsh_lanes.py remove <lane> [--force]   只注销登记，不动安装树与 HOME
py dsh_lanes.py primary [lane] [--clear]  ★ 主要版本：新建 lane 继承它的 API key
py dsh_lanes.py sync-key [lane ...]       把主要版本的 API key 补到已有 lane
py dsh_lanes.py delete <lane> [--stop] [--keep-home] [--keep-install] [--home-too] [--yes]
py dsh_lanes.py clone <源 lane> <新 lane> [--port N] [--no-inherit]
py dsh_lanes.py upgrade <lane> <版本|tag> [--stop] [--dry-run] [--no-boot-check]
                                          [--registry auto|official|mirror]
py dsh_lanes.py upgrade <lane> <版本|tag> --in-terminal
                                          外部安装（默认路径那份）：新开终端跑 npm i -g
py dsh_lanes.py upgrade <lane> --rollback
py dsh_lanes.py verify <lane> [--fix]
py dsh_lanes.py plugins <lane> [--dump]   插件清单（只读）
py dsh_lanes.py market <lane> [--offline] 插件市场现状
py dsh_lanes.py market-off <lane> [--no-boot-check] / market-on <lane>
py dsh_lanes.py market-update <lane> [--version V] [--anyway] [--no-boot-check] / market-rollback <lane>
py dsh_lanes.py chat-backup <lane> [--name 后缀] / chat-list <lane>
py dsh_lanes.py chat-restore <lane> [名称] [--stop] [--yes] [--force] / chat-rm <lane> <名称> [--yes]
py dsh_lanes.py chat-sync <lane> [--from LANE] [--dry-run] [--backup] [--stop] [--yes]
                                          把主 DSH 的对话同步进来（只搬对话，配置不动）
py dsh_lanes.py stop [lane ...]           缺省停全部
py dsh_lanes.py logs <lane> [--lines N]
py selftest.py [lane ...]                 全链路复检：open → 认证握手 → 重复 open → stop
```

`--root <目录>` 可以临时覆盖 lane 根目录。

---

## 7. 配置：`lanes.json`

- 首次 `create`（或在窗口里新建版本）时**自动生成**，形状见 `lanes.example.json`。
- 它记的是**你这台机器**的路径与登记表，属于本机配置——**不要提交、不要分享**（`.gitignore` 里已经排除）。
- 关键字段：`root`（lane 根目录）、`port_range`、`default_cwd`（决定会话归属，DSH 按工作目录分项目）、
  `primary`（主要版本，新建 lane 继承它的 API key）、`allow_scripts`（npm 11 起安装脚本要显式授权）、
  `registry`（下载源，见下）、`lanes`（登记表）。

### 界面状态：`ui.theme` / `ui.split`

`ui` 这一层只记窗口自己的样子，删掉整段就回到默认：

| 键 | 含义 | 默认 |
| --- | --- | --- |
| `ui.w` `ui.h` `ui.x` `ui.y` | 窗口大小与位置（关窗时写回） | 1060×720 居中 |
| `ui.theme` | `light`（浅海）/ `dark`（深海）；点顶栏 `◐` 切换 | `light` |
| `ui.split` | 左右分栏比例（0~1，拖中间那条缝就会更新） | `0.57` |

命令行可以临时换主题而不写进配置：`py dsh_lanes_gui.py --theme dark`。

### 下载源：`registry`（新建 / 升级时可逐次选择）

`registry` 取值：留空或 `auto`（默认，先官方再镜像）、`official`（`registry.npmjs.org`）、
`mirror`（`registry.npmmirror.com`），也可以直接写一个 http 地址（内网源；这种情况下不会自动换源）。

两条规矩：

1. **查版本和下载一定用同一个源。** 以前查版本走官方、下载走 npm 默认源（你 `.npmrc` 里的镜像），
   两边不一致就出事：官方当天发的新版，镜像常常只同步了一半——`@deepseek-ai/dsh@0.2.0-rc.1`
   镜像已经有了，可它的 12 个 `@deepseek-ai/dsh-*` 子包还没到（当时逐个核对了 259 个包），
   于是 npm 在第一个缺的子包上报 `ETARGET` 装不上。现在用的是同一个源。
2. **缺东西会自动换另一个源重试一次**（命令行加 `--registry`，GUI 在「新建版本 / 升级版本」
   对话框里有「下载源」三选一，勾「记住」就写进 `lanes.json`）。换源是安全的：这类失败发生在
   npm 写盘之前（日志停在 `reify:loadTrees`，安装树没被动过），实测过。日志里会写明换了哪个源。

命令行临时改一次：`py dsh_lanes.py upgrade next 0.2.0-rc.1 --registry mirror`。

### 不继承密钥和设置：`--no-inherit`

新建版本、复制一套都支持。默认是**整套继承**（老行为，不勾就不变）；勾上（或加 `--no-inherit`）之后：

| 照旧带 | 不带 |
| --- | --- |
| 安装树、对话与附件（`sessions\`、`attachments\`）、已装插件（`profiles\`）、插件自己的数据（语音模型、插件设置…） | `.credentials.yaml`（API key）、`.anonymous-user-id`（匿名身份）、`settings.yaml*`（DSH 设置）、`.agent-presets\`（agent 预设 / 人格） |

- **新建版本**本来只继承 API key 这一项，所以它的 `--no-inherit` 就等于"不复制 key"，首次启动自己填。
- **复制**的跳过范围写在 `dsh_lanes.py` 的 `NO_INHERIT_HOME_ITEMS` 一个清单里 —— GUI 勾选框和
  CLI 参数走的是同一份清单，要加减只改那里。
- 跳过了哪些会打在输出里（`未继承 : …`），lane 登记里记 `noInherit: true`。
- 副本因此**没有 API key**，卡片上会出现「补齐 API key…」，想补随时点（从主要版本复制一次）。
- 只跳 **HOME 顶层**的名字：`profiles\web\settings.yaml` 这种嵌套同名文件不会被误伤。

### 与主 DSH 同步对话：`chat-sync`

用途：副本（或任何一条 lane）里想看主 DSH 的新对话，又**不想**把主那边的密钥 / 设置 / 插件
重新灌进来。

```
py dsh_lanes.py chat-sync <目标 lane>              # 源＝主要版本
py dsh_lanes.py chat-sync <目标 lane> --dry-run    # 只算账，一个文件都不写
py dsh_lanes.py chat-sync <目标 lane> --from next  # 换源
```

| 搬 | 不搬 |
| --- | --- |
| `sessions\**`（会话正文）、`attachments\**`（附件）、`storages\session_projcache\**`（会话列表用的投影记录）、`storages\workspace.json`（工作区登记，并集合并） | `.credentials.yaml`、`.anonymous-user-id`、`settings.yaml*`、`profiles\`（插件与插件包）、各插件数据、`archivedSessionIds` / `pinnedSessionIds` |

三条规矩：

1. **缺的补上；同名按修改时间较新的赢**，覆盖了几条会打在屏幕上。
2. **投影缓存只跟"这次真动过的会话"走。** 那份记录里存着按**源**的会话文件算出来的检查点，
   给一个"目标更新、被跳过没动"的会话塞源记录，两边就对不上（记录说 5000 轮、文件 3000 轮）。
3. 复制保留 mtime —— 否则副本那份看起来"永远比源新"，下次同步就再也不动了。

安全默认：目标是主要版本时不给同步（自己跟自己没意义）；目标正在运行时**拒绝**（`--stop` 才替你停）；
`--backup` 先给目标存一份对话快照，同步错了能 `chat-restore` 回去；`storages\workspace.json`
写之前留 `.bak`。GUI 上就是卡片上的「同步对话…」（主要版本的卡片上没有这个按钮）。

---

## 8. 已知限制 / 有意不做

- **只测过 Windows。**
- **不给"升级安不安全"的结论。** 工具只能按预设逻辑工作；组合树、插件 peer 闸门这些是宿主内部事实，
  宿主一变，工具输出的"安全/不安全"就是拿过期规则替你决定，比不给更危险。
  它只做确凿能验证的部分：装没装上、起没起来（HTTP 200 + cookie 握手）、文件指纹对不对、能不能退回。
- **接管来的外部安装（npm 全局那一份）不在本进程里升级。** 那棵树在你的全局前缀里
  （`%APPDATA%\npm\node_modules\@deepseek-ai\dsh`），装它等于改你日常真正在用的那一套，
  npm 覆盖时还会连带动旁边的入口 shim。所以这条路改成了「只铺命令」：
  GUI 上点「在终端里升级」，CLI 加 `--in-terminal` —— 工具把
  `npm install -g @deepseek-ai/dsh@<版本>` 写成一个 `.bat`（落在 `<root>\logs\`），
  在新控制台窗口里打开它，**npm 由那个窗口跑**，进度和报错都在你眼前。
  装完回启动器点「刷新」：接管型 lane 的版本号按安装树自愈，不用手改 `lanes.json`。
  它正在运行时默认**先停掉它再开终端**（node 占着的文件 npm 覆盖不了），对话框里的勾选框
  可以关掉这个行为；装完从卡片上重新打开即可。
- **不做通用插件开关**（理由见第 5 节）。
- **三条 lane 的对话记忆不互通**：会话文件按版本隔离；跨版本要"记得"的东西应该放在记忆层
  （比如一个跨会话的记忆插件 / 记忆库），而不是会话文件层——会话格式在版本之间会变代际，
  强行共享迟早出问题。
- 端口目前是"自动挑第一个空闲"，没有固定分配策略。

---

## 9. 排障

| 症状 | 先看 |
| --- | --- |
| 不知道环境缺什么 | `py dsh_lanes.py doctor` |
| 起了但浏览器打不开 | `py dsh_lanes.py logs <lane>`（找 `dsh web: <url>` 那行） |
| 端口被别的东西占了 | `py dsh_lanes.py instances` |
| 副本一装插件就崩 | `py dsh_lanes.py verify <lane>`；再 `verify <lane> --fix` |
| 怀疑 profile 补丁层坏了 | `verify` 会顺手体检：调宿主自带那份 js-yaml 真解析一遍，报错原话带 `行:列` |
| 用 exe 起不来又没报错 | `--windowed` 的 GUI exe 没有控制台：改用 `dsh-lanes.exe doctor`（同目录）看报错 |
| 新建 / 升级报 npm `ETARGET` / `notarget` | 那个源还没同步到这一版：加 `--registry official`（或 `mirror`）换一个源再试，或等几小时。工具默认就会自动换另一个源重试一次 |
| 卡片上的版本和你自己 `npm i -g` 升过的不一致 | 不用管：每轮刷新都会从安装树读真实版本并更正台账（接管型 lane 的树在启动器之外，只读不改） |
| 新建 / 复制的 lane 一启动就让你填 API key | 建它时勾了「不继承密钥和设置」（`--no-inherit`）：点卡片上的「补齐 API key…」从主要版本补一次，或自己在界面里填 |
| 副本里看不到主 DSH 的新对话 | `py dsh_lanes.py chat-sync <副本> --dry-run` 先看会搬什么，不加 `--dry-run` 就同步（只搬对话，配置不动） |

---

## 10. 自己打包成 exe

```bat
py -m pip install pyinstaller      :: 只需一次
build_exe.bat                      :: 双击也行
```

产物（`dist\`，**是文件夹不是单个文件**）：

| 目录 | 打包参数 | 实测大小（zip 后） |
| --- | --- | --- |
| `dist\dsh-lanes\` | `--onedir --console` | 8.2 MB |
| `dist\dsh-lanes-gui\` | `--onedir --windowed` | 11.5 MB |

**为什么要两个**：`--windowed` 的 GUI exe 没有控制台，早期崩溃是**静默**的。
出问题时 `dsh-lanes.exe doctor` 是唯一能看到报错的入口。

**为什么默认 `--onedir` 而不是 `--onefile`**（实测踩过，不是偏好问题）：

`--onefile` 每次启动都要把自己解包到 `%TEMP%`。在开发这台机器上这一步会被拦掉：

```
[PYI-28584:ERROR] Could not create temporary directory!
（GUI 版没有控制台，只弹一个标题为 "Error" 的对话框 —— 双击看起来就是"什么都没发生/起不来"）
```

换成 `--onedir`（exe 旁边放 DLL 和 `_internal` 文件夹，**不需要解包**）后，CLI 与 GUI 都正常启动。
所以要分享的话：**把整个文件夹打包成 zip 发出去，别只拷 exe**（exe 离开那些文件跑不起来）。

**冻结后有两个行为会变**（源码里 `FROZEN` 分支，就这两处）：

- **配置位置**：`lanes.json` 跟着 **exe 所在目录**走。因为冻结后 `__file__` 指向 exe 自己的目录树，
  而 `--onefile` 那种临时解包目录一退出就没了。该目录不可写（例如塞进 `Program Files`）时退回 `%APPDATA%\dsh-lanes`。
- **默认工作区**：源码运行时取"脚本上两级目录"（也就是你放 dsh 项目的那个文件夹），
  冻结后那个位置没有意义，改用用户主目录。

**想要图标**：加 `--icon your.ico`（仓库里没带图标）。
**想试单文件**：把 `--onedir` 换回 `--onefile`，但先在你自己的机器上双击验证能不能起来。

仓库的 `.gitignore` 已经排除 `build/`、`dist/`、`*.spec`：**exe 不要提交进仓库**，
要分享就发到 GitHub 的 **Releases**（附件，把 zip 挂上去）。
打包好的两个 zip 就放在 `dist\`：`dsh-lanes-onedir.zip`（CLI）、`dsh-lanes-gui-onedir.zip`（GUI）。

---

## 11. 界面：「大肥鱼化」

v0.3.0 把界面换成一套自绘外观（品牌蓝 + 官方鲸鱼图标），**功能一行没减**：

- `ui_kit.py`（新增）：`RoundedButton` / `RoundedCard` / `Pill` / `Chip` / `MiniScrollbar`，
  纯 `tkinter` + `math`，零第三方依赖；API 向 `tk.Button` 靠拢，所以老的调用点不用改；
- 卡片改成圆角 + 悬停 + 左侧状态色条，版本号与运行状态改成芯片 / 胶囊；
- 日志面板重做：时间戳列、级别字形、小节留白、空状态、「自动滚动已暂停」提示，上限 4000 行；
- 顶栏三行压两行，dist-tag 改芯片，窄窗口自动少显示几个；
- `assets/app.ico`：官方 favicon 光栅化出来的鲸鱼，七档尺寸、透明底，窗口标题栏与 exe 共用；
- `tools/dsh_stub.cs` + `build_stub.bat`：46 KB 的 GUI 子系统 C# 壳，去找本机的 `pythonw`
  再拉起 `dsh_lanes_gui.py` —— 比 PyInstaller 文件夹版（2.3 MB + `_internal\` 22.3 MB）
  小两个数量级，而且改完 `.py` 不用重新打包。

v0.3.1 起外观有了「层」，v0.3.3 起整窗是一片海，v0.3.4 把两种天色拉回同一套关系：

- **配色令牌化**（`theme_whale.py`）：所有颜色、间距、字号、圆角、投影、动效收敛到一套令牌，
  控件只认语义名（`label2` / `layer1` / `brand` …）。顺带把对比度修到 WCAG AA 线上
  （旧次要文字 `#8A8F98` 在 `#F4F5F7` 上只有 **2.96:1**，新的 `label2` 是 **5.73:1** 浅海 / **7.27:1** 深海；
  令牌层自带 `py theme_whale.py --check` 会逐对验收 29 组，不达标直接退出码 1）。
- **浅海 / 深海两套主题**：顶栏 `◐` 一键切换，选择写进 `lanes.json → ui.theme`。
  Windows 的标题栏也跟着变深（`DwmSetWindowAttribute`，Win10 1809+ / Win11）——
  不然深海窗口顶上永远压着一条浅灰带。
- **两种主题是同一套关系，不是两套皮肤**：海水色值也进了主题表（`water_top` / `water_bottom` /
  `water_edge` / 光斑强度），两套只换明度不换色相；并且有一条**硬约束**：
  **卡片永远比海水亮、窗边那圈永远比卡片深**（`--check` 里会验，深海以前正好反着来，
  卡片像在发亮的水面上凿出来的洞）。
- **整窗海面**：窗口底色不是一块纯色，而是一张按窗口尺寸渲染的海面图：上浅下深的底色
  ＋四周压深（渐晕，不然最上面那条 16px 的缝就是"白边"）＋三处光斑。Tk 的控件全都不透明，
  所以海只在"没被控件盖住"的地方露出来 —— 顶栏、底栏是浮在水上的圆角卡，左列卡片之间留 12px 缝，
  中间分栏缝 12px；卡片圆角外面那圈缺口单独贴了一张同位置的海面切片，不会露出色块。
- **换主题不重启进程**：原地双缓冲重建（新的一套先在海水底下建好、画好，再一次性翻上来），
  首次 ~0.65 秒、之后 ~0.3 秒，日志/分栏/滚动位置全部带过去，中途不会露出半成品那一帧。
- **左右分栏可拖拽**：中间那条缝能拖（悬停会亮成品牌蓝，松手就复位），比例写回
  `lanes.json → ui.split`，下次打开还是这个比例；两条 lane 卡片列表和日志面板各自有最小宽度，
  拖不到会把对方挤没。

v0.3.9 把「拖动/滚轮一闪一闪」这条线收掉了（细节见 CHANGELOG）：卡片的圆角面不再
"每换一个宽度就整块重渲染"，而是**尺寸一变先画快版**（四个抗锯齿角贴图 + 直线矩形，
约 0.1ms），停下 140ms 后再补完整版（含投影）；滚动时也不再递归标脏几十个子窗口，
并给画布加了 `WS_CLIPCHILDREN`。代价与边界也写在 CHANGELOG 里：`RDW_ALLCHILDREN` 是
**省不掉**的（省掉后滚完卡片会整片空白，`heal()` 都救不回来）。

细节、以及顺手修掉的四个坑（黑窗口 / 受限令牌 / 日志占用 / 端口被占）见
[docs/界面美化.md](docs/界面美化.md)。单独看控件：`py ui_kit_demo.py`（浅海/深海都能切）。

---

## 12. 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。当前版本 **v0.3.9**（2026-10-26）：

- **拖动 / 滚轮不再一闪一闪**：卡片改成「快版 / 完整版」两档画 —— 尺寸一变就按**此刻**
  的尺寸画快版（4 个 AA 角贴图 + 直线矩形，约 0.1ms），停下 140ms 后再补抗锯齿 + 投影的
  完整版。拖动 30 步里"画的面和小一截"从平均 2.8/4 张降到 **0**，完整重画从 56 次降到 0；
- **滚动**：每格从标脏两次降到一次，并给画布加 `WS_CLIPCHILDREN` 让滚动只搬画布自己；
  平色主题下不再在滚动停下后把每张卡重画一遍（那一下白闪没了）；每格 13~60ms → **11~30ms**；
- **悬停卡片** 72.8ms → **1.2ms**（以前换描边色＝换缓存键＝整块冷渲染）；
- **分栏缝松手后复位**（以前指针不移开就一直亮着品牌蓝，看着像被"选中"）；
- 卡片超采样 3× → 2×、渲染缓存上限 700 → 240；
- 回归探针：`_t_wheel` / `_t_flicker` / `_t_split` / `_t_theme` / `theme_whale --check` /
  `--selfcheck` / `ui_kit_demo --smoke` 全 PASS；`_t_wheel` 的像素判据与 v0.3.8 一致。

上一版 **v0.3.8**（2026-10-26）：

- **配色回到 v0.3.1~v0.3.3 那套平色**（用户截图逐像素对过：浅海 `#E9F3FC` 底 +
  `#F3F9FE` 卡片 + `#C6DCF2` 描边，深海 `#0A1428` + `#0F1E38` + `#1D3252`）；
  那两版**根本没有海面**，所以 v0.3.4 加的纵向渐变 + 三处光斑整条撤掉：
  `flat: True` → `ocean_image` 直接返回 None，窗口背景就是一整块 `bg_base`，
  一张图都不生成（"改尺寸重画海面"这一整类隐患随之消失，实测真重算 0 次）；
- 日志槽底色改回"跟卡片同色"（新令牌 `well` = layer1），右栏不再是一大片水色；
- 窗口标题和状态条左下角常驻版本号 `v0.3.8`，一眼能确认开的是不是最新那版；
- 对比度自检 30 项全绿（15 × 2），4 个回归测试（flicker / wheel / split / theme）全 PASS。

上一版 **v0.3.7**（2026-10-26）：

- **不闪了（第二层）**：v0.3.6 去掉 `RDW_ERASE` 之后擦除是 0 了，但"标脏"还是
  `after(40ms)` 排队 + 每次事件重排，拖动时事件间隔 16~45ms 永远比 40ms 短 ——
  那个任务一次也跑不到，左列在整个拖动过程中都是空的。现在改成**立刻标脏**
  （几微秒，1 秒 60 次也不花钱）。实测 8 步拖分栏：标脏 **0 → 32 次**，
  每格滚轮 16.8 → 11.4ms；
- 浅海色值确认与 v0.3.4 完全一致（逐像素比过）；看着淡是窗口尺寸不同 ——
  海面渐变按窗口高度算，矮窗口走不到最深那一档。

上一版 **v0.3.6**（2026-10-26）：

- 滚轮/拖窗口/拖分栏时 `RedrawWindow` 不再带 `ERASE`（擦一遍底色再画，中间那一帧就是闪）
  和 `UPDATENOW`（同步重画会撕成"一半新一半旧"）。实测擦除：滚 8 格 **13 → 0**、
  拖窗口 **33 → 0**、拖分栏 **13 → 0**；
- 浅海退回 v0.3.4 的配色；对比度自检 34 → 32 项（撤掉了与"要柔和"打架的那条）；
- 卡片自检不再被 `winfo_ismapped` 蒙住（canvas 窗口项滚出可视区即被标未映射）。

上一版 **v0.3.5**（2026-10-26）：

- **滚一下卡片就白**：Windows 上 canvas 的窗口项是真子窗口，Tk 搬完像素不会让它重画 →
  按 HWND 强制重画兜底；
- 滚区高度不再用 `winfo_reqheight()`（列表底图就是那个 Label 的 image，等于自己往上反馈，
  表现为多一截空海 / 最后一张卡被压扁滚不到）；
- 滚轮直接绑在列表控件树上（`bind_all` + 反推指针会丢事件）、高精度滚轮累积 delta；
- 浅海压深一档（v0.3.6 已退回）。

上一版 **v0.3.4**（2026-10-26）：

- **两种模式拉回同一套关系**：海水色值进主题表（`water_*`），两套只换明度不换色相；新增约束
  「卡片永远比海水亮、窗边永远比卡片深」并写进 `--check`（深海以前反着来）；
- **标题栏跟着主题变深**（`DwmSetWindowAttribute`，Win10 1809+/Win11），深海窗口顶上的浅灰条没了；
- **换主题不再重启进程**：原地双缓冲重建，首次 ~0.65 秒、之后 ~0.3 秒，中途不再露出半成品；
- 顺手修：`OceanBackdrop._name` 撞上 `tkinter.Misc` 的内部属性，深海的海面一直在按浅海渲染；
  根目录可写性探测（会重试 3×150ms）挪到后台线程。

上一版 **v0.3.3**（2026-10-26）：

- **整窗海面**：海面背景铺满整个窗口（`OceanBackdrop` + 按位置裁的切片），左右分栏拖动后
  比例记在 `ui.split`；四周边距、卡片间距一起放大，卡片才像"浮在水上"；
- **可拖拽分栏**：`tk.PanedWindow`，缝 12px、悬停亮蓝、两条边都有最小宽度；
- **修掉切换主题/关窗时的控制台噪音**：销毁前取消 `after` 排队的任务
  （`invalid command name "..._poll_queue"` 那串）。

上一版 **v0.3.2**（2026-10-26）：

- **浅海 / 深海切换**：顶栏 `◐` 按钮 ＋ `--theme` 命令行；`ui.theme` 记在 `lanes.json`；
- 修掉三个只在重建窗口时才暴露的坑：`_save_geometry` 整体覆盖会把 `ui.theme` 抹掉、
  换 Tk 根后旧 `PhotoImage` 缓存报 `image "pyimage1" doesn't exist`、重启循环里窗口
  位置会跳。

上一版 **v0.3.1**（2026-10-26）：

- **配色令牌化**：新增 `theme_whale.py`（26 组对比度自检 + 间距/字号/圆角/投影/动效令牌），
  `ui_kit.py` 与 `dsh_lanes_gui.py` 里写死的十六进制清零；
- **可访问性**：旧次要文字只有 2.96:1（低于 WCAG AA 的 4.5:1），新令牌全部 ≥4.5:1，
  焦点环 2px、按钮 Enter/Space 可触发。

上一版 **v0.3.0**（2026-10-25）：

- **界面重做**：自绘控件库 `ui_kit.py`、圆角卡片 + 状态色条、日志面板重做、顶栏重排；
- **图标**：`assets/app.ico` 换成官方鲸鱼（七档尺寸、透明底、品牌蓝）；
- **46 KB 轻量壳**：`tools/dsh_stub.cs`，GUI 子系统、不弹黑窗口；
- **顺手修的四件事**：`subprocess` 统一 `CREATE_NO_WINDOW`；受限令牌（沙箱）在界面上直接点名；
  日志文件被占用时降级而不拦死「打开 lane」；端口被占但读不到 PID 时直接接管。

上一版 **v0.2.3**（2026-10-24）：

- 「默认路径那份」也能升级了：GUI 的升级对话框里多了「在终端里升级」，
  CLI 是新旗标 `upgrade <lane> <版本> --in-terminal`；工具只把
  `npm install -g …` 写成脚本并在**新终端**里打开，npm 不在启动器进程里跑。
- 外部安装的版本列表里，"已装"标记改按**它自己那棵树**核对（以前错看启动器的
  `versions\` 目录，所以从不显示）。

上一版 **v0.2.2**（2026-10-13）：

- 新建 / 复制可以选择「不继承密钥和设置」（`--no-inherit`）—— 不带 API key、匿名身份、
  DSH 设置与 agent 预设，插件、插件数据、对话照旧复制；
- 新增「与主 DSH 同步对话」（`chat-sync <lane>`）—— 只搬对话、附件、会话列表记录与工作区登记，
  密钥 / 设置 / 插件一律不动；缺的补上、同名按修改时间较新的赢。

上一版 **v0.2.1**（2026-10-01）：

- 台账自愈：接管型 lane（npm 全局那份）的版本号不再停在登记那一刻，每轮刷新按安装树更正；
- 新建 / 升级可以自己选下载源（官方源 / 镜像源），查版本与下载保证同源，缺包自动换源重试一次；
- 打包改为 `--onedir`（`--onefile` 在本机起不来）；
- 修掉"运行态写不进去 → 窗口变成关不掉的半成品"。

---

## 13. 许可

MIT，见 [LICENSE](LICENSE)。
