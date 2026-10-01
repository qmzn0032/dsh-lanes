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
| **复制一整套** | `clone <源> <新>` | 安装树 + `DSH_HOME`（会话、设置、凭据、已装插件）整套复制成副本，动副本不碰原件 |
| **升到指定版本** | `upgrade <lane> <版本>` | 就地升级 + 核对 + 启动冒烟，起不来自动退回；`--rollback` 回上一版 |
| 核对隔离 | `verify <lane>` | 逐条比对链接、版本、pnpm 账本、本地依赖，并体检 profile 补丁层 |
| 备份对话 | `chat-backup` / `chat-restore` | 会话整棵备份（逐文件指纹）+ 恢复（先校验快照、再另存现状） |
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
py dsh_lanes.py create <lane> <ver> [--port N] [--force] [--registry auto|official|mirror]
py dsh_lanes.py open <lane|ver> [--port N] [--cwd DIR] [--no-browser] [--detach] [--timeout N]
py dsh_lanes.py list
py dsh_lanes.py instances                 列出所有 DSH 实例（含不是本启动器启动的）
py dsh_lanes.py adopt <lane> [--install 路径] [--port N] [--home 目录]
py dsh_lanes.py remove <lane> [--force]   只注销登记，不动安装树与 HOME
py dsh_lanes.py primary [lane] [--clear]  ★ 主要版本：新建 lane 继承它的 API key
py dsh_lanes.py sync-key [lane ...]       把主要版本的 API key 补到已有 lane
py dsh_lanes.py delete <lane> [--stop] [--keep-home] [--keep-install] [--home-too] [--yes]
py dsh_lanes.py clone <源 lane> <新 lane> [--port N]
py dsh_lanes.py upgrade <lane> <版本|tag> [--stop] [--dry-run] [--no-boot-check]
                                          [--registry auto|official|mirror]
py dsh_lanes.py upgrade <lane> --rollback
py dsh_lanes.py verify <lane> [--fix]
py dsh_lanes.py plugins <lane> [--dump]   插件清单（只读）
py dsh_lanes.py market <lane> [--offline] 插件市场现状
py dsh_lanes.py market-off <lane> [--no-boot-check] / market-on <lane>
py dsh_lanes.py market-update <lane> [--version V] [--anyway] [--no-boot-check] / market-rollback <lane>
py dsh_lanes.py chat-backup <lane> [--name 后缀] / chat-list <lane>
py dsh_lanes.py chat-restore <lane> [名称] [--stop] [--yes] [--force] / chat-rm <lane> <名称> [--yes]
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

---

## 8. 已知限制 / 有意不做

- **只测过 Windows。**
- **不给"升级安不安全"的结论。** 工具只能按预设逻辑工作；组合树、插件 peer 闸门这些是宿主内部事实，
  宿主一变，工具输出的"安全/不安全"就是拿过期规则替你决定，比不给更危险。
  它只做确凿能验证的部分：装没装上、起没起来（HTTP 200 + cookie 握手）、文件指纹对不对、能不能退回。
- **接管来的外部安装（npm 全局那一份）不参与 `upgrade`**：那等于直接改你正在用的那一份。
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

## 11. 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。当前版本 **v0.2.1**（2026-10-01）：

- 台账自愈：接管型 lane（npm 全局那份）的版本号不再停在登记那一刻，每轮刷新按安装树更正；
- 新建 / 升级可以自己选下载源（官方源 / 镜像源），查版本与下载保证同源，缺包自动换源重试一次；
- 打包改为 `--onedir`（`--onefile` 在本机起不来）；
- 修掉"运行态写不进去 → 窗口变成关不掉的半成品"。

---

## 12. 许可

MIT，见 [LICENSE](LICENSE)。
