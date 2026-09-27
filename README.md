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

- 自己的 `lanes.json`（登记表、主要版本、窗口几何）
- lane 的安装树（创建 / 升级 / 复制 / 删除）
- lane 的 `DSH_HOME`（复制过来的 API key、对话恢复、运行态文件 `<root>\run\<lane>.json`、备份目录 `<root>\backups\`）
- 「插件市场」单独开关（`market-off` / `market-on`）会往对应 profile 的补丁层里写一段**带起止标记**的块，用 `market-on` 可整段撤销——其它插件一个都不碰

**它不做的事**：不碰 dsh 自己的源代码；不做通用插件开关（在真实 profile 上写坏过一次，已停用，
要用插件开关请去 dsh 自己的插件市场页面）；不给"这次升级安不安全"的判断结论。

---

## 6. 命令速查

```
py dsh_lanes.py doctor                    环境自检
py dsh_lanes.py versions [--all]          查 registry 版本与 dist-tag
py dsh_lanes.py create <lane> <ver> [--port N] [--force]
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
  `primary`（主要版本，新建 lane 继承它的 API key）、`allow_scripts`（npm 11 起安装脚本要显式授权）、`lanes`（登记表）。

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

---

## 10. 许可

MIT，见 [LICENSE](LICENSE)。
