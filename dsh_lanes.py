#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DSH 多版本启动器（第一步：创建 / 打开指定版本）
================================================

纯 Python 标准库实现，不依赖任何第三方包，也不需要 pnpm / npx。

核心能力
--------
1. `versions` —— 查询 npm registry 上 @deepseek-ai/dsh 的全部版本与 dist-tag（纯脚本 HTTP）
2. `create`   —— 把指定版本装进独立目录（互不覆盖）并登记成一条 lane
3. `open`     —— 用该 lane 自己的 DSH_HOME / 端口启动 dsh web，并打开带 token 的正确 URL
4. `list` / `stop` / `logs` / `doctor` —— 查看、停止、排障

为什么每个 lane 要独立 DSH_HOME
--------------------------------
DSH 启动时会把 `$DSH_HOME/profiles/node_modules` 的模块回退链接重新指向**当次启动所用安装**的
依赖闭包（dsh-app-boot 的 healProfilesModuleFallback）。两套版本共用同一个 home，会随着谁启动
就把这份共享镜像往谁那边治一次。所以 lane 之间必须隔离安装目录 + 隔离 home。

目录布局（默认 root = D:\\dsh-lanes，可在 lanes.json 改）
--------------------------------------------------------
    <root>/
      versions/<版本>/        npm --prefix 目标：node_modules/@deepseek-ai/dsh/lib/bin.js
      homes/<lane>/           DSH_HOME：settings/sessions/credentials/profiles
      run/<lane>.json         运行态：pid / port / url
      logs/<lane>.log         启动与安装日志

用法
----
    py dsh_lanes.py doctor
    py dsh_lanes.py versions
    py dsh_lanes.py create next 0.1.7-rc.2
    py dsh_lanes.py open next
    py dsh_lanes.py open 0.1.7-rc.2 --port 3082
    py dsh_lanes.py list
    py dsh_lanes.py logs next
    py dsh_lanes.py stop next
    py dsh_lanes.py primary global      # 标记常用版本：新建 lane 会继承它的 API key
    py dsh_lanes.py delete next --stop  # 删除一套 DSH（登记 + 安装树 + HOME）
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import http.cookiejar
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# ======================= 常量与默认配置 =======================

# 冻结成 exe（PyInstaller）之后 `__file__` 指向**临时解包目录**（_MEIPASS）：
# 配置必须跟着 exe 走，跟着临时目录走的话一退出就没了。
FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """本程序自己的目录：源码运行＝脚本目录；冻结后＝exe 所在目录。

    冻结后优先把 `lanes.json` 放在 exe 旁边（便携、拷走就能用）；
    那个目录不可写时（例如被塞进 Program Files）退到 `%APPDATA%\\dsh-lanes`，
    避免"第一次保存配置就崩"。
    """
    if not FROZEN:
        return Path(__file__).resolve().parent
    base = Path(sys.executable).resolve().parent
    try:
        probe = base / ".dsh-lanes-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "dsh-lanes" if appdata else base


SCRIPT_DIR = app_dir()
CONFIG_PATH = SCRIPT_DIR / "lanes.json"

# 提示文字里"下一步该敲什么"用的程序名：源码是 .py，冻结后是 .exe 自己的名字
ME_NAME = Path(sys.executable).name if FROZEN else Path(__file__).name
PKG = "@deepseek-ai/dsh"
PKG_URL_NAME = "@deepseek-ai%2Fdsh"

# 官方代码库。取自已装包 package.json 的 repository 字段
# （git+https://github.com/deepseek-ai/deepseek-harness.git，directory: apps/cli），
# 2026-09-27 用本机那份 0.1.7-rc.2 核对过。
REPO_URL = "https://github.com/deepseek-ai/deepseek-harness"

REGISTRY_CANDIDATES = [
    "https://registry.npmjs.org",
    "https://registry.npmmirror.com",
]

# npm 11 起 install script 默认需要显式授权（RFC npm/rfcs#868）。
# 这张表必须写在**目标目录的 package.json 的 allowScripts 对象**里：
#   - 数组形式无效（会被当成键 "0"/"1"...），必须是 {"包名": true}
#   - 环境变量 npm_config_allow_scripts / --allow-scripts 在项目式安装里会被 npm 直接拒绝
DEFAULT_ALLOW_SCRIPTS = {
    "@deepseek-ai/dsh-subprocess-local": True,
    "koffi": True,
    "node-pty": True,
    "@google/genai": True,
    "protobufjs": True,
}

DEFAULT_CONFIG = {
    "root": "",
    "node": "",
    "npm_cli": "",
    "registry": "",
    "port_range": [3080, 3129],
    "default_cwd": "",
    "startup_timeout": 240,
    "open_browser": True,
    # 「主要版本」：你日常用的那条 lane。新建 lane 时会自动把它的 API key 复制过去，
    # 删除它需要二次确认（GUI 里要手打 lane 名）。
    "primary": "",
    "allow_scripts": DEFAULT_ALLOW_SCRIPTS,
    "lanes": {},
}

# ======================= 终端输出 =======================


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GRAY = "\033[90m"


def setup_terminal() -> None:
    if os.name == "nt":
        try:
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                k.SetConsoleMode(h, mode.value | 0x0004)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def col(text: str, color: str = "") -> str:
    return f"{color}{text}{C.RESET}" if color else text


def info(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(col("  [OK] ", C.GREEN) + msg, flush=True)


def warn(msg: str) -> None:
    print(col("  [!!] ", C.YELLOW) + msg, flush=True)


def fail(msg: str) -> None:
    print(col("  [XX] ", C.RED) + msg, flush=True)


# ======================= 配置读写 =======================


def default_root() -> str:
    """默认 lane 根目录：优先 D 盘（固定盘、空间大），否则回落用户目录。"""
    for cand in ("D:\\dsh-lanes", "C:\\dsh-lanes"):
        drive = Path(cand).drive
        if drive and Path(drive + "\\").exists():
            return cand
    return str(Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "dsh-lanes")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as exc:
            fail(f"lanes.json 解析失败，改用默认配置：{exc}")
    if not cfg.get("root"):
        cfg["root"] = default_root()
    if not cfg.get("default_cwd"):
        if FROZEN:
            # 冻结后 __file__ 在临时解包目录里，"上两级"没有任何意义 → 用用户主目录
            cfg["default_cwd"] = str(Path.home())
        else:
            # 脚本位于 <工作区>\启动器\ 下，默认工作区取上两级
            try:
                cfg["default_cwd"] = str(Path(__file__).resolve().parents[2])
            except IndexError:
                cfg["default_cwd"] = str(Path.home())
    merged = dict(DEFAULT_ALLOW_SCRIPTS)
    merged.update(cfg.get("allow_scripts") or {})
    cfg["allow_scripts"] = merged
    cfg.setdefault("lanes", {})
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(cfg)
    CONFIG_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def paths(cfg: dict) -> dict:
    root = Path(cfg["root"])
    return {
        "root": root,
        "versions": root / "versions",
        # clone 出来的安装树放这里，而不是 versions/<版本>：
        # versions 是按"版本"命名的，两条 lane 同版本会撞车，而且那样无法原地升级副本
        # （升级会把同版本的别的 lane 一起改掉）。
        "clones": root / "clones",
        "homes": root / "homes",
        "run": root / "run",
        "logs": root / "logs",
        # 备份放这里：chat/<lane>/<时间戳> = 对话记录快照，market/<lane>/<时间戳> = 插件市场更新前的快照
        "backups": root / "backups",
    }


def ensure_layout(cfg: dict) -> dict:
    p = paths(cfg)
    for key in ("root", "versions", "clones", "homes", "run", "logs", "backups"):
        p[key].mkdir(parents=True, exist_ok=True)
    return p


# ======================= 工具函数 =======================


def find_node(cfg: dict) -> str | None:
    if cfg.get("node") and Path(cfg["node"]).is_file():
        return cfg["node"]
    found = shutil.which("node")
    if found:
        return found
    for cand in (
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe"),
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def find_npm_cli(cfg: dict, node: str | None) -> str | None:
    if cfg.get("npm_cli") and Path(cfg["npm_cli"]).is_file():
        return cfg["npm_cli"]
    if node:
        cand = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if cand.is_file():
            return str(cand)
    for cand in (
        r"C:\Program Files\nodejs\node_modules\npm\bin\npm-cli.js",
        str(Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "npm" / "bin" / "npm-cli.js"),
    ):
        if cand and Path(cand).is_file():
            return cand
    return None


def _read_tcp_table() -> dict[int, int] | None:
    """读一次 TCP LISTEN 表 → {端口: PID}。读不到返回 None（调用方决定怎么降级）。

    直接调 iphlpapi 的 GetExtendedTcpTable：不 spawn 子进程、不用管道，
    也不依赖 netstat / WMI（本机沙箱下这两条都是不可用的）。
    """
    if os.name != "nt":
        return {}
    AF_INET = 2
    TCP_TABLE_OWNER_PID_LISTENER = 3
    ERROR_INSUFFICIENT_BUFFER = 122
    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    ret = iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if ret != ERROR_INSUFFICIENT_BUFFER:
        return None
    buf = ctypes.create_string_buffer(size.value)
    ret = iphlpapi.GetExtendedTcpTable(
        buf, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
    )
    if ret != 0:
        return None
    raw = buf.raw
    count = struct.unpack_from("<I", raw, 0)[0]
    rows: dict[int, int] = {}
    row_size = 24  # MIB_TCPROW_OWNER_PID = 6 个 DWORD
    for index in range(count):
        base = 4 + index * row_size
        if base + row_size > len(raw):
            break
        local_port = struct.unpack_from(">H", raw, base + 8)[0]  # 低 16 位、网络字节序
        pid = struct.unpack_from("<I", raw, base + 20)[0]
        rows[local_port] = pid
    return rows


_tcp_cache: dict = {"at": 0.0, "rows": None}
_tcp_lock = threading.Lock()


def tcp_listeners(ttl: float = 0.5, force: bool = False) -> dict[int, int]:
    """TCP LISTEN 端口 → 持有进程 PID（带 TTL 缓存）。

    为什么要缓存：GUI 每轮刷新会问很多次（每条 lane、实例发现、端口挑选），
    读一次表本身只要 0.3ms，但重复十几次也没必要。
    """
    with _tcp_lock:
        cached = _tcp_cache["rows"]
        if (
            not force
            and ttl > 0
            and cached is not None
            and time.monotonic() - _tcp_cache["at"] < ttl
        ):
            return dict(cached)
    rows = _read_tcp_table()
    if rows is None:
        with _tcp_lock:
            return dict(_tcp_cache["rows"] or {})  # 读失败就用上一次成功的结果
    with _tcp_lock:
        _tcp_cache["at"] = time.monotonic()
        _tcp_cache["rows"] = rows
    return dict(rows)


def invalidate_tcp_cache() -> None:
    """启动 / 停止 / 删除之后就地把缓存作废，别让界面看到过期状态。"""
    with _tcp_lock:
        _tcp_cache["at"] = 0.0


def listening_pid(port: int) -> int:
    """这个端口上有监听进程吗？有就返回它的 PID，否则 0。"""
    try:
        return int(tcp_listeners().get(int(port), 0))
    except (TypeError, ValueError):
        return 0


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已被占用。

    走 TCP LISTEN 表，**不用"试着连一下"**：本机实测连一个没有监听的端口不会被立刻
    拒绝，而是要等满 600ms 超时（安全软件把 SYN 丢了），每次判断都卡 0.6 秒——
    这是启动器界面卡顿的根因。读整张表只要 0.3ms，而且"端口被占了"的权威判据本来就是
    表里有没有 LISTEN 项，不是连得上连不上。
    """
    if int(port) in tcp_listeners():
        return True
    if _tcp_cache["rows"] is None and os.name == "nt":
        # 表读不到（极少见）→ 退回连接探测：慢，但绝不把已占用的端口误判成空闲
        try:
            with socket.create_connection((host, int(port)), timeout=0.6):
                return True
        except OSError:
            return False
    return False


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_uint32()
        if not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(handle)


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


# ======================= 实例发现（纯 ctypes，不依赖 netstat / WMI） =======================

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_image(pid: int) -> str:
    """取进程可执行文件全路径；取不到返回空串（权限/沙箱限制都可能）。"""
    if os.name != "nt" or pid <= 0:
        return ""
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = ctypes.c_ulong(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k.CloseHandle(handle)


def probe_dsh_like(port: int, host: str = "127.0.0.1") -> bool:
    """没带 token 的裸 GET 返回 401 —— 这正是 DSH 信任围栏的特征
    （dsh-client-connection：没有有效 cookie 一律 401）。用来把"别的程序占了这个端口"区分开。"""
    import http.client

    try:
        conn = http.client.HTTPConnection(host, port, timeout=1.5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        code = resp.status
        resp.read(64)
        conn.close()
        return code == 401
    except Exception:
        return False


def process_start_time(pid: int) -> str:
    """进程启动时间（本机时区字符串）。用于识别"这个实例是什么时候起来的"。"""
    if os.name != "nt" or pid <= 0:
        return ""
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    try:
        created, exited, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not k.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return ""
        ticks = (created.high << 32) | created.low
        unix = ticks / 1e7 - 11644473600  # FILETIME 纪元(1601) 与 Unix 纪元之差
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(unix))
    finally:
        k.CloseHandle(handle)


def find_installs(cfg: dict | None = None) -> list[dict]:
    """枚举本机所有 `@deepseek-ai/dsh` 安装树：npm 全局前缀 / npx 缓存 / PATH / 各 lane 版本目录。

    为什么要连 npx 缓存一起找：老启动器用的是 `npx @deepseek-ai/dsh web`，
    而 npx 装的是**缓存目录**里那份，与 `npm i -g` 的全局目录不是同一棵树——只看全局会漏。
    每条都带来源标签，方便判断"到底在用哪一份"。
    """
    results: list[dict] = []
    seen: set[str] = set()

    def add(path: Path, source: str) -> None:
        key = str(path).lower()
        if key in seen:
            return
        manifest = path / "package.json"
        if not manifest.is_file():
            return
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            return
        if data.get("name") != PKG:
            return
        seen.add(key)
        try:
            mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest.stat().st_mtime))
        except OSError:
            mtime = ""
        results.append(
            {
                "version": data.get("version", "?"),
                "path": str(path),
                "source": source,
                "installedAt": mtime,
            }
        )

    appdata = os.environ.get("APPDATA")
    if appdata:
        add(Path(appdata) / "npm" / "node_modules" / "@deepseek-ai" / "dsh", "npm 全局")
    which = shutil.which("dsh")
    if which:
        try:
            add(Path(which).resolve().parent / "node_modules" / "@deepseek-ai" / "dsh", "PATH 上的 dsh")
        except OSError:
            pass
    add(Path(r"C:\Program Files\nodejs\node_modules\@deepseek-ai\dsh"), "nodejs 安装目录")

    local = os.environ.get("LOCALAPPDATA")
    if local:
        npx_dir = Path(local) / "npm-cache" / "_npx"
        if npx_dir.is_dir():
            try:
                for entry in npx_dir.iterdir():
                    add(entry / "node_modules" / "@deepseek-ai" / "dsh", "npx 缓存")
            except OSError:
                pass

    if cfg:
        for lane, data in cfg.get("lanes", {}).items():
            if isinstance(data, dict) and data.get("version"):
                # 注意：lane 目录自己的 package.json 是 npm --prefix 的前缀清单
                # （name = dsh-lane-<版本>），真正的 dsh 清单在嵌套的 node_modules 里。
                add(
                    paths(cfg)["versions"] / str(data["version"]) / "node_modules" / "@deepseek-ai" / "dsh",
                    f"lane {lane}",
                )

    return results


def find_global_install() -> dict | None:
    """只取 npm 全局那份（兼容旧调用）；找不到返回 None。"""
    for item in find_installs(None):
        if item["source"] == "npm 全局":
            return item
    return None


def discover_instances(cfg: dict) -> list[dict]:
    """列出"不是本启动器启动的" DSH web 实例（例如全局安装那套 3080）。

    收录条件：端口落在 port_range（DSH 约定 3080-3129）内，或带 401 围栏特征。
    """
    listeners = tcp_listeners()
    lo, hi = cfg.get("port_range", [3080, 3129])
    owned: dict[int, str] = {}
    for lane in cfg.get("lanes", {}):
        rt = read_run(cfg, lane)
        if rt and int(rt.get("port") or 0):
            owned[int(rt["port"])] = lane

    found: list[dict] = []
    for port in sorted(listeners):
        pid = listeners[port]
        lane = owned.get(port)
        if lane and pid_alive(pid):
            continue  # 本启动器管理的，卡片区已经有了
        in_range = int(lo) <= port <= int(hi)
        image = process_image(pid)
        exe = Path(image).name if image else ""
        node_like = bool(re.search(r"node|dsh|harness", exe, re.I))
        # 先把明显无关的端口排除掉，再谈 HTTP 探测：
        # 探测是有超时的网络请求，如果对每个监听端口都探一遍，几十个端口就能拖住十几秒
        # （GUI 在 __init__ 里调它，会表现为"窗口一直不出来"）。
        if not in_range and not node_like:
            continue
        # 401 围栏也不是 DSH 独有（实测 VS Code 调试端口也 401），所以段外还要进程名像 node/dsh。
        dsh_like = probe_dsh_like(port)
        if not in_range and not (dsh_like and node_like):
            continue
        found.append(
            {
                "port": port,
                "pid": pid,
                "exe": exe or "?",
                "image": image,
                "dsh_like": dsh_like,
                "started": process_start_time(pid),
                "url": f"http://127.0.0.1:{port}",
            }
        )
    return found


def read_run(cfg: dict, lane: str) -> dict | None:
    f = paths(cfg)["run"] / f"{lane}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_run(cfg: dict, lane: str, data: dict) -> None:
    p = ensure_layout(cfg)
    (p["run"] / f"{lane}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_run(cfg: dict, lane: str) -> None:
    f = paths(cfg)["run"] / f"{lane}.json"
    try:
        f.unlink()
    except FileNotFoundError:
        pass


def lane_runtime(cfg: dict, lane: str) -> dict | None:
    """返回仍在运行的运行态记录；进程已死则顺手清理。

    被"接管"的实例（不是本启动器启动的）要额外核对进程启动时间：
    PID 会被系统回收，只靠 pid_alive 会把一个不相干的新进程误判成"还在跑"。
    """
    data = read_run(cfg, lane)
    if not data:
        return None
    pid = int(data.get("pid", 0))
    if not pid_alive(pid):
        clear_run(cfg, lane)
        return None
    expect = data.get("procStartedAt")
    if data.get("adopted") and expect:
        actual = process_start_time(pid)
        if actual and actual != expect:
            clear_run(cfg, lane)
            return None
    return data


def reconcile_lane(cfg: dict, lane: str) -> dict | None:
    """刷新时的自愈：没有存活运行态、但这条 lane 的端口上是个 DSH 实例 → 自动重新接管。

    为什么要这一步：你用别的方式（老的启动.bat / npx）重启那套 DSH 时，
    运行态里记的 PID 就过期了。没有自愈的话卡片会一直显示"已停止"，
    直到你手动点一次「打开」。这里让它自己认回来。

    成本控制：先查 TCP 表里这个端口有没有监听（0.1ms，一表管所有 lane）、
    再看进程名像不像 node/dsh，最后才做一次带超时的 401 围栏探测。
    """
    run = lane_runtime(cfg, lane)
    if run:
        return run
    lane_data = cfg.get("lanes", {}).get(lane)
    if not isinstance(lane_data, dict):
        return None
    port = int(lane_data.get("port") or 0)
    pid = listening_pid(port) if port else 0
    if not pid or not pid_alive(pid):
        return None
    if not re.search(r"node|dsh|harness", process_image(pid), re.I):
        return None
    if not probe_dsh_like(port):
        return None
    started = process_start_time(pid) or time.strftime("%Y-%m-%d %H:%M:%S")
    write_run(
        cfg,
        lane,
        {
            "lane": lane,
            "version": lane_data.get("version", "?"),
            "port": port,
            "pid": pid,
            "url": f"http://127.0.0.1:{port}",
            "adopted": True,
            "procStartedAt": started,
            "startedAt": started,
            "note": "自动重新接管了该端口上的实例（不是本启动器启动的）",
        },
    )
    return read_run(cfg, lane)


def lane_states(cfg: dict, ttl: float = 0.5) -> dict[str, dict | None]:
    """一次算出**所有** lane 的运行态，复用同一份 TCP 表。

    GUI 一轮刷新里要问好几次"这条 lane 在跑吗"（算签名、建卡片、更新计数）。
    每次都重新 reconcile 的话，同一份事实要算三遍；这里一次算完交给调用方复用。
    """
    tcp_listeners(ttl=ttl)  # 预热：后面所有 listening_pid 直接命中缓存
    out: dict[str, dict | None] = {}
    for name in cfg.get("lanes", {}):
        if isinstance(cfg.get("lanes", {}).get(name), dict):
            out[name] = reconcile_lane(cfg, name)
    return out


# ======================= 版本查询（纯脚本 HTTP） =======================


def fetch_index(cfg: dict, name: str = PKG) -> tuple[dict, str]:
    """查询 registry，返回 (packument, 实际使用的 registry)。

    官方 registry 在本机会握手超时（schannel/网络原因），所以首个候选只等 8 秒就换下一个。
    `name` 默认是 dsh 本体；插件市场更新时用它查 `dshmarket`。
    """
    regs = [cfg["registry"]] if cfg.get("registry") else list(REGISTRY_CANDIDATES)
    quoted = urllib.parse.quote(name, safe="")
    last: Exception | None = None
    for index, reg in enumerate(regs):
        url = f"{reg.rstrip('/')}/{quoted}"
        timeout = 8 if index == 0 else 25
        try:
            # 用完整 packument（而非 abbreviated）以拿到 time 字段里的发布日期
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "dsh-lanes-launcher",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp), reg
        except Exception as exc:  # noqa: BLE001
            last = exc
            warn(f"registry 不可用 {reg}：{exc}")
    raise RuntimeError(f"所有 registry 都查询失败：{last}")


def tail_lines(path: Path, count: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    except OSError:
        return []


def wait_authenticated(url: str, timeout: float = 30.0, interval: float = 0.6) -> tuple[bool, str]:
    """按浏览器的方式验证服务可用：带 cookie jar 走一次 token → cookie → 200 握手。

    dsh-client-connection 的规则是：根路径带正确 token 的 GET 会下发 cookie 并重定向到 ./，
    之后靠 cookie 放行，否则 401。所以不带 cookie jar 的裸请求必然 401——
    这个探针同时验证了三件事：进程活着、token 是**本次**的、静态资源可服务。
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    deadline = time.time() + timeout
    last = "未知"
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=8) as resp:
                if resp.status == 200 and len(jar) > 0:
                    return True, f"HTTP {resp.status} + cookie"
                last = f"HTTP {resp.status}（未下发 cookie）"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(interval)
    return False, last


def version_key(v: str):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$", v)
    if not m:
        return ((0, 0, 0), 1, ())
    base = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    pre = m.group(4)
    if pre is None:
        return (base, 1, ())  # 正式版 > 预发布版
    parts = []
    for seg in pre.split("."):
        parts.append((1, int(seg), "") if seg.isdigit() else (0, 0, seg))
    return (base, 0, tuple(parts))


def sorted_versions(packument: dict) -> list[str]:
    return sorted(packument.get("versions", {}).keys(), key=version_key)


def resolve_tag(packument: dict, spec: str) -> str:
    tags = packument.get("dist-tags", {})
    if spec in tags:
        return tags[spec]
    if spec in packument.get("versions", {}):
        return spec
    # 允许 "0.1.7" 这类前缀写法
    matches = [v for v in packument.get("versions", {}) if v.startswith(spec)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        return sorted(matches, key=version_key)[-1]
    raise KeyError(spec)


# ======================= 子进程：无管道执行（写日志 + 可选跟随） =======================


def _new_group_flags() -> int:
    return subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0


def follow_file(path: Path, stop: threading.Event, on_line=None, start_offset: int = 0) -> threading.Thread:
    """跟随一个正在被写入的日志文件，边打印边交给 on_line。

    start_offset 很关键：日志是追加写的，同一个 lane 反复启动会让旧进程的
    `dsh web: ...?token=` 行还留在文件里。从 0 读就会抓到**上一次的旧 token**
    （token 是每进程的），表现为浏览器 401。所以必须从"本次启动前"的偏移开始读。
    """

    def worker() -> None:
        while not path.exists() and not stop.is_set():
            time.sleep(0.05)
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            return
        with fh:
            try:
                fh.seek(start_offset)
            except OSError:
                pass
            while True:
                line = fh.readline()
                if line:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    if on_line:
                        on_line(line)
                    continue
                if stop.is_set():
                    rest = fh.read()
                    if rest:
                        sys.stdout.write(rest)
                        sys.stdout.flush()
                        if on_line:
                            on_line(rest)
                    return
                time.sleep(0.15)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def run_logged(cmd: list[str], cwd: Path, env: dict, log_path: Path, follow: bool = True) -> int:
    """执行命令：stdout/stderr 直接落到日志文件（不用管道），可选实时跟随打印。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    thread = None
    with open(log_path, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(cmd)} (cwd={cwd})\n")
        logf.flush()
        offset = logf.tell()
        if follow:
            thread = follow_file(log_path, stop, start_offset=offset)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=_new_group_flags(),
        )
        code = proc.wait()
        if thread is not None:
            stop.set()
            thread.join(timeout=3)
    return code


# ======================= npm 安装 =======================


def build_npm_env(cfg: dict, node: str) -> dict:
    env = os.environ.copy()
    # 关键：项目式安装里 npm 会直接拒绝来自 CLI/env 的 allow-scripts
    # （EALLOWSCRIPTS），必须删掉环境变量，改由目标 package.json 的 allowScripts 承担。
    for key in [k for k in env if k.lower().startswith("npm_config_allow_scripts")]:
        env.pop(key, None)
    node_dir = str(Path(node).parent)
    path = env.get("PATH", "")
    if node_dir and node_dir not in path:
        env["PATH"] = node_dir + os.pathsep + path
    return env


def write_lane_package_json(vdir: Path, version: str, allow_scripts: dict) -> None:
    manifest_path = vdir / "package.json"
    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    manifest.setdefault("name", f"dsh-lane-{version}")
    manifest["private"] = True
    manifest.setdefault("version", "0.0.0")
    manifest["description"] = f"DSH lane install tree for @deepseek-ai/dsh@{version}"
    manifest["allowScripts"] = dict(allow_scripts)
    vdir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def installed_version(vdir: Path) -> str | None:
    manifest_path = vdir / "node_modules" / "@deepseek-ai" / "dsh" / "package.json"
    if not manifest_path.is_file():
        return None
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8")).get("version")
    except Exception:
        return None


def bin_js(vdir: Path) -> Path:
    return vdir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"


def install_version(cfg: dict, version: str, follow: bool = True) -> Path:
    """把指定版本装进 <root>/versions/<version>；已装且版本一致则直接复用。"""
    p = ensure_layout(cfg)
    vdir = p["versions"] / version
    existing = installed_version(vdir)
    if existing == version:
        ok(f"版本 {version} 已安装，直接复用：{vdir}")
        return vdir

    node = find_node(cfg)
    npm_cli = find_npm_cli(cfg, node)
    if not node or not npm_cli:
        raise RuntimeError("找不到 node 或 npm-cli.js，请检查 Node.js 安装（可用 doctor 子命令）")

    write_lane_package_json(vdir, version, cfg["allow_scripts"])
    env = build_npm_env(cfg, node)
    log = p["logs"] / f"install-{version}.log"
    cmd = [
        node,
        npm_cli,
        "install",
        "--prefix",
        str(vdir),
        f"{PKG}@{version}",
        "--no-audit",
        "--no-fund",
        "--loglevel=notice",
    ]
    info(col(f"正在安装 {PKG}@{version} → {vdir}", C.BOLD))
    info(col(f"（日志：{log}）", C.GRAY))
    code = run_logged(cmd, cwd=vdir, env=env, log_path=log, follow=follow)
    got = installed_version(vdir)
    if code != 0 or got != version:
        raise RuntimeError(
            f"安装失败（npm 退出码 {code}，实际装到 {got!r}）。完整日志：{log}"
        )
    if not bin_js(vdir).is_file():
        raise RuntimeError(f"安装完成但入口缺失：{bin_js(vdir)}")
    ok(f"已安装 {PKG}@{got}")
    return vdir


# ======================= lane 管理 =======================


def pick_port(cfg: dict, exclude: set[int] | None = None) -> int:
    """挑一个空闲端口：读一次 TCP 表，再在 port_range 里找第一个既没登记也没人监听的。"""
    lo, hi = cfg.get("port_range", [3080, 3129])
    used = {int(v.get("port", 0)) for v in cfg["lanes"].values() if isinstance(v, dict)}
    listening = set(tcp_listeners())
    for port in range(int(lo), int(hi) + 1):
        if exclude and port in exclude:
            continue
        if port in used or port in listening:
            continue
        return port
    raise RuntimeError(f"{lo}-{hi} 之间没有空闲端口")


def find_lane_by_version(cfg: dict, version: str) -> str | None:
    for name, lane in cfg["lanes"].items():
        if isinstance(lane, dict) and lane.get("version") == version:
            return name
    return None


# ======================= 主要版本 & 凭据继承 =======================
#
# 「主要版本」= 你日常真正在用的那条 lane（例如接管来的 global）。
# 之所以需要这个概念：新装一条 lane 时会得到一份全新的空 DSH_HOME，
# 里面没有 API key，第一次启动要你手动填一遍。而这个 key 本来就是同一把，
# 所以在**创建时**从主要版本复制一次即可，之后各 lane 不再联动（要换自己改）。

CRED_FILENAME = ".credentials.yaml"


def primary_lane(cfg: dict) -> str | None:
    """当前标记的主要版本 lane 名（未设置或已失效时返回 None）。"""
    name = str(cfg.get("primary") or "").strip()
    if name and isinstance(cfg.get("lanes", {}).get(name), dict):
        return name
    return None


def ordered_lanes(cfg: dict) -> list[tuple[str, dict]]:
    """主要版本永远排第一，其余保持登记顺序（sorted 是稳定排序）。"""
    lanes = [(n, d) for n, d in cfg.get("lanes", {}).items() if isinstance(d, dict)]
    top = primary_lane(cfg)
    if not top:
        return lanes
    return sorted(lanes, key=lambda kv: 0 if kv[0] == top else 1)


def set_primary(cfg: dict, lane: str | None) -> None:
    cfg["primary"] = lane or ""
    save_config(cfg)


def credentials_path(home) -> Path:
    return Path(home) / CRED_FILENAME


def _refs_block(lines: list[str]) -> tuple[dict[str, str], int, int]:
    """在 .credentials.yaml 的行里定位 `refs:` 段。

    只做定点文本解析：不引入 yaml 依赖，也不会动 `records:` 等其它段。
    返回 (refs, 段起始行号, 段结束行号)，找不到时 (-1, -1)。
    """
    start = -1
    for i, line in enumerate(lines):
        if line.rstrip() == "refs:":
            start = i
            break
    if start < 0:
        return {}, -1, -1
    refs: dict[str, str] = {}
    end = len(lines)
    for j in range(start + 1, len(lines)):
        line = lines[j]
        if not line.strip() or not line[:1].isspace():
            end = j  # 空行或顶格的新段（records: …）都表示 refs 结束
            break
        m = re.match(r"^\s+([^\s:#][^:]*):\s*(.*)$", line)
        if m:
            refs[m.group(1).strip()] = m.group(2).strip()
    return refs, start, end


def read_credential_refs(home) -> dict[str, str]:
    """读出某个 DSH_HOME 里已存的密钥引用（refs 段，如 DEEPSEEK_API_KEY）。"""
    path = credentials_path(home)
    if not path.is_file():
        return {}
    try:
        return _refs_block(path.read_text(encoding="utf-8").splitlines())[0]
    except Exception:
        return {}


def merge_credential_refs(path: Path, refs: dict[str, str]) -> list[str]:
    """把 refs 合并进凭据文件：缺则插入、有则更新，其它内容原样保留。

    返回**实际被改动**的键名列表（已经是同样值的不算）。
    """
    lines: list[str] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
    # `version: 1` 这一行是必需的：0.1.7 的 credentials-local 会把没有它的文件判成
    # "pre-release flat layout" 并**直接拒绝加载**，然后一串等 `credentials` 服务的插件
    # 全都起不来（新建的 lane 会表现为"启动超时、没抓到 URL"）。
    # 实测：create 出来的 plgtest 就是这个死法，日志里写着
    # `uses the pre-release flat layout. Add version: 1 and nest the existing 1 entry under refs:`。
    # 必须**在读 refs 段之前**补这一行，否则行号会错位（第一版就踩了：补完再按旧行号
    # 替换，把刚插的 version 行换掉了，文件里出现两个 refs: → DUPLICATE_KEY）。
    if not any(line.strip().startswith("version:") for line in lines):
        lines.insert(0, "version: 1")
    existing, start, end = _refs_block(lines)
    merged = dict(existing)
    touched: list[str] = []
    for key, val in refs.items():
        if merged.get(key) != val:
            touched.append(key)
        merged[key] = val
    block = ["refs:"] + [f"  {k}: {v}" for k, v in merged.items()]
    if start < 0:
        # 没有 refs 段：插在 version: 行之后（DSH 写的文件都是这个顺序），否则放最前
        idx = next(
            (i + 1 for i, ln in enumerate(lines) if ln.strip().startswith("version:")), 0
        )
        lines[idx:idx] = block
    else:
        lines[start:end] = block
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        try:  # 改的是已有文件（含它自己的 browser-session 记录）→ 先留一份
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except Exception:
            pass
    path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    return touched


def inherit_credentials(cfg: dict, lane: str) -> dict:
    """新建 lane 时从主要版本复制一次 API key（只复制 refs，不复制会话/设置/登录态）。

    返回一份可打印的结果说明，供 create / adopt / GUI 复用。
    """
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    out = {
        "lane": lane,
        "source": None,
        "home": str(home),
        "path": str(credentials_path(home)),
        "available": [],
        "written": [],
        "error": None,
        "skipped": None,
    }

    src = primary_lane(cfg)
    if not src:
        out["skipped"] = "还没有设置主要版本"
        return out
    if src == lane:
        out["skipped"] = "它自己就是主要版本"
        return out
    src_home = Path(cfg["lanes"][src].get("home") or (paths(cfg)["homes"] / src))
    out["source"] = src
    try:
        if src_home.resolve() == home.resolve():
            out["skipped"] = f"与主要版本「{src}」用的是同一个 DSH_HOME，无需复制"
            return out
    except Exception:
        pass

    refs = read_credential_refs(src_home)
    out["available"] = sorted(refs)
    if not refs:
        out["error"] = (
            f"主要版本「{src}」的 {credentials_path(src_home)} 里没有 refs 段，"
            f"没有 API key 可复制"
        )
        return out
    try:
        out["written"] = merge_credential_refs(credentials_path(home), refs)
    except Exception as exc:  # noqa: BLE001
        # 单条写失败（权限、文件被占）不应该打断整批同步
        out["error"] = f"写入失败：{type(exc).__name__}: {exc}"
    return out


def print_inherit_result(res: dict) -> None:
    """把 inherit_credentials 的结果打成一行控制台说明。"""
    if res.get("skipped"):
        info(f"      API key   : 未复制 —— {res['skipped']}")
    elif res.get("error"):
        warn(f"      API key   : 未复制 —— {res['error']}")
    else:
        keys = "、".join(res["available"]) or "(无)"
        extra = "" if res["written"] else "（值已一致，无需改动）"
        ok(f"      API key   : 已从主要版本「{res['source']}」复制 {len(res['available'])} 项：{keys}{extra}")
        info(col(f"                  → {res['path']}", C.GRAY))


def resolve_target(cfg: dict, target: str) -> str:
    """把用户给的 lane 名或版本号统一成 lane 名；必要时自动登记一条 lane。"""
    if target in cfg["lanes"]:
        return target
    by_ver = find_lane_by_version(cfg, target)
    if by_ver:
        return by_ver
    # 是已安装的版本？自动登记
    vdir = paths(cfg)["versions"] / target
    if installed_version(vdir) == target:
        name = target
        cfg["lanes"][name] = {
            "version": target,
            "port": pick_port(cfg),
            "home": str(paths(cfg)["homes"] / name),
            "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "由 open 自动登记",
        }
        save_config(cfg)
        info(col(f"已自动登记 lane「{name}」（版本 {target}）", C.CYAN))
        return name
    raise KeyError(target)


def lane_install_dir(cfg: dict, lane: str) -> Path:
    """这条 lane 用哪棵安装树。

    - 常规 lane：`<root>/versions/<版本>`（npm --prefix 前缀目录，本启动器装的）
    - 接管的 lane（`installDir` 字段）：**dsh 包目录本身**（如
      `%APPDATA%\\npm\\node_modules\\@deepseek-ai\\dsh`），也就是 find_installs 报出来的那个路径。
      两种写法由 lane_entry_script() 统一解析。
    """
    lane_data = cfg["lanes"][lane]
    if lane_data.get("installDir"):
        return Path(lane_data["installDir"])
    return paths(cfg)["versions"] / str(lane_data["version"])


def lane_entry_script(cfg: dict, lane: str) -> Path:
    """兼容两种安装树写法，返回 `lib/bin.js` 的实际路径。"""
    base = lane_install_dir(cfg, lane)
    direct = base / "lib" / "bin.js"          # 接管型：base 就是 dsh 包目录
    if direct.is_file():
        return direct
    return bin_js(base)                        # 常规型：base 是 --prefix 前缀目录


def require_version_installed(cfg: dict, lane: str) -> tuple[str, Path, Path]:
    lane_data = cfg["lanes"][lane]
    version = str(lane_data["version"])
    vdir = lane_install_dir(cfg, lane)
    entry = lane_entry_script(cfg, lane)
    if not entry.is_file():
        if lane_data.get("installDir"):
            raise RuntimeError(
                f"lane「{lane}」指向的安装树里没有入口：{entry}\n"
                f"    （installDir = {vdir}）"
            )
        raise RuntimeError(
            f"lane「{lane}」的版本 {version} 尚未安装。先执行：\n"
            f"    py {ME_NAME} create {lane} {version}"
        )
    if not lane_data.get("installDir") and installed_version(vdir) != version:
        raise RuntimeError(
            f"lane「{lane}」的安装树版本与登记不符（登记 {version}，实际 {installed_version(vdir)}）"
        )
    home = Path(lane_data.get("home") or (paths(cfg)["homes"] / lane))
    return version, vdir, home


# ======================= 子命令 =======================


def cmd_doctor(cfg: dict, args) -> int:
    info(col("── 环境自检 ──", C.BOLD))
    node = find_node(cfg)
    info(f"  node        : {node or '未找到'}")
    if node:
        try:
            out = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=20).stdout.strip()
            ok(f"node 可执行（{out}）")
        except Exception as exc:
            fail(f"node 无法执行：{exc}")
    npm_cli = find_npm_cli(cfg, node)
    info(f"  npm-cli.js  : {npm_cli or '未找到'}")
    if node and npm_cli:
        try:
            out = subprocess.run(
                [node, npm_cli, "-v"], capture_output=True, text=True, timeout=30
            ).stdout.strip()
            ok(f"npm 可执行（{out}）")
        except Exception as exc:
            fail(f"npm 无法执行：{exc}")
    info(f"  python      : {sys.version.split()[0]}")
    p = paths(cfg)
    info(f"  lane root   : {p['root']}")
    try:
        ensure_layout(cfg)
        probe = p["root"] / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        ok("lane root 可写")
    except Exception as exc:
        fail(f"lane root 不可写：{exc}")
    info(f"  默认工作区  : {cfg.get('default_cwd')}")
    info(f"  端口范围    : {cfg['port_range'][0]}-{cfg['port_range'][1]}")
    try:
        packument, reg = fetch_index(cfg)
        tags = ", ".join(f"{k}={v}" for k, v in sorted(packument.get("dist-tags", {}).items()))
        ok(f"registry 可达（{reg}）：{len(packument.get('versions', {}))} 个版本 / {tags}")
    except Exception as exc:
        fail(f"registry 不可达：{exc}")
    if CONFIG_PATH.exists():
        ok(f"配置文件：{CONFIG_PATH}")
    else:
        info(col(f"  配置文件    : 未创建（首次 create 时自动生成 {CONFIG_PATH}）", C.GRAY))
    return 0


def cmd_versions(cfg: dict, args) -> int:
    packument, reg = fetch_index(cfg)
    tags = packument.get("dist-tags", {})
    versions = sorted_versions(packument)
    times = packument.get("time", {})
    info(col(f"── npm 上的 {PKG}（来源 {reg}）──", C.BOLD))
    info(col("  dist-tags：", C.CYAN) + ", ".join(f"{k}={v}" for k, v in sorted(tags.items())))
    info("")
    tag_of = {}
    for tag, ver in tags.items():
        tag_of.setdefault(ver, []).append(tag)
    limit = len(versions) if args.all else min(15, len(versions))
    shown = versions[-limit:]
    info(f"  {'版本':<16}{'标签':<12}{'发布日期':<14}本地")
    info("  " + "─" * 54)
    for v in reversed(shown):
        installed = installed_version(paths(cfg)["versions"] / v) == v
        date = (times.get(v) or "")[:10]
        info(f"  {v:<16}{','.join(tag_of.get(v, [])) or '-':<12}{date:<14}"
             + (col("已装", C.GREEN) if installed else col("-", C.GRAY)))
    if not args.all and len(versions) > limit:
        info(col(f"  … 共 {len(versions)} 个版本，加 --all 看全部", C.GRAY))
    return 0


def cmd_create(cfg: dict, args) -> int:
    p = ensure_layout(cfg)
    lane = args.lane
    try:
        packument, _ = fetch_index(cfg)
        version = resolve_tag(packument, args.version)
    except Exception as exc:
        fail(f"无法解析版本 {args.version!r}：{exc}")
        return 1
    if version != args.version:
        info(col(f"  {args.version} → {version}", C.GRAY))
    if lane in cfg["lanes"] and not args.force:
        old = cfg["lanes"][lane]
        if old.get("version") != version:
            fail(
                f"lane「{lane}」已存在，版本 {old.get('version')}。"
                f"要改写请加 --force，或换个 lane 名。"
            )
            return 1
    vdir = install_version(cfg, version, follow=args.follow)
    home = paths(cfg)["homes"] / lane
    home.mkdir(parents=True, exist_ok=True)
    port = args.port or (cfg["lanes"].get(lane, {}) or {}).get("port") or pick_port(cfg)
    cfg["lanes"][lane] = {
        "version": version,
        "port": int(port),
        "home": str(home),
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_config(cfg)

    # 只在创建这一次复制 API key（之后各 lane 互不影响，要改自己改）
    inherit = inherit_credentials(cfg, lane)

    info("")
    ok(f"lane「{lane}」就绪")
    info(f"      版本      : {version}")
    info(f"      安装目录  : {vdir}")
    info(f"      DSH_HOME : {home}")
    info(f"      端口      : {port}")
    print_inherit_result(inherit)
    if not primary_lane(cfg):
        info("")
        warn("还没有设置主要版本。设一个之后，以后新建的版本会自动继承它的 API key：")
        info(col(f"      py {ME_NAME} primary {lane}", C.CYAN))
    info("")
    info(col(f"  下一步： py {ME_NAME} open {lane}", C.CYAN))
    return 0


URL_RE = re.compile(r"dsh web:\s*(\S+)")


def cmd_open(cfg: dict, args) -> int:
    try:
        lane = resolve_target(cfg, args.target)
    except KeyError:
        fail(f"找不到 lane 或已安装版本「{args.target}」。先跑 versions / create。")
        return 1
    try:
        version, vdir, home = require_version_installed(cfg, lane)
    except RuntimeError as exc:
        fail(str(exc))
        return 1

    running = lane_runtime(cfg, lane)
    if running:
        url = running.get("url") or f"http://127.0.0.1:{running.get('port')}"
        warn(f"lane「{lane}」已在运行（pid {running.get('pid')}，端口 {running.get('port')}）")
        info(col(f"  {url}", C.CYAN))
        if cfg.get("open_browser", True) and not args.no_browser:
            webbrowser.open(url)
        return 0

    node = find_node(cfg)
    if not node:
        fail("找不到 node")
        return 1
    port = args.port or int(cfg["lanes"][lane].get("port") or 0) or pick_port(cfg)
    holder = listening_pid(port)
    if holder:
        # 端口上已经有东西。如果那是个 DSH 实例（例如你用老方式启动的那套），
        # 就"接管"它：不重复起第二个进程，把它的 PID 记进运行态，之后就能从这里停掉。
        pid = holder
        if pid and pid_alive(pid) and probe_dsh_like(port):
            started = process_start_time(pid) or time.strftime("%Y-%m-%d %H:%M:%S")
            url = f"http://127.0.0.1:{port}"
            write_run(
                cfg,
                lane,
                {
                    "lane": lane,
                    "version": version,
                    "port": port,
                    "pid": pid,
                    "url": url,
                    "adopted": True,
                    "installedAt": cfg["lanes"][lane].get("installDir", ""),
                    "procStartedAt": started,
                    "startedAt": started,
                    "note": "接管了原本就在这个端口上运行的实例（不是本启动器启动的）",
                },
            )
            info("")
            warn(f"端口 {port} 上已经有一个 DSH 实例在跑（pid {pid}，启动于 {started}）")
            ok("已接管它，不会再起第二个进程；运行态与停止按钮都归本启动器管了")
            info(col(f"  {url}", C.BOLD + C.CYAN))
            if args.adopt_only:
                return 0
            if cfg.get("open_browser", True) and not args.no_browser:
                # 这个实例的 token 只存在于它自己的 stdout 里，我们拿不到，
                # 只能用裸地址——浏览器里若已有它的登录 cookie 就能直接进。
                webbrowser.open(url)
            return 0
        fail(f"端口 {port} 已被占用（不是 DSH 实例）。换一个：--port 3083，或先停掉占用者。")
        return 1
    entry = lane_entry_script(cfg, lane)
    if not entry.is_file():
        fail(f"入口缺失：{entry}（建议重新 create，或用 adopt 指向正确的安装树）")
        return 1

    env = os.environ.copy()
    env["DSH_HOME"] = str(home)
    env.pop("DSH_WEB_URL", None)  # 避免继承别的实例的地址
    cwd = Path(args.cwd) if args.cwd else Path(cfg.get("default_cwd") or Path.home())
    if not cwd.is_dir():
        warn(f"工作区 {cwd} 不存在，改用 {Path.home()}")
        cwd = Path.home()

    p = ensure_layout(cfg)
    log = p["logs"] / f"{lane}.log"
    cmd = [node, str(entry), "web", "--port", str(port), "--no-open"]

    info(col(f"── 启动 lane「{lane}」──", C.BOLD))
    info(f"      版本      : {version}")
    info(f"      DSH_HOME : {home}")
    info(f"      工作区    : {cwd}")
    info(f"      端口      : {port}")
    info(f"      日志      : {log}")
    info("")

    url_box: dict[str, str] = {}
    pattern = URL_RE

    def on_line(line: str) -> None:
        m = pattern.search(line)
        if m and "url" not in url_box:
            url_box["url"] = m.group(1).strip()

    stop = threading.Event()
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", errors="replace") as logf:
        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} $ {' '.join(cmd)} (cwd={cwd})\n")
        logf.flush()
        offset = logf.tell()  # 只认本次启动之后写的行（旧 token 不可用）
        thread = follow_file(log, stop, on_line, start_offset=offset)
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=subprocess.STDOUT,
            creationflags=_new_group_flags(),
        )
        write_run(
            cfg,
            lane,
            {
                "lane": lane,
                "version": version,
                "port": port,
                "pid": proc.pid,
                "url": "",
                "cwd": str(cwd),
                "installDir": str(vdir),
                "procStartedAt": process_start_time(proc.pid),
                "startedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

        # 就绪判据 = 从日志抓到 `dsh web: <带 token 的 URL>`。
        # 不能只看端口通：webserver 绑定早于 URL 打印，用裸地址访问会 401。
        deadline = time.time() + int(args.timeout or cfg.get("startup_timeout", 240))
        while time.time() < deadline and proc.poll() is None and "url" not in url_box:
            time.sleep(0.3)

        url = url_box.get("url")
        if url:
            # 按浏览器的方式确认服务真的可用：token → cookie → 200
            remaining = max(10.0, deadline - time.time())
            info(col("  正在验证登录握手（token → cookie）…", C.GRAY))
            good, detail = wait_authenticated(url, timeout=min(45.0, remaining))
            if not good:
                fail(f"服务起来了但认证握手失败（{detail}）：URL 里的 token 该进程不认。")
                fail(f"日志尾部（完整日志：{log}）：")
                for line in tail_lines(log, 15):
                    info(col("    " + line.rstrip(), C.GRAY))
                stop.set()
                thread.join(timeout=2)
                if proc.poll() is None:
                    kill_tree(proc.pid)
                clear_run(cfg, lane)
                return 1
            data = read_run(cfg, lane) or {}
            data["url"] = url
            write_run(cfg, lane, data)
            info("")
            ok(f"lane「{lane}」已启动并验证通过（{detail}）")
            info(col(f"  {url}", C.BOLD + C.CYAN))
            if cfg.get("open_browser", True) and not args.no_browser:
                webbrowser.open(url)
        else:
            code = proc.poll()
            fail(f"启动超时或进程已退出（退出码 {code}）：没抓到 dsh 打印的 URL 行")
            fail(f"日志尾部（完整日志：{log}）：")
            for line in tail_lines(log, 15):
                info(col("    " + line.rstrip(), C.GRAY))
            stop.set()
            thread.join(timeout=2)
            if proc.poll() is None:
                kill_tree(proc.pid)
            clear_run(cfg, lane)
            return 1

        if args.detach:
            info(col("  已转入后台（--detach）。停止： "
                    f"py {ME_NAME} stop {lane}", C.GRAY))
            stop.set()
            thread.join(timeout=2)
            return 0

        info(col("  按 Ctrl+C 停止该 lane。", C.GRAY))
        try:
            while proc.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            info("")
            warn("收到停止指令 (Ctrl+C)")
        finally:
            stop.set()
            thread.join(timeout=2)
            if proc.poll() is None:
                kill_tree(proc.pid)
            clear_run(cfg, lane)
            ok("lane 已停止")
    return 0


def cmd_instances(cfg: dict, args) -> int:
    """列出本机上所有 DSH 实例，含不是本启动器启动的那些（例如全局安装的 3080）。"""
    info(col("── 未登记的实例（不是本启动器启动的）──", C.BOLD))
    found = discover_instances(cfg)
    if not found:
        info(col("  （没有发现。你在用的 3080 若确实在跑，说明进程枚举被限制了。）", C.GRAY))
    else:
        info(f"  {'端口':<7}{'PID':<9}{'进程':<16}{'启动时间':<21}特征")
        info("  " + "─" * 78)
        for item in found:
            feature = "像是 DSH 实例" if item["dsh_like"] else "端口被占用（不一定是 DSH）"
            info(
                f"  {item['port']:<7}{item['pid']:<9}{item['exe']:<16}"
                f"{item.get('started') or '?':<21}{feature}"
            )
            info(col(f"          {item['url']}", C.CYAN))

    info("")
    info(col("── 本机各套安装（到底是哪一份在跑）──", C.BOLD))
    installs = find_installs(cfg)
    if not installs:
        info(col("  （没找到任何 @deepseek-ai/dsh 安装树）", C.GRAY))
    else:
        info(f"  {'来源':<16}{'版本':<16}{'写入时间':<18}路径")
        info("  " + "─" * 96)
        for item in installs:
            info(
                f"  {item['source']:<16}{item['version']:<16}"
                f"{item.get('installedAt') or '?':<18}{item['path']}"
            )
    return 0


def cmd_adopt(cfg: dict, args) -> int:
    """把本机已有的一份安装（默认 npm 全局那份）注册成 lane，并沿用它的 DSH_HOME。

    这样就能用同一个启动器"打开 / 停止"你平时在用的那套，而不必再装一份。
    """
    lane = args.lane
    ensure_layout(cfg)
    if lane in cfg.get("lanes", {}) and not args.force:
        fail(f"lane「{lane}」已存在。要覆盖请加 --force。")
        return 1

    installs = find_installs(cfg)
    target = None
    if args.install:
        want = str(Path(args.install).resolve()).lower()
        target = next((i for i in installs if str(Path(i["path"]).resolve()).lower() == want), None)
        if target is None:
            manifest = Path(args.install) / "package.json"
            data = {}
            if manifest.is_file():
                try:
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            if data.get("name") == PKG:
                target = {
                    "version": data.get("version", "?"),
                    "path": str(Path(args.install)),
                    "source": "指定路径",
                    "installedAt": "",
                }
        if target is None:
            fail(f"{args.install} 不是有效的 {PKG} 安装树（目录下要有 package.json）")
            return 1
    else:
        target = next((i for i in installs if i["source"] == "npm 全局"), None)
        if target is None:
            fail("找不到 npm 全局安装。用 --install <路径> 指定，或先跑 instances 看有哪些安装树")
            return 1

    home = (
        Path(args.home).expanduser()
        if args.home
        else Path(os.environ.get("USERPROFILE") or Path.home()) / ".dsh"
    )
    port = int(args.port) if args.port else 0
    if not port:
        port = 3080
        if port_in_use(port) and not probe_dsh_like(port):
            port = pick_port(cfg)
    cfg["lanes"][lane] = {
        "version": target["version"],
        "port": port,
        "home": str(home),
        "installDir": target["path"],
        "kind": "adopted",
        "source": target["source"],
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_config(cfg)
    inherit = inherit_credentials(cfg, lane)
    info("")
    ok(f"已把「{target['source']}」注册为 lane「{lane}」")
    info(f"      版本      : {target['version']}")
    info(f"      安装目录  : {target['path']}")
    info(f"      DSH_HOME : {home}")
    info(f"      端口      : {port}")
    print_inherit_result(inherit)
    warn("这条 lane 用的是你现有的 DSH_HOME（含已有会话 / 设置 / 插件），不是隔离 home。")
    info("")
    info(
        col(
            f"  下一步： py {ME_NAME} open {lane}"
            "   （若该端口已有实例在跑，会自动接管它而不会再起一个）",
            C.CYAN,
        )
    )
    return 0


def cmd_list(cfg: dict, args) -> int:
    lanes = cfg.get("lanes", {})
    info(col("── lanes ──", C.BOLD))
    if not lanes:
        info(col("  （还没有 lane。用 create 建一条，例如：create next 0.1.7-rc.2）", C.GRAY))
        return 0
    top = primary_lane(cfg)
    info(f"  {'':<3}{'lane':<12}{'版本':<16}{'端口':<7}{'状态':<10}DSH_HOME")
    info("  " + "─" * 78)
    for name, lane in ordered_lanes(cfg):
        ver = lane.get("version", "?")
        port = lane.get("port", "-")
        rt = reconcile_lane(cfg, name)  # 顺带自愈：端口上有实例就自动接管
        plain = f"运行中 {rt['pid']}" if rt else "已停止"
        state = col(f"{plain:<14}", C.GREEN if rt else C.GRAY)
        mark = col("★", C.CYAN) if name == top else " "
        info(f"  {mark:<3}{name:<12}{ver:<16}{str(port):<7}{state}{lane.get('home', '')}")
        if rt and rt.get("url"):
            info(col(f"                  {rt['url']}", C.CYAN))
    if top:
        info("")
        info(col(f"  ★ = 主要版本（{top}）：新建的 lane 会自动继承它的 API key。", C.GRAY))
    else:
        info("")
        warn("还没有主要版本。用 primary <lane> 指定，之后新建 lane 会自动继承它的 API key。")
    return 0


def cmd_stop(cfg: dict, args) -> int:
    targets = args.target or list(cfg.get("lanes", {}).keys())
    if isinstance(targets, str):
        targets = [targets]
    any_stopped = False
    for lane in targets:
        lane_data = cfg.get("lanes", {}).get(lane) or {}
        rt = read_run(cfg, lane)
        pid = int(rt.get("pid", 0)) if rt else 0
        # 没有运行记录时，按这条 lane 的端口把它找回来
        # （典型场景：用老方式启动的实例，或运行态文件被清过）
        if not (pid and pid_alive(pid)):
            port = int(lane_data.get("port") or 0)
            found = tcp_listeners().get(port, 0) if port else 0
            if found and pid_alive(found):
                if probe_dsh_like(port) or re.search(
                    r"node|dsh|harness", process_image(found), re.I
                ):
                    info(f"lane「{lane}」没有运行记录，但端口 {port} 上有进程 {found}，按端口停止它")
                    pid = found
                else:
                    warn(f"端口 {port} 被一个不像 DSH 的进程占用（pid {found}），不擅自结束它")
        if pid and pid_alive(pid):
            started = process_start_time(pid)
            info(f"正在停止 lane「{lane}」(pid {pid}{'，启动于 ' + started if started else ''}) …")
            kill_tree(pid)
            time.sleep(0.6)
            if pid_alive(pid):
                warn(f"lane「{lane}」进程 {pid} 仍存活，请手动检查")
            else:
                ok(f"lane「{lane}」已停止")
            any_stopped = True
        else:
            warn(f"lane「{lane}」没有正在运行的进程")
        clear_run(cfg, lane)
    return 0 if any_stopped or not targets else 1


def cmd_remove(cfg: dict, args) -> int:
    """从登记表里移除一条 lane（不动安装树与 DSH_HOME）。"""
    lane = args.lane
    data = cfg.get("lanes", {}).get(lane)
    if not isinstance(data, dict):
        fail(f"没有 lane「{lane}」")
        return 1
    rt = lane_runtime(cfg, lane)
    if rt and not args.force:
        fail(f"lane「{lane}」正在运行（pid {rt.get('pid')}）。先 stop 它，或加 --force 只注销登记。")
        return 1
    del cfg["lanes"][lane]
    save_config(cfg)
    clear_run(cfg, lane)
    ok(f"已从登记表移除 lane「{lane}」")
    if data.get("installDir"):
        info("（接管型：只删了登记，它的安装树与 DSH_HOME 原样保留，随时可用 adopt 再加回来）")
    else:
        info(f"（安装树还在：{paths(cfg)['versions'] / str(data.get('version'))}；要省空间自行删目录）")
    return 0


def cmd_primary(cfg: dict, args) -> int:
    """标记 / 查看「主要版本」——你日常用的那一套。

    主要版本有两个作用：① 新建 lane 时自动把它的 API key 复制给新 lane；
    ② 删除它需要二次确认（GUI 里要手打 lane 名）。
    """
    lane = getattr(args, "lane", None)
    if getattr(args, "clear", False):
        cur = primary_lane(cfg)
        if not cur:
            warn("当前本来就没有主要版本标记。")
            return 0
        set_primary(cfg, None)
        ok(f"已清除主要版本标记（原为「{cur}」）")
        warn("清除后新建的 lane 不会自动继承 API key。")
        return 0

    if not lane:
        cur = primary_lane(cfg)
        if not cur:
            warn("还没有设置主要版本。用法：primary <lane>，例如 primary global")
            names = [n for n, _ in ordered_lanes(cfg)]
            if names:
                info(col(f"      现有 lane：{'、'.join(names)}", C.GRAY))
            return 0
        data = cfg["lanes"][cur]
        info(col("── 主要版本 ──", C.BOLD))
        info(f"      lane      : {cur}")
        info(f"      版本      : {data.get('version')}")
        info(f"      端口      : {data.get('port')}")
        info(f"      DSH_HOME : {data.get('home')}")
        refs = read_credential_refs(data.get("home") or "")
        info(f"      API key   : {'、'.join(sorted(refs)) if refs else '（还没有，新建版本时复制不到东西）'}")
        info("")
        info(col("  它是你日常用的那一套：新建 lane 会自动复制它的 API key（只复制密钥，", C.GRAY))
        info(col("  会话与设置仍各自独立）；删除它需要二次确认。", C.GRAY))
        return 0

    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」。先 create 或 adopt。")
        return 1
    if primary_lane(cfg) == lane:
        info(f"lane「{lane}」已经是主要版本了。")
        return 0
    set_primary(cfg, lane)
    ok(f"已把 lane「{lane}」设为主要版本")
    refs = read_credential_refs(cfg["lanes"][lane].get("home") or "")
    if refs:
        info(f"      以后新建的版本会自动继承：{'、'.join(sorted(refs))}")
    else:
        warn("      它的 DSH_HOME 里还没有 API key（.credentials.yaml 无 refs 段），")
        warn("      新建别的版本时复制不到东西——先把它启动一次并填好 key 再设会更划算。")
    return 0


def cmd_sync_key(cfg: dict, args) -> int:
    """把主要版本的 API key 补齐到已有 lane。

    新建 lane 时已经自动复制过一次；这条命令是给"在加这个功能之前就建好的
    lane"补一次的（它们的 DSH_HOME 里只有 browser-session，没有 refs 段）。
    """
    names = list(getattr(args, "lane", None) or [n for n, _ in ordered_lanes(cfg)])
    src = primary_lane(cfg)
    if not src:
        fail("还没有设置主要版本。先跑：primary <lane>")
        return 1
    refs = read_credential_refs(cfg["lanes"][src].get("home") or "")
    if not refs:
        fail(f"主要版本「{src}」的 DSH_HOME 里没有 API key（refs 段为空），没有东西可同步。")
        info(col(f"      该文件：{credentials_path(cfg['lanes'][src].get('home') or '')}", C.GRAY))
        return 1

    info(col(f"── 把主要版本「{src}」的 API key 同步到其它 lane ──", C.BOLD))
    info(f"  主要版本现有：{'、'.join(sorted(refs))}")
    info("")
    checked = 0
    changed = 0
    for lane in names:
        if lane not in cfg.get("lanes", {}):
            warn(f"没有 lane「{lane}」，跳过")
            continue
        checked += 1
        info(f"  · {lane}")
        res = inherit_credentials(cfg, lane)
        print_inherit_result(res)
        if res.get("written"):
            changed += 1
    info("")
    ok(f"完成：检查 {checked} 条，实际写入 {changed} 条。")
    if changed:
        info(col("  注意：正在运行中的实例要重启一次才会用上新的 key。", C.GRAY))
    return 0


def dir_size(path: Path, limit: int = 60000) -> int:
    """目录体积（字节）。entry 数超 limit 就提前收敛，避免扫一个大 HOME 卡住界面。"""
    total = 0
    seen = 0
    stack = [Path(path)]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    seen += 1
                    if seen > limit:
                        return total
                    try:
                        if _is_reparse(entry.path):
                            continue  # junction / symlink：不算体积，也不跟进去（跟进去会重复计 600 遍）
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def human_size(n: int) -> str:
    val = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} GB"


# ======================= 复制一套 DSH（clone） =======================

REPARSE_FLAG = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT：junction 与 symlink 都带这个位


def _is_reparse(path: Path) -> bool:
    """这个条目是不是 junction / symlink。

    为什么专门写一个：Windows 上 **junction（目录联接）的 `os.path.islink()` 返回 False**，
    只查 islink 会把 600 个 junction 当成普通目录递归复制进去——既白复制 1GB 多，
    又会让副本和原件共享同一批宿主代码（后面那条才是要命的）。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if getattr(st, "st_file_attributes", 0) & REPARSE_FLAG:
        return True
    try:
        return bool(os.path.isjunction(path)) or os.path.islink(path)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def _read_link_target(path: Path) -> str | None:
    """读链接目标（**不跟随**）。读不到返回 None。"""
    try:
        return os.readlink(path)
    except OSError:
        return None


def strip_win_prefix(text: str) -> str:
    """`\\\\?\\C:\\...` → `C:\\...`。

    os.readlink 读 junction 会带 `\\\\?\\` 前缀，而 PowerShell 的 LinkTarget 不带——
    两边不归一化就没法比较。
    """
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\"):]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\"):]
    return text


def _manifest_name(path) -> str | None:
    try:
        return json.loads((Path(path) / "package.json").read_text(encoding="utf-8")).get("name")
    except Exception:
        return None


def _manifest_version(path) -> str | None:
    try:
        return json.loads((Path(path) / "package.json").read_text(encoding="utf-8")).get("version")
    except Exception:
        return None


def copy_tree(src: Path, dst: Path, on_progress=None) -> dict:
    """把 src 整棵复制到 dst。

    规则（三条，缺一条就会静默出错）：
      1. 普通文件/目录 → 复制，并保留时间戳；
      2. **junction / symlink 不复制内容、也不照抄链接**，只计数。
         照抄是错的：本机这些链接的目标都是绝对路径，照抄会让副本继续指向**原安装树**
         （`<HOME>/profiles/node_modules/*` → 全局那份 dsh）。那样"副本"其实和原件共享宿主代码，
         升级副本会连带影响原件，而且**不报任何错**——升级测试的结论就假了。
         正确做法是留空，由 DSH 启动时按"这次启动用的安装树"重建（`healProfilesModuleFallback`），
         事后用 `verify` 核对它到底指对了没有。
      3. 单条失败不中断整棵复制，但会记进 errors —— 有 error 的副本不可信，上层必须报错。
    """
    stats = {"files": 0, "dirs": 0, "bytes": 0, "links": 0, "errors": []}
    stack = [(Path(src), Path(dst))]
    while stack:
        s, d = stack.pop()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            stats["errors"].append((str(d), str(exc)))
            continue
        stats["dirs"] += 1
        try:
            entries = list(os.scandir(s))
        except OSError as exc:
            stats["errors"].append((str(s), str(exc)))
            continue
        for entry in entries:
            sp = Path(entry.path)
            dp = d / entry.name
            if _is_reparse(sp):
                stats["links"] += 1
                continue
            try:
                is_dir = entry.is_dir()
            except OSError as exc:
                stats["errors"].append((str(sp), str(exc)))
                continue
            if is_dir:
                stack.append((sp, dp))
                continue
            try:
                shutil.copyfile(sp, dp)
                try:
                    shutil.copystat(sp, dp)  # 保留 mtime：会话文件的"最新时间"要能对上
                except OSError:
                    pass
                stats["bytes"] += entry.stat().st_size
                stats["files"] += 1
            except OSError as exc:
                stats["errors"].append((str(sp), str(exc)))
            if on_progress and stats["files"] and stats["files"] % 400 == 0:
                on_progress(stats)
    return stats


def remap_link_target(target: str, pairs: list[tuple]) -> str:
    """把链接目标里的"源安装树 / 源 HOME"前缀换成副本的对应路径。

    必须按**路径段**匹配：`...\\@deepseek-ai\\dsh` 是 `...\\@deepseek-ai\\dsh-acp` 的字符串前缀，
    但绝不是它的父目录。不做边界判断就会把 `dsh-acp` 重定向成 `<副本>\\-acp`——实测踩到过，
    后果是 230 个链接被误判成"源里已悬空"而跳过（不报错，副本悄悄少一堆包）。
    """
    norm = strip_win_prefix(target)
    low = _norm_win(norm)
    for src_prefix, dst_prefix in pairs:
        s = str(Path(src_prefix))
        sl = _norm_win(s)
        if low == sl:
            return str(dst_prefix)
        if low.startswith(sl + "\\"):
            rest = norm[len(s):].lstrip("\\/")
            return str(Path(dst_prefix) / rest)
    return norm


def make_junction(target, link) -> None:
    """建目录联接（junction）。junction 不需要管理员权限，也不用开发者模式。"""
    link = Path(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    if _is_reparse(link) or link.exists():
        return
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def junction_map(root) -> dict:
    """列出 root 下**所有** junction/symlink → {相对路径: 目标}（不跟进去、不递归链接内部）。"""
    root = Path(root)
    out: dict[str, str] = {}
    if not root.is_dir():
        return out
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for entry in entries:
            sp = Path(entry.path)
            if _is_reparse(sp):
                target = _read_link_target(sp)
                if target:
                    out[str(sp.relative_to(root))] = strip_win_prefix(target)
                continue
            try:
                if entry.is_dir():
                    stack.append(sp)
            except OSError:
                continue
    return out


def relink_junctions(src_root, dst_root, pairs: list[tuple]) -> dict:
    """把 src_root 下所有 junction/symlink，按**副本自己的路径**在 dst_root 重建一遍。

    为什么必须做，而且必须对**安装树和 HOME 都做**：
    · 这些链接的目标都是**绝对路径**。照抄会让副本继续指向原件——"副本"和原件共享同一批代码，
      升级副本会连带影响原件，而且**不报任何错**。
    · 反过来，只跳过不重建也不行：本机 `<prefix>\\node_modules\\<dep>` 就是一批 junction
      （依赖是被"提升"上去的），跳过等于副本**少了 230 个依赖包**，升级测试直接失真。
    · 留空等 DSH 自己建也不行：这一版 DSH 的 runtime 解析**不创建链接**
      （`dsh-app-boot` 文档原话："runtime 解析不创建链接"），实测确认它只建空目录。
      只有"profile 需要现场安装依赖"那条路径会顺手建，你的完整 profile 走不到那儿。

    源里本来就悬空的链接（目标早被新版本删掉、老链接没清）跳过并计数，不算错。
    """
    src_root = Path(src_root)
    dst_root = Path(dst_root)
    out = {"total": 0, "made": 0, "remapped": 0, "skipped": 0, "dangling": 0, "errors": []}
    if not src_root.is_dir():
        return out
    stack = [src_root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as exc:
            out["errors"].append((str(cur), str(exc)))
            continue
        for entry in entries:
            sp = Path(entry.path)
            try:
                dp = dst_root / sp.relative_to(src_root)
            except ValueError:
                continue
            if _is_reparse(sp):
                out["total"] += 1
                target = _read_link_target(sp)
                if not target:
                    out["skipped"] += 1
                    continue
                new_target = remap_link_target(target, pairs)
                if not Path(new_target).exists():
                    # 源里本来就是**悬空**链接：目标早就被新版本删掉了，而这一版 DSH 的
                    # runtime 解析不再重建链接，于是老链接一直留着没人清。复制不了，也不算错。
                    out["dangling"] += 1
                    continue
                if _norm_win(new_target) != _norm_win(strip_win_prefix(target)):
                    out["remapped"] += 1
                try:
                    make_junction(new_target, dp)
                    out["made"] += 1
                except Exception as exc:  # noqa: BLE001
                    out["skipped"] += 1
                    out["errors"].append((str(dp), f"{type(exc).__name__}: {exc}"))
                continue
            try:
                if entry.is_dir():
                    stack.append(sp)
            except OSError:
                continue
    return out


def _merge_relink(*results: dict) -> dict:
    total = {"total": 0, "made": 0, "remapped": 0, "skipped": 0, "dangling": 0, "errors": []}
    for item in results:
        for key in ("total", "made", "remapped", "skipped", "dangling"):
            total[key] += item[key]
        total["errors"].extend(item["errors"])
    return total


def lane_clone_plan(cfg: dict, src: str, new: str, sizes: bool = True) -> dict:
    """算出一条 lane 的复制方案：装到哪、从哪来、多大、空间够不够、有没有硬问题。"""
    data = cfg["lanes"][src]
    p = ensure_layout(cfg)
    src_home = Path(data.get("home") or (p["homes"] / src))
    src_install = lane_install_dir(cfg, src)
    if _manifest_name(src_install) == PKG:
        src_pkg = src_install                       # 接管型：installDir 本身就是 dsh 包目录
        src_root = src_install                      # 复制它的全部内容
    else:
        src_pkg = src_install / "node_modules" / "@deepseek-ai" / "dsh"
        src_root = src_install                      # 常规型：整棵 prefix 一起搬（含提升到顶层的依赖）
    dst_install = p["clones"] / new
    dst_pkg = dst_install / "node_modules" / "@deepseek-ai" / "dsh"
    dst_root = dst_pkg if src_root == src_pkg else dst_install
    plan = {
        "src": src,
        "src_version": str(data.get("version") or "?"),
        "src_home": src_home,
        "src_install": src_install,
        "src_pkg": src_pkg,
        "src_root": src_root,
        "home": p["homes"] / new,
        "install": dst_install,
        "pkg": dst_pkg,
        "root": dst_root,
        "home_size": None,
        "install_size": None,
        "free": None,
        "problems": [],
    }
    if not src_home.is_dir():
        plan["problems"].append(f"源 lane 的 DSH_HOME 不存在：{src_home}")
    if not src_pkg.is_dir():
        plan["problems"].append(f"源 lane 的 dsh 包目录不存在（入口缺失）：{src_pkg}")
    if plan["home"].exists():
        plan["problems"].append(f"目标 DSH_HOME 已存在：{plan['home']}")
    if plan["install"].exists():
        plan["problems"].append(f"目标安装目录已存在：{plan['install']}")
    if sizes:
        plan["home_size"] = dir_size(src_home)
        plan["install_size"] = dir_size(src_install)
        try:
            base = plan["install"] if plan["install"].exists() else Path(cfg["root"])
            plan["free"] = shutil.disk_usage(str(base)).free
        except OSError:
            plan["free"] = None
    return plan


def package_dir_of_entry(entry: Path) -> Path:
    """从入口脚本往上找 dsh 包目录（兼容"包目录"与"prefix 前缀目录"两种安装树写法）。"""
    cur = Path(entry).parent
    for _ in range(4):
        if _manifest_name(cur) == PKG:
            return cur
        cur = cur.parent
    return Path(entry).parent.parent


def count_sessions(home) -> int:
    root = Path(home) / "sessions"
    if not root.is_dir():
        return 0
    total = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        total += sum(1 for name in filenames if name.endswith(".zstd"))
    return total


def profile_bundles(home, profile: str = "web") -> list[str] | None:
    manifest = Path(home) / "profiles" / profile / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    return list(((data.get("dsh") or {}).get("profile") or {}).get("bundles") or [])


def farm_targets(home) -> dict[str, str]:
    """读出"宿主依赖农场"（`<HOME>/profiles/node_modules`）里每个链接指向哪。

    这个目录是 DSH 启动时建的：把宿主自己的依赖用 junction 接进 profile，
    好让 profile 里装的插件能 require 到宿主的包。指向哪，决定插件**实际加载的是哪份宿主代码**——
    所以它是"隔离成立没有"的权威判据。
    """
    farm = Path(home) / "profiles" / "node_modules"
    out: dict[str, str] = {}
    if not farm.is_dir():
        return out
    for scope in farm.iterdir():
        if _is_reparse(scope):
            target = _read_link_target(scope)
            if target:
                out[scope.name] = strip_win_prefix(target)
            continue
        if not scope.is_dir():
            continue
        try:
            children = list(scope.iterdir())
        except OSError:
            continue
        for child in children:
            if _is_reparse(child):
                target = _read_link_target(child)
                if target:
                    out[f"{scope.name}/{child.name}"] = strip_win_prefix(target)
    return out


def _norm_win(path_text: str) -> str:
    return strip_win_prefix(str(path_text)).replace("/", "\\").rstrip("\\").lower()


def cmd_clone(cfg: dict, args) -> int:
    """把一条 lane 连安装树带 DSH_HOME **完整复制**成一条新 lane。

    为什么要"复制"而不是"新建"：新建出来的是空环境——没有你的会话、没有你装的插件、
    没有你的设置，根本测不出插件冲突。复制出来的是当前环境的快照，升级它就能回答
    "这批插件在新版本上还活着吗"，而原件一路不动（还能随时回退）。
    """
    try:
        src = resolve_target(cfg, args.source)
    except KeyError:
        fail(f"找不到 lane 或已安装版本「{args.source}」。先跑 list / instances 看看有哪些。")
        return 1
    new = args.lane
    if new == src:
        fail("源 lane 和目标 lane 不能同名")
        return 1
    if new in cfg["lanes"]:
        fail(f"lane「{new}」已存在。换个名字，或先 `delete {new}`。")
        return 1
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", new):
        fail("lane 名只允许字母 / 数字 / . _ - （1-32 字符）")
        return 1

    plan = lane_clone_plan(cfg, src, new, sizes=True)
    if plan["problems"]:
        for item in plan["problems"]:
            fail(item)
        return 1

    need = int(((plan["home_size"] or 0) + (plan["install_size"] or 0)) * 1.05)
    info(col(f"── 复制 lane「{src}」→「{new}」 ──", C.BOLD))
    info(f"  源安装树    : {plan['src_install']}")
    info(f"  源 DSH_HOME : {plan['src_home']}")
    info(f"  待复制      : 安装树 {human_size(plan['install_size'] or 0)}"
         f" + HOME {human_size(plan['home_size'] or 0)}"
         f" ≈ {human_size(need)}")
    if plan["free"]:
        info(f"  目标盘剩余  : {human_size(plan['free'])}")
        if need > plan["free"]:
            fail(f"空间不足：需要约 {human_size(need)}，只剩 {human_size(plan['free'])}")
            return 1

    src_state = lane_runtime(cfg, src)
    if src_state:
        warn(f"源 lane「{src}」正在运行：正在写入的那个会话文件可能复制到一半（其它内容不受影响）")

    started = time.time()
    last = {"t": 0.0}

    def progress(stats: dict, label: str) -> None:
        now = time.time()
        if now - last["t"] < 2.0:
            return
        last["t"] = now
        info(col(f"      …{label} {stats['files']} 个文件 / {human_size(stats['bytes'])}"
                 f"（跳过 {stats['links']} 个链接）", C.GRAY))

    info("")
    info(col("  [1/4] 复制安装树…", C.CYAN))
    st_install = copy_tree(plan["src_root"], plan["root"], on_progress=lambda s: progress(s, "安装树"))
    write_lane_package_json(plan["install"], plan["src_version"], cfg.get("allow_scripts") or {})

    info(col("  [2/4] 复制 DSH_HOME（会话 / 设置 / 已装插件）…", C.CYAN))
    st_home = copy_tree(plan["src_home"], plan["home"], on_progress=lambda s: progress(s, "HOME"))

    info(col("  [3/4] 重建 junction（安装树 + HOME，全部重定向到副本自己的路径）…", C.CYAN))
    pairs = [
        (plan["src_pkg"], plan["pkg"]),
        (plan["src_install"], plan["install"]),
        (plan["src_home"], plan["home"]),
    ]
    rel = _merge_relink(
        relink_junctions(plan["src_root"], plan["root"], pairs),
        relink_junctions(plan["src_home"], plan["home"], pairs),
    )
    info(col(f"      重建 {rel['made']}/{rel['total']} 个链接"
             f"（重定向 {rel['remapped']} 个，源里已悬空跳过 {rel['dangling']} 个）", C.GRAY))

    info(col("  [4/4] 登记新 lane…", C.CYAN))
    port = int(args.port) if getattr(args, "port", None) else pick_port(cfg)
    cfg["lanes"][new] = {
        "version": plan["src_version"],
        "port": port,
        "home": str(plan["home"]),
        "installDir": str(plan["install"]),
        "kind": "clone",
        "clonedFrom": src,
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_config(cfg)

    errors = list(st_install["errors"]) + list(st_home["errors"])
    links = st_install["links"] + st_home["links"]
    elapsed = time.time() - started
    info("")
    ok(f"lane「{new}」已就绪（复制自 {src}，版本 {plan['src_version']}）")
    info(f"      安装目录  : {plan['install']}")
    info(f"      DSH_HOME  : {plan['home']}")
    info(f"      端口      : {port}")
    info(f"      复制内容  : {st_install['files'] + st_home['files']} 个文件 / "
         f"{human_size(st_install['bytes'] + st_home['bytes'])}，用时 {elapsed:.1f}s")
    info(f"      会话文件  : {count_sessions(plan['home'])} 个"
         f"（源 lane {count_sessions(plan['src_home'])} 个）")
    info(f"      宿主农场  : 重建链接 {rel['made']}/{rel['total']} 个"
         f"，跳过 {links} 个原本的链接（由上面这一步按副本路径重连）")
    if rel["errors"]:
        warn(f"有 {len(rel['errors'])} 个农场链接没建起来（副本仍能跑，但依赖解析结构和原件不一致）")
        for path_text, reason in rel["errors"][:4]:
            info(col(f"      {path_text}：{reason}", C.GRAY))
        info(col(f"      核对： py {ME_NAME} verify {new}", C.CYAN))
    refs = read_credential_refs(plan["home"])
    info(f"      API key   : {'、'.join(sorted(refs)) if refs else '无（源 HOME 里就没有）'}")

    if errors:
        info("")
        fail(f"有 {len(errors)} 个条目没能复制——**这个副本不可信**，别拿它下结论。")
        for path_text, reason in errors[:6]:
            info(col(f"      {path_text}：{reason}", C.GRAY))
        if len(errors) > 6:
            info(col(f"      …还有 {len(errors) - 6} 条", C.GRAY))
        warn("如果源 lane 正在运行，先 `stop` 它再删掉这个副本重来：")
        info(col(f"      py {ME_NAME} delete {new} --stop", C.CYAN))
        return 1

    info("")
    info(col(f"  下一步①： py {ME_NAME} open {new} --no-browser", C.CYAN))
    info(col("          （副本有自己的安装树和 HOME，可以和原件同时开着）", C.GRAY))
    info(col(f"  下一步②： py {ME_NAME} verify {new}   # 核对隔离真的成立", C.CYAN))
    info(col(f"  下一步③： py {ME_NAME} upgrade {new} <版本>   # 升级这个副本，原件一点不动", C.CYAN))
    return 0


# ======================= 升级到指定版本 =======================
#
# 目标：把某一条 lane **精确**换成另一个版本，并且随时能退回去。
# 三种安装树的升级方式不同，按树在哪自动判：
#
#   tree     ：`<root>\versions\<版本>`（本启动器装的 next / stable）→ 装一棵新树 + 改登记。
#              旧树原地留着，所以"退回去"是**秒级**的（只改登记，不重装、不联网）。
#   clone    ：`<root>\clones\<lane>`（复制出来的副本）→ **就地** `npm install --prefix`。
#              路径不变，所以 HOME 里那几百条 junction 一条都不用重连（复制时领教过这点）。
#   external ：接管来的安装（npm 全局那份、Desktop 自带的 app.asar）→ **这一步不动它**。
#              改它等于改你日常真正在用的那一套，得单独一步、单独确认。
#
# 三道保险：①运行中拒绝升级（除非 `--stop`，升完还给你起回来）；
# ②升完核对（入口 / 实际版本 / 补丁层体检）；③**启动冒烟**——真起一次再停回去，
# 起不来**自动退回**到升级前的版本。


def lane_upgrade_plan(cfg: dict, lane: str) -> dict:
    """这条 lane 该怎么升级：树在哪、走哪种方式、旧版本能不能秒退。"""
    data = cfg["lanes"][lane]
    p = paths(cfg)
    install = data.get("installDir")
    if not install:
        vdir = p["versions"] / str(data.get("version") or "")
        return {
            "mode": "tree",
            "dir": vdir,
            "why": f"本启动器装的（{p['versions']}\\<版本>）：换版本＝换一棵树，旧树留着，退回是秒级的",
        }
    idir = Path(install)
    try:
        idir.relative_to(p["clones"])
        return {
            "mode": "clone",
            "dir": idir,
            "why": f"副本自己的安装树（{idir}）：就地升级，路径不变，HOME 里的链接一条都不用重连",
        }
    except ValueError:
        pass
    kind = "Desktop 自带的（app.asar）" if "app.asar" in str(idir) else "接管来的外部安装"
    return {"mode": "external", "dir": idir, "why": f"{kind}（{idir}）"}


def npm_install_in_place(cfg: dict, tree: Path, version: str, follow: bool = True) -> None:
    """在**已有的**安装树里就地换版本（`npm install --prefix <tree> <pkg>@<version>`）。

    副本走这条路的关键理由是**路径不变**：DSH 的 HOME 里那些 junction 记的是绝对路径，
    换个目录就得逐条重连（复制那一步已经为此写了一整套 `relink_junctions`）；
    就地升级则一条都不用动。
    """
    p = ensure_layout(cfg)
    node = find_node(cfg)
    npm_cli = find_npm_cli(cfg, node)
    if not node or not npm_cli:
        raise RuntimeError("找不到 node 或 npm-cli.js，请检查 Node.js 安装（可用 doctor 子命令）")
    write_lane_package_json(tree, version, cfg.get("allow_scripts") or {})
    env = build_npm_env(cfg, node)
    log = p["logs"] / f"upgrade-{tree.name}-{version}.log"
    cmd = [
        node, npm_cli, "install", "--prefix", str(tree), f"{PKG}@{version}",
        "--no-audit", "--no-fund", "--loglevel=notice",
    ]
    info(col(f"正在原地安装 {PKG}@{version} → {tree}", C.BOLD))
    info(col(f"（日志：{log}）", C.GRAY))
    code = run_logged(cmd, cwd=tree, env=env, log_path=log, follow=follow)
    got = installed_version(tree)
    if code != 0 or got != version:
        raise RuntimeError(f"原地安装失败（npm 退出码 {code}，实际装到 {got!r}）。完整日志：{log}")


def upgrade_checks(cfg: dict, lane: str, version: str, tree: Path,
                   home: Path) -> tuple[list[str], list[str]]:
    """升完核对 → (致命问题, 只是提醒的问题)。

    分开的原因：补丁层体检报出的东西**不是这次升级造成的**（升级没碰过那一层），
    不该拿它去触发自动退回；只有"入口没了 / 版本不对 / 起不来"才是升级本身失败。
    """
    fatal: list[str] = []
    notes: list[str] = []
    entry = lane_entry_script(cfg, lane)
    if not entry.is_file():
        fatal.append(f"升级后找不到入口脚本：{entry}")
    actual = installed_version(tree)
    if actual != version:
        fatal.append(f"升级后安装树里的实际版本是 {actual!r}，不是 {version!r}")
    else:
        ok(f"安装树里的实际版本 = {actual}")
    if entry.is_file():
        pkg_dir = package_dir_of_entry(entry)
        for patch_path in (profile_dir(home) / PATCH_LAYER_FILE, Path(home) / PATCH_LAYER_FILE):
            if patch_path.is_file():
                report_patch_layer(cfg, home, pkg_dir, patch_path, notes, tree)
    farm = farm_targets(home)
    if farm:
        inside = sum(1 for t in farm.values() if _norm_win(t).startswith(_norm_win(str(tree))))
        info(f"  宿主农场  : {len(farm)} 条链接，指向本次安装树的 {inside} 条")
        if inside != len(farm):
            notes.append(
                f"宿主农场里有 {len(farm) - inside} 条链接还指着别的安装树——"
                "宿主启动时会自己按当次安装重指（下面的启动冒烟就会做这件事）"
            )
    return fatal, notes


def cmd_upgrade(cfg: dict, args) -> int:
    me = ME_NAME
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」（现有：{'、'.join(cfg.get('lanes', {})) or '无'}）")
        return 1
    data = cfg["lanes"][lane]
    p = ensure_layout(cfg)
    plan = lane_upgrade_plan(cfg, lane)

    info(col(f"── 升级 lane「{lane}」──", C.BOLD))
    info(f"  当前版本  : {data.get('version')}")
    info(f"  安装树    : {plan['dir']}")
    info(f"  升级方式  : {plan['why']}")

    if plan["mode"] == "external":
        fail(
            f"「{lane}」的安装树在本启动器目录之外 —— 改它等于改你日常真正在用的那一套，"
            "这一步有意不动它。"
        )
        info(col("      要动它得单独一步：先停实例 → 换那一份安装 → 起回来 → 确认能退回。", C.GRAY))
        info(col(f"      只想看状态： py {me} list / py {me} instances", C.CYAN))
        return 1

    # 目标版本：--rollback 时取上次升级前记下的版本
    rollback = bool(getattr(args, "rollback", False))
    spec = data.get("previousVersion") if rollback else getattr(args, "version", None)
    if not spec:
        fail("要写明目标版本（例如 0.1.7-rc.2 / latest），或用 --rollback 退回上一次的版本")
        return 1
    registry_note = ""
    packument = None
    try:
        packument, reg = fetch_index(cfg)
        registry_note = f"（registry {reg}）"
    except Exception as exc:  # noqa: BLE001 —— 离线也可能只是"目标版本早装好了"
        if re.match(r"^\d+\.\d+\.\d+", str(spec)):
            version = str(spec)
            warn(f"连不上 registry（{exc}）——就按写死的 {version} 装，装不上会明确报错")
        else:
            fail(f"连不上 registry，也没法解析 {spec!r}：{exc}")
            return 1
    if packument is not None:
        try:
            version = resolve_tag(packument, str(spec))
        except Exception:  # noqa: BLE001
            fail(f"registry 上没有这个版本/tag：{spec!r}（可能打错了，或它还没发布）")
            known = sorted_versions(packument)
            if known:
                info(col(f"      已有的最近几版：{'、'.join(known[-6:])}", C.GRAY))
                info(col(f"      想按 tag 装： py {me} upgrade {lane} latest", C.CYAN))
            return 1
    tagnote = f"{spec} → {version} " if str(spec) != version else f"{version} "
    info(col(f"  目标版本  : {tagnote}{registry_note}", C.CYAN))

    current = str(data.get("version") or "")
    before = installed_version(plan["dir"]) or current
    if version == before:
        ok(f"lane「{lane}」已经就是 {version}，不用动")
        return 0

    running = lane_runtime(cfg, lane)
    was_running = bool(running)

    if getattr(args, "dry_run", False):
        info("")
        info(col("  --dry-run：只算不做。真执行会按这个顺序来：", C.CYAN))
        steps: list[str] = []
        if plan["mode"] == "tree":
            reuse = installed_version(p["versions"] / version) == version
            steps.append(f"装一棵新树 {p['versions'] / version}"
                         + ("（本地已有，直接复用、不联网）" if reuse else "（走 npm）"))
            steps.append(f"把登记改成 {version}；旧树 {plan['dir']} 留着 → 退回是秒级的")
        else:
            steps.append(f"就地 npm install --prefix {plan['dir']} {PKG}@{version}（路径不变）")
        if running:
            steps.append(f"先停掉它（现在跑着：pid {running.get('pid')}，端口 {running.get('port')}）"
                         "—— 不带 --stop 的话真执行会被拒绝")
        steps.append("核对（入口 / 实际版本 / 补丁层）+ 启动冒烟（真起一次再停回去）")
        steps.append(f"起不来就自动退回 {before}")
        if was_running:
            steps.append("升完把它起回来（它升级前是运行中的）")
        for index, step in enumerate(steps, 1):
            info(f"    {index}) {step}")
        return 0

    if running and not getattr(args, "stop", False):
        fail(
            f"lane「{lane}」正在运行（pid {running.get('pid')}，端口 {running.get('port')}）——"
            "升级要换掉它的安装树。先停："
        )
        info(col(f"      py {me} stop {lane}          # 或加 --stop 让升级自己停，升完再起回来", C.CYAN))
        return 1

    if running:
        info(col("  先停掉它（--stop）…", C.CYAN))
        if cmd_stop(cfg, argparse.Namespace(target=[lane])) != 0:
            fail("停不掉，放弃升级")
            return 1
        info("")

    started = time.time()
    try:
        if plan["mode"] == "tree":
            vdir = install_version(cfg, version)
        else:
            npm_install_in_place(cfg, plan["dir"], version)
            vdir = plan["dir"]
    except Exception as exc:  # noqa: BLE001
        fail(f"安装失败：{exc}")
        info(col(f"      登记没动，lane「{lane}」还是 {before}（安装树可能被 npm 改了一半）", C.GRAY))
        info(col(f"      想干净重来： 再跑一次  py {me} upgrade {lane} {version}", C.CYAN))
        return 1

    data["version"] = version
    data["previousVersion"] = before
    data["upgradedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_config(cfg)
    home = Path(data.get("home") or (p["homes"] / lane))

    info("")
    info(col("  [核对]", C.CYAN))
    fatal, notes = upgrade_checks(cfg, lane, version, vdir, home)
    for item in fatal:
        fail(item)

    smoke_ok = True
    if not getattr(args, "no_boot_check", False):
        info("")
        info(col("  [启动冒烟] 真起一次再停回去（这一步会顺手把宿主农场重指到新安装树）…", C.CYAN))
        smoke_ok = boot_smoke(cfg, lane, timeout=180)
        if smoke_ok:
            ok("起来过，又停回去了")
        else:
            fail(f"起不来。日志：{p['logs'] / (lane + '.log')}")

    if fatal or not smoke_ok:
        info("")
        warn(f"这次升级不成立 —— 自动退回 {before}…")
        try:
            if plan["mode"] == "tree":
                install_version(cfg, before)
            else:
                npm_install_in_place(cfg, plan["dir"], before)
        except Exception as exc:  # noqa: BLE001
            fail(f"自动退回也失败了：{exc}")
            info(col(f"      手动退回： py {me} upgrade {lane} {before}", C.CYAN))
            return 1
        data["version"] = before
        data["previousVersion"] = version
        data["upgradedAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_config(cfg)
        ok(f"已退回 {before}（登记和安装树都改回来了）")
        if was_running:
            info(col("      它升级前是运行中的，正在起回来…", C.CYAN))
            cmd_open(cfg, argparse.Namespace(target=lane, port=None, cwd=None, timeout=None,
                                            no_browser=False, detach=True, adopt_only=False))
        return 1

    info("")
    for item in notes:
        warn(item)
    ok(f"lane「{lane}」已升到 {version}（原 {before}）")
    info(f"      用时      : {time.time() - started:.1f}s")
    info(f"      退回      : py {me} upgrade {lane} --rollback")
    info(f"      核对      : py {me} verify {lane}")
    if plan["mode"] == "tree":
        info(col(f"      旧树还在  : {plan['dir']}（所以退回不用重装）", C.GRAY))
    if was_running:
        info(col("      它升级前是运行中的，正在起回来…", C.CYAN))
        cmd_open(cfg, argparse.Namespace(target=lane, port=None, cwd=None, timeout=None,
                                         no_browser=False, detach=True, adopt_only=False))
    else:
        info(col(f"      打开      : py {me} open {lane}", C.CYAN))
    return 0


def cmd_verify(cfg: dict, args) -> int:
    """核对一条 lane 的"隔离"是不是真的成立。

    专治最阴的一类错误：副本看起来能跑，插件却悄悄 require 到了**另一个版本**的宿主代码——
    不报错、不崩溃，只是"升级测试"的结论变成假的。所以这里比的是权威判据：
    宿主依赖农场（`profiles/node_modules`）里每个链接**实际指向哪**。
    """
    lane = args.lane
    data = cfg.get("lanes", {}).get(lane)
    if not data:
        fail(f"没有 lane「{lane}」")
        return 1
    try:
        version, vdir, home = require_version_installed(cfg, lane)
    except RuntimeError as exc:
        fail(str(exc))
        return 1
    entry = lane_entry_script(cfg, lane)
    pkg_dir = package_dir_of_entry(entry)
    info(col(f"── 核对 lane「{lane}」──", C.BOLD))
    info(f"  版本      : {version}")
    info(f"  安装树    : {vdir}")
    info(f"  dsh 包目录: {pkg_dir}")
    info(f"  DSH_HOME  : {home}")

    problems: list[str] = []
    try:
        entry.relative_to(vdir)
        ok("入口脚本在本 lane 自己的安装树里")
    except ValueError:
        problems.append(f"入口脚本不在本 lane 的安装树里：{entry}（vdir={vdir}）")
    actual = _manifest_version(pkg_dir)
    if actual and version and actual != version:
        problems.append(f"登记版本 {version} 与安装树里的实际版本 {actual} 不符")

    src = data.get("clonedFrom")
    src_home = None
    src_pkg = None
    src_install_dir = None
    if src and src in cfg["lanes"]:
        try:
            _v, src_install_dir, src_home = require_version_installed(cfg, src)
            src_pkg = package_dir_of_entry(lane_entry_script(cfg, src))
        except RuntimeError:
            src_home = None

    # 副本被 `upgrade` 升到别的版本之后，"按源 lane 逐条比对"就失去意义了：
    # 源还是旧版本，它的依赖闭包和副本的已经不同（新版会加/删包），比出来的"差异"
    # 全是版本差异、不是隔离问题 —— 会把人引到错的方向。这时只核对另一条更强的性质：
    # **每条链接都指向副本自己的安装树**。
    same_as_source = True
    if src and src in cfg["lanes"]:
        src_ver = str(cfg["lanes"][src].get("version") or "")
        if src_ver and version and src_ver != version:
            same_as_source = False
            warn(f"副本已是 {version}，源 lane「{src}」还是 {src_ver} —— 版本不同，"
                 "逐条比对源 lane 没有意义；改为核对「每条链接都指向本 lane 自己」")

    # 安装树里的 junction 也要逐条比：本机 <prefix>\node_modules\<dep> 就是一批 junction，
    # 少了它们副本就是"少了 230 个依赖包"，而这种缺失不报错、只是行为不一样。
    if src and src_install_dir and _manifest_name(src_install_dir) == PKG:
        src_root, dst_root = src_install_dir, pkg_dir
    else:
        src_root, dst_root = src_install_dir, vdir
    if src_root and Path(src_root).is_dir() and same_as_source:
        src_j = junction_map(src_root)
        my_j = junction_map(dst_root)
        live_j = {k: v for k, v in src_j.items() if Path(v).exists()}
        pairs_j = [(src_pkg, pkg_dir), (src_install_dir, vdir)]
        want_j = {k: remap_link_target(v, pairs_j) for k, v in live_j.items()}
        bad_j = sorted(
            k for k, want in want_j.items() if _norm_win(my_j.get(k, "")) != _norm_win(want)
        )
        info(f"  安装树链接: 源 {len(src_j)} 条（活 {len(live_j)}）/ 本 lane {len(my_j)} 条，"
             f"逐条一致 {len(want_j) - len(bad_j)} 条")
        if bad_j:
            problems.append(
                f"有 {len(bad_j)} 个安装树链接没对上（副本的依赖不完整或指回了原件）：{bad_j[:5]}"
            )

    mine = farm_targets(home)                        # {包名: 实际指向}
    source_links = farm_targets(src_home) if (src_home and same_as_source) else {}

    if getattr(args, "fix", False) and src_home and src_pkg:
        info(col("  --fix：按源 lane 重连 junction（安装树 + HOME）…", C.CYAN))
        pairs = [(src_pkg, pkg_dir), (src_install_dir, vdir), (src_home, home)]
        rel = _merge_relink(
            relink_junctions(src_install_dir, vdir, pairs),
            relink_junctions(src_home, home, pairs),
        )
        info(col(f"      重建 {rel['made']}/{rel['total']} 个链接（重定向 {rel['remapped']} 个）", C.GRAY))
        if rel["errors"]:
            for path_text, reason in rel["errors"][:4]:
                info(col(f"      {path_text}：{reason}", C.GRAY))
        mine = farm_targets(home)

    if source_links:
        # 有源可比（副本）：逐条比对 —— 副本每条链接都应等于"源的目标，把源安装树前缀换成副本的"
        # 源里已悬空的（目标不存在）不算差异：那种链接谁也建不出来，源自己也是坏的。
        live = {n: t for n, t in source_links.items() if Path(t).exists()}
        dangling = len(source_links) - len(live)
        # 前缀形态的安装树里，依赖是"提升"到 <install>\node_modules\<dep> 的（不在 dsh 包目录里），
        # 所以映射前缀必须和 clone 用同一组：包目录 → 整棵安装树 → HOME。
        pairs = [(src_pkg, pkg_dir), (src_install_dir, vdir), (src_home, home)]
        expected = {
            name: remap_link_target(target, pairs)
            for name, target in live.items()
        }
        missing = sorted(n for n in expected if n not in mine)
        bad = sorted(
            (n, mine[n], expected[n])
            for n in expected
            if n in mine and _norm_win(mine[n]) != _norm_win(expected[n])
        )
        extra = sorted(n for n in mine if n not in expected)
        info(f"  宿主农场  : 源「{src}」{len(source_links)} 条（其中源里已悬空 {dangling} 条）"
             f" / 本 lane {len(mine)} 条，逐条比对一致 {len(expected) - len(missing) - len(bad)} 条")
        if missing:
            problems.append(
                f"有 {len(missing)} 条农场链接没建起来（依赖解析结构和原件不一致）：{missing[:5]}"
            )
        if bad:
            problems.append(
                f"有 {len(bad)} 条农场链接指向了**别的安装树**——插件会加载到别的版本的宿主代码，"
                "这条 lane 的测试结论不可信"
            )
            for name, got, want in bad[:4]:
                info(col(f"      ✗ {name}  实际 → {got}", C.RED))
                info(col(f"               应为 → {want}", C.GRAY))
        if extra:
            warn(f"有 {len(extra)} 条链接是源 lane 没有的：{extra[:5]}")
        if not (missing or bad):
            ok("农场逐条与源 lane 对应，且全部指向本 lane 自己的安装树（隔离成立）")
    elif mine:
        # 没有源可比（普通 lane / 接管的 lane / 已升级到别的版本的副本）：
        # 只要求链接都指向本 lane 自己的安装树。
        # 注意"自己的安装树"有**两个合法根**：接管型里 vdir 就是 dsh 包目录，
        # 前缀型里依赖被提升到 `<安装树>\node_modules\<dep>`（不在 dsh 包目录下面）——
        # 只判 pkg_dir 会把前缀型的整片提升依赖误报成"指向别处"。
        own_roots = {_norm_win(str(vdir)), _norm_win(str(pkg_dir))}
        ours = lambda target: any(_norm_win(target).startswith(root) for root in own_roots)
        same = sum(1 for t in mine.values() if ours(t))
        info(f"  宿主农场  : {len(mine)} 条链接，其中指向本 lane 安装树的 {same} 条")
        outside = sorted((n, t) for n, t in mine.items() if not ours(t))
        if outside:
            warn(f"有 {len(outside)} 条指向别处（例如 DSH Desktop 自带那份），逐条看一眼：")
            for name, target in outside[:6]:
                info(col(f"      · {name} → {target}", C.GRAY))
        else:
            ok("农场里全部链接都指向本 lane 自己的安装树（隔离成立）")
    else:
        farm_dir = Path(home) / "profiles" / "node_modules"
        if farm_dir.exists():
            problems.append(f"宿主依赖农场里一条链接都没有：{farm_dir}")
        else:
            info(col(f"  宿主农场  : 不存在（{farm_dir}）——源 lane 没有农场时属正常", C.GRAY))

    # pnpm 账本（.modules.yaml）：复制出来的副本会带着**原件**的 virtualStoreDir。
    # 它平时不影响启动、也不报错，但一旦要用 pnpm 给这条 lane 装/更新插件，
    # pnpm 会直接拒绝干活（ERR_PNPM_UNEXPECTED_VIRTUAL_STORE），而且它指的是别条 lane 的目录。
    ledger = pnpm_ledger_state(cfg, lane, PROFILE_NAME)
    if ledger["exists"]:
        info(f"  pnpm 账本 : store={ledger['store'] or '?'}")
        info(col(f"              virtualStore={ledger['virtual'] or '?'}", C.GRAY))
        if ledger["outside"]:
            if getattr(args, "fix", False):
                rep = repair_pnpm_virtual_store(cfg, lane, PROFILE_NAME)
                if rep["changed"]:
                    info(col(f"      --fix：已改成 {rep['want']}", C.CYAN))
                    ledger = pnpm_ledger_state(cfg, lane, PROFILE_NAME)
                else:
                    warn(f"      --fix 没能改动：{rep['error'] or '没找到该字段'}")
            if ledger["outside"]:
                problems.append(
                    "pnpm 账本里的 virtualStoreDir 指在本 lane 之外（"
                    f"{ledger['virtual']}）——用 pnpm 给这条 lane 装/更新插件会被它拒绝；"
                    "verify --fix 可就地修正"
                )
        else:
            ok("pnpm 账本里的 virtualStoreDir 指在本 lane 自己身上")
    else:
        info(col("  pnpm 账本 : 不存在（这个 profile 不是 pnpm 装的）", C.GRAY))

    # 本地插件（`file:` 依赖）：pnpm 把解析结果记成相对路径，换个深度就指不到地方。
    # 复制/搬动之后必然遇到，所以这里也核一遍（实测报错长这样：ENOENT scandir 'D:\...\Desktop\...'）。
    file_specs = pnpm_file_spec_state(cfg, lane, PROFILE_NAME)
    for item in file_specs:
        if item["broken"]:
            if getattr(args, "fix", False):
                rep = repair_pnpm_file_specs(cfg, lane, PROFILE_NAME)
                if rep["changed"]:
                    for line in rep["changed"]:
                        info(col(f"      --fix：{line}", C.CYAN))
                    file_specs = pnpm_file_spec_state(cfg, lane, PROFILE_NAME)
                    break
            problems.append(
                f"本地依赖 {item['name']} 指不到地方：{item['lock'] or item['specifier']} → "
                f"{item['resolved']}（复制/搬动后深度变了，pnpm 账本里记的相对路径不再成立；"
                "verify --fix 可以换成绝对路径）"
            )
    if file_specs and not any(item["broken"] for item in file_specs):
        ok(f"本地依赖（file:）{len(file_specs)} 个都能指到地方")

    # 补丁层体检（2026-09-27 那次故障的防线）：真解析一遍 + 市场读不读得到。
    # 这是"下一次启动会不会直接挂"的唯一提前判据 —— 补丁层坏了，症状只会出现在启动那一刻。
    for patch_path in (profile_dir(home) / PATCH_LAYER_FILE, Path(home) / PATCH_LAYER_FILE):
        if patch_path.is_file():
            report_patch_layer(cfg, home, pkg_dir, patch_path, problems, vdir)
        else:
            info(col(f"  补丁层    : 无（{patch_path}）", C.GRAY))

    if src and src in cfg["lanes"]:
        if src_home:
            a, b = count_sessions(home), count_sessions(src_home)
            line = f"  会话文件  : 本 lane {a} 个 / 源 lane「{src}」{b} 个"
            info(line)
            if a < b:
                warn("比源 lane 少（源 lane 在复制后又写过会话，或复制时有文件被占用）")
            ba, bb = profile_bundles(home), profile_bundles(src_home)
            if ba is not None and bb is not None:
                if ba == bb:
                    ok(f"插件清单一致（{len(ba)} 个组合包，含官方 {sum(1 for x in ba if x.startswith('@deepseek-ai/'))} 个）")
                else:
                    only_a = [x for x in ba if x not in bb]
                    only_b = [x for x in bb if x not in ba]
                    problems.append(f"插件清单与源 lane 不一致：多 {only_a} 少 {only_b}")
    info("")
    if problems:
        for item in problems:
            fail(item)
        return 1
    ok("核对通过")
    return 0


# ======================= 插件开关（一键禁用自己装的插件） =======================

PROFILE_NAME = "web"
BLOCK_BEGIN = "# >>> dsh-lanes 禁用第三方插件（启动器自动生成，勿手改；用 plugins-on 撤销）"
BLOCK_END = "# <<< dsh-lanes 禁用第三方插件结束"

# 插件市场（dshmarket）有自己的开关块：它是最需要"随手关掉/随手打开"的那一个
# （关掉别的插件容易，关掉"插件市场"本身以前得手改 YAML）。两个块互不干扰：
# plugins-on 只删上面那段，market-on 只删下面这段。
MARKET_PKG = "dshmarket"
MARKET_BLOCK_BEGIN = "# >>> dsh-lanes 插件市场开关（启动器自动生成，勿手改；用 market-on 撤销）"
MARKET_BLOCK_END = "# <<< dsh-lanes 插件市场开关结束"
# pnpm 的"新版本按住"一次性绕过：必须用 .npmrc 的拼法（`--config.minimumReleaseAge=0` 在
# pnpm 12.3+ 的原生 CLI 上会被静默忽略——不报未知选项，只是白写）。这是 dshmarket 自己的
# 源码里给的结论（lib/install.js 的 RELEASE_AGE_OVERRIDE），不是我猜的。
MARKET_BYPASS_FLAG = "--config.minimum-release-age=0"

DUMP_ID_RE = re.compile(r"^(\s*)-\s+id:\s*['\"]?([A-Za-z0-9_.@/:-]+)['\"]?\s*$")
DUMP_DISABLED_RE = re.compile(r"^\s*disabled:\s*(.*)$")
PATCH_ID_RE = re.compile(r"^\s*(?:-\s*)?id:\s*['\"]?([A-Za-z0-9_.@/:-]+)['\"]?\s*$")
UNMATCHED_RE = re.compile(r'patch:\s*entry\s+"?([^"\s]+)"?\s+not found')
SKIPPED_RE = re.compile(r'skipping profile bundle\s+"?([^":\s]+)"?', re.I)


def profile_dir(home, profile: str = PROFILE_NAME) -> Path:
    return Path(home) / "profiles" / profile


def read_profile_bundles(home, profile: str = PROFILE_NAME) -> list[str]:
    """profile 里启用了哪些组合包（顺序有意义：loader 是有序的）。"""
    manifest = profile_dir(home, profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return []
    return list((((data.get("dsh") or {}).get("profile") or {}).get("bundles")) or [])


def is_official_bundle(pkg: str) -> bool:
    return pkg.startswith("@deepseek-ai/")


def patch_ids(text: str) -> list[str]:
    """取这个组合包**插入**的行 id（只认 `insert:` 块里的，覆盖行不算）。

    **行 id 不等于包名**——实测：`dshmarket` 的行 id 是 `dsh-market`，
    `beauticode-dsh` 是 `beauticode-bridge`，`dsh-plugin-browser` 是 `browser`。
    写错 id 不会报错，只会被静默跳过（`patch: entry X not found`）。

    为什么只取 insert：补丁文件里既有 `- insert:`（本包新增的行），也有裸的 `- id: X`
    （**覆盖别人的行**）。实测踩到：`dsh-mnemon` 的补丁里有一条 `- id: connection`，
    那是给宿主修服务作用域的覆盖行；把它当成"本包的行"禁掉，官方那批等 `connection`
    服务的行就永远不激活，整个应用起不来（日志只会含糊地写 `7 entries did not activate`）。

    还要跳过注释（注释里常带示例 `- insert: - id: ...`）和 `!!js` 表达式正文。
    """
    ids: list[str] = []
    insert_indent: int | None = None
    js_indent: int | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if "!!js" in raw:
            js_indent = indent
            continue
        if js_indent is not None:
            if indent > js_indent:
                continue
            js_indent = None
        stripped = raw.lstrip()
        if re.match(r"^-\s*insert:\s*$", stripped):
            insert_indent = indent
            continue
        if stripped.startswith("- insert:"):
            # 行内写法：- insert: [{id: browser, name: '...'}]
            for inline in re.findall(r"[{,]\s*id:\s*['\"]?([A-Za-z0-9_.@/:-]+)", stripped):
                if inline not in ids:
                    ids.append(inline)
            continue
        if insert_indent is None:
            continue
        if indent <= insert_indent:
            insert_indent = None
            continue
        match = PATCH_ID_RE.match(raw)
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def bundle_row_ids(home, pkg: str, profile: str = PROFILE_NAME) -> list[str]:
    """这个组合包会往 loader 里插哪些行。优先按 package.json 里的 `dsh.bundle.patch` 找补丁文件。"""
    base = profile_dir(home, profile) / "node_modules" / pkg
    patch: Path | None = None
    manifest = base / "package.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            rel = ((data.get("dsh") or {}).get("bundle") or {}).get("patch") or ""
            if rel:
                patch = base / str(rel).lstrip("./")
        except Exception:
            patch = None
    if patch is None or not patch.is_file():
        fallback = base / "cordis.patch.yml"
        patch = fallback if fallback.is_file() else None
    if patch is None:
        return []
    try:
        return patch_ids(patch.read_text(encoding="utf-8"))
    except OSError:
        return []


def parse_dump(text: str) -> dict[str, dict]:
    """把 `--dump-config` 的 YAML 解成 {行 id: {name, disabled, expr}}。

    只要有 id 行就起一条记录，随后缩进更深的 `disabled:` 归属它——不引 yaml 依赖，
    因为这个输出是我们自己要看的两三个字段，规则稳定。
    """
    rows: dict[str, dict] = {}
    cur: str | None = None
    cur_indent = 0
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = DUMP_ID_RE.match(raw)
        if match:
            cur = match.group(2)
            cur_indent = len(match.group(1))
            rows.setdefault(cur, {"name": "", "disabled": False, "expr": False})
            continue
        if cur is None:
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent <= cur_indent:
            cur = None
            continue
        name_match = re.match(r"^\s*name:\s*(.+?)\s*$", raw)
        if name_match:
            rows[cur]["name"] = name_match.group(1).strip("'\"")
            continue
        off_match = DUMP_DISABLED_RE.match(raw)
        if off_match:
            value = off_match.group(1).strip()
            if value.startswith("!!js"):
                rows[cur]["expr"] = True
            else:
                rows[cur]["disabled"] = value.lower() in ("true", "yes", "1")
    return rows


def run_dump_config(cfg: dict, lane: str, profile: str = PROFILE_NAME, timeout: int = 300) -> dict:
    """离线跑一次 `dsh --profile <p> --dump-config`，拿**组合之后的真实行清单**。

    这是唯一能证明"禁用真的生效"的手段：它打印每一行的 id / name / disabled，
    并把 `patch: entry X not found` 这种**静默跳过**写进 stderr。

    两个实测细节：
    · 它**不是只读**——会重写 `<home>/profiles/<p>/cordis.yml`（那是每次启动都会重新生成的）。
    · 子进程输出直接接**文件句柄**，不用管道：某些沙箱下管道会被拒绝（EPERM）。
    """
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    node = find_node(cfg)
    entry = lane_entry_script(cfg, lane)
    out_p = paths(cfg)["logs"] / f".dump-{lane}.txt"
    err_p = paths(cfg)["logs"] / f".dump-{lane}.err"
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    if node:
        node_dir = str(Path(node).parent)
        if node_dir not in env.get("PATH", ""):
            env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as fo, open(err_p, "w", encoding="utf-8") as fe:
            rc = subprocess.run(
                [str(node), str(entry), "--profile", profile, "--dump-config"],
                stdout=fo,
                stderr=fe,
                env=env,
                timeout=timeout,
                cwd=str(cfg.get("default_cwd") or home),
            ).returncode
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "rc": -1, "error": f"{type(exc).__name__}: {exc}", "rows": {}, "unmatched": [], "skipped": []}
    text = out_p.read_text(encoding="utf-8", errors="replace") if out_p.is_file() else ""
    err = err_p.read_text(encoding="utf-8", errors="replace") if err_p.is_file() else ""
    for path in (out_p, err_p):
        try:
            path.unlink()
        except OSError:
            pass
    return {
        "ok": rc == 0,
        "rc": rc,
        "rows": parse_dump(text),
        "unmatched": sorted(set(UNMATCHED_RE.findall(err))),
        "skipped": sorted(set(SKIPPED_RE.findall(err))),
        "stderr": err,
    }


def plugin_patch_path(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> Path:
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    return profile_dir(home, profile) / "cordis.patch.yml"


def lane_home(cfg: dict, lane: str) -> Path:
    data = cfg["lanes"][lane]
    return Path(data.get("home") or (paths(cfg)["homes"] / lane))


def find_block(lines: list[str], begin: str = BLOCK_BEGIN, end: str = BLOCK_END) -> tuple[int, int] | None:
    """找我写的那一段（起止行号，含两端）。找不到返回 None。

    启动器会往用户层写**两种**带标记的块（一键禁用 / 插件市场开关），
    所以起止标记必须能指定——否则会认错别人的块。
    """
    b = e = None
    for index, line in enumerate(lines):
        if line.strip() == begin:
            b = index
        elif line.strip() == end and b is not None:
            e = index
            break
    if b is None or e is None:
        return None
    return b, e


def strip_launcher_blocks(lines: list[str]) -> list[str]:
    """去掉启动器写的全部块（两种），只留下用户自己的内容。"""
    out = list(lines)
    for begin, end in ((BLOCK_BEGIN, BLOCK_END), (MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)):
        while True:
            span = find_block(out, begin, end)
            if not span:
                break
            out = out[: span[0]] + out[span[1] + 1:]
    return out


def user_layer_text(text: str) -> str:
    """只剩"你自己写的内容"的正文（去掉启动器的两个块）。

    用来回答一个具体问题：撤销时发现文件与备份不一样，**到底是用户手改了，
    还是只是启动器的另一个开关块还挂在里面**——这两件事的提示语气完全不同。
    """
    return "\n".join(strip_launcher_blocks(text.splitlines())).strip("\n")


def user_disabled_ids(text: str) -> set[str]:
    """用户层里已经 `disabled: true` 的行 id（**不含**启动器写的两个块）。"""
    lines = strip_launcher_blocks(text.splitlines())
    ids: set[str] = set()
    current: str | None = None
    for raw in lines:
        if raw.strip().startswith("#"):
            continue
        match = PATCH_ID_RE.match(raw.split(" #", 1)[0].rstrip())
        if match:
            current = match.group(1)
            continue
        off = DUMP_DISABLED_RE.match(raw)
        if off and current and off.group(1).strip().lower() in ("true", "yes", "1"):
            ids.add(current)
            current = None
    return ids


# ── 写补丁层：照插件市场 dshmarket 的写法（lib/patch.js）────────────────────
#
# 为什么不用 YAML 库：本启动器只依赖标准库；而插件市场自己也是**逐行扫描**这份文件的
# （它同样不解析 YAML，因为用户手写的补丁可能带它不认的结构）。所以"市场能不能读懂我写的
# 开关"这件事，唯一的判据就是市场那套行规则：
#
#     - id: <行 id>            ← 必须**整行就这些**（行尾跟 `# 注释` 市场就看不见）
#       disabled: true|false
#
# 实测（拿市场自己的 readUserPatchState 读我们写的文件）：旧格式把包名写成行尾注释
# `- id: browser   # dsh-plugin-browser`，市场读到的是 **0 条禁用**——也就是说
# 启动器关掉的插件，在插件市场页面上显示的还是"启用中"。两个工具看同一份文件却各说各话。
#
# 还有三个"会把 profile 写坏"的形状，市场都挡掉了，这里同样挡：
#   1. 模板自带的 `[]` 占位符——在它后面追加条目 = 一份文档里两个顶层元素，YAML 直接报错；
#   2. 删掉最后一条后只剩注释——那不再是顶层数组，dsh 拒绝启动这个 profile；
#   3. 本来就不是条目数组的文件——拒绝写入，别把它弄得更坏。
ROW_ID_STRICT_RE = re.compile(r"^- id: ([A-Za-z0-9_.-]+)\s*$")
ROW_ID_OK_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
DISABLED_LINE_RE = re.compile(r"^ {2}disabled: (true|false)\s*$")
PLACEHOLDER_RE = re.compile(r"^[ \t]*\[[ \t]*\][ \t]*(?:#.*)?$", re.M)
COMMENTED_PLACEHOLDER_RE = re.compile(r"^[ \t]*#[ \t]*\[[ \t]*\][ \t]*$", re.M)

# 宿主基础设施行：关掉它们等于掐掉补丁层自己赖以生效的那条链（定时器 → 热加载 →
# web 服务 → 存储/设置）。这张表照抄 dshmarket 的 PROTECTED_MODULE_PATTERNS——
# 它是从 dsh-plugin-hub 那套插件控制台搬过来的，针对的就是同一个宿主。
PROTECTED_MODULE_PATTERNS = [
    re.compile(r"^cordis:"),
    re.compile(r"^@deepseek-ai/cordis-plugin-"),
    re.compile(r"^@deepseek-ai/dsh-host-"),
    re.compile(r"^@deepseek-ai/dsh-client-modules$"),
    re.compile(r"^@deepseek-ai/dsh-client-connection$"),
    re.compile(r"^@deepseek-ai/dsh-client-hmr$"),
    re.compile(r"^@deepseek-ai/dsh-client-runtime$"),
    re.compile(r"^@deepseek-ai/dsh-client-locale$"),
    re.compile(r"^@deepseek-ai/dsh-client-web"),
    re.compile(r"^@deepseek-ai/dsh-web-frontend$"),
    re.compile(r"^@deepseek-ai/dsh-web-app$"),
    re.compile(r"^@deepseek-ai/dsh-hmr$"),
    re.compile(r"^@deepseek-ai/dsh-settings"),
    re.compile(r"^@deepseek-ai/dsh-credentials"),
    re.compile(r"^@deepseek-ai/dsh-session"),
    re.compile(r"^@deepseek-ai/dsh-storage"),
    re.compile(r"^@deepseek-ai/dsh-typert"),
    re.compile(r"^@deepseek-ai/dsh-api-remotes$"),
    re.compile(r"^@deepseek-ai/dsh-tools$"),
    re.compile(r"^@deepseek-ai/dsh-system-prompt$"),
    re.compile(r"^@deepseek-ai/dsh-agent"),
    re.compile(r"^@deepseek-ai/dsh-llm"),
    re.compile(r"^@deepseek-ai/dsh-persona$"),
    re.compile(r"^@deepseek-ai/dsh-scope$"),
    re.compile(r"^@deepseek-ai/dsh-launch-environment$"),
    re.compile(r"^@deepseek-ai/dsh-shell$"),
    re.compile(r"^@deepseek-ai/dsh-subprocess"),
    re.compile(r"^@deepseek-ai/dsh-fs"),
    re.compile(r"^@deepseek-ai/dsh-sandbox"),
    re.compile(r"^@deepseek-ai/dsh-jobs"),
    re.compile(r"^@deepseek-ai/dsh-skill"),
    re.compile(r"^@deepseek-ai/dsh-goal"),
    re.compile(r"^@deepseek-ai/dsh-workflow"),
    re.compile(r"^@deepseek-ai/dsh-subagent"),
    re.compile(r"^@deepseek-ai/dsh-web$"),
    re.compile(r"^@deepseek-ai/dsh-workspace"),
    re.compile(r"^@deepseek-ai/dsh-user-approval$"),
    re.compile(r"^@deepseek-ai/dsh-user-questions$"),
    re.compile(r"^@deepseek-ai/dsh-commands$"),
    re.compile(r"^@deepseek-ai/dsh-hook"),
    re.compile(r"^@deepseek-ai/dsh-spill"),
    re.compile(r"^@deepseek-ai/dsh-guard"),
    re.compile(r"^@deepseek-ai/dsh-tool-call-timeout-policy$"),
    re.compile(r"^@deepseek-ai/dsh-repeat-tool-reminder$"),
]


def is_protected_module(name: str | None) -> bool:
    return bool(name) and any(p.search(str(name)) for p in PROTECTED_MODULE_PATTERNS)


def patch_content_lines(text: str) -> list[str]:
    """去掉注释与空行后的内容行（判 YAML 形状用）。"""
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


def patch_row_state(text: str) -> dict:
    """按**插件市场的行规则**读这份补丁：禁用 / 强制打开 / 插入了哪些行 id。

    行规则比我们自己的更严（行尾不能有注释）——这是故意的：市场读不出来的写法，
    在市场页面上就等于没关。所以这份函数的结论才是"市场看到的真相"。
    """
    disables: list[str] = []
    forced: list[str] = []
    inserts: list[str] = []
    lines = text.splitlines()
    in_insert = False
    for index, line in enumerate(lines):
        if re.match(r"^- insert:\s*$", line):
            in_insert = True
            continue
        if re.match(r"^- ", line):
            in_insert = False
        if in_insert:
            match = re.match(r"^ {4}- id: ([A-Za-z0-9_.-]+)", line)
            if match:
                inserts.append(match.group(1))
            continue
        match = ROW_ID_STRICT_RE.match(line)
        if not match:
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        value = DISABLED_LINE_RE.match(nxt)
        if value is None:
            continue
        (disables if value.group(1) == "true" else forced).append(match.group(1))
    return {"disables": disables, "forced": forced, "inserts": inserts}


def patch_dialect_ok(text: str) -> tuple[bool, str]:
    """这份补丁能不能**安全地追加一个顶层条目**。挡掉三类会写坏 profile 的形状。"""
    content = patch_content_lines(text)
    if not content:
        return True, ""
    core = "\n".join(content).strip()
    if core in ("[]", "[ ]"):
        return True, ""
    last = content[-1].strip()
    if last.startswith("[") or last.startswith("{"):
        return False, "补丁层以顶层流式结构结尾，追加进去会变成两个顶层元素（YAML 直接报错）"
    bad = [line for line in content if not line.startswith(("-", " "))]
    if bad:
        return False, f"补丁层顶层出现了不是条目的内容：`{bad[0].strip()[:40]}`"
    if not content[0].lstrip().startswith("-"):
        return False, "补丁层不是顶层数组"
    return True, ""


# ======================= 补丁层体检（verify 用） =======================
#
# 2026-09-27 那次把用户日常那套写坏（`YAMLException: duplicated mapping key (13:3)`，
# 整个 profile 拒绝启动）之后加的防线。核心教训是**格式正确性不能自证**：
# 我们自己的形状检查（`patch_dialect_ok`）对那份坏文件判"通过"，只有别人的解析器
# 才判 REJECTED。所以这里的第一条判据就是"拿宿主自己的解析器真解析一遍"。
#
# 两条判据：
#   1. **宿主判据**：用 dsh 自己那份 js-yaml（带 `!!js` 方言，与 dsh-app-boot 的
#      entryListSchema、插件市场 lib/check.js 同一套）解析；报错原话（含 行:列）直接给用户。
#      node 或 js-yaml 找不到时降级为"跳过"，绝不假装通过。
#   2. **市场判据**：插件市场是**逐行扫**这份文件的（`readUserPatchState`），
#      行尾带 `# 注释` 或引号的 `- id: X` 它读不到 —— 那意味着"看着关了、市场上还开着"。
#
# 只读不写：这里永远不改补丁层（写开关已停用）。
PATCH_LAYER_FILE = "cordis.patch.yml"
MARKET_ID_LINE_RE = re.compile(r"^- id: ([A-Za-z0-9_.-]+)\s*$")
MARKET_LOOSE_ID_RE = re.compile(r"^-\s*id:\s*\S")
MARKET_DISABLED_LINE_RE = re.compile(r"^ {2}disabled: (true|false)\s*$")

# 判决脚本：方言抄自 dsh-app-boot 的 entryListSchema（JSON_SCHEMA + `!!js` 标量类型）。
# 输出一行 JSON 到 stdout，Python 侧只认这一行。
# 走 `node -e`（脚本作为**一个 argv**传进去）而不是落一个临时 .cjs 文件：
# 少一次写盘、不碰临时目录（有些环境对 %TEMP% 是拒绝的），也不受路径转义影响。
# 两个路径走环境变量，免去对 `process.argv` 偏移的假设。
YAML_JUDGE_JS = r"""
const fs = require('fs');
const yaml = require(process.env.DSH_LANES_JUDGE_YAML);
const jsExpr = new yaml.Type('tag:yaml.org,2002:js', {
  kind: 'scalar',
  resolve: (data) => typeof data === 'string',
  construct: (data) => ({ __jsExpr: String(data) }),
});
const schema = yaml.JSON_SCHEMA.extend(jsExpr);
const answer = (payload) => { process.stdout.write(JSON.stringify(payload)); };
let text;
try {
  text = fs.readFileSync(process.env.DSH_LANES_JUDGE_FILE, 'utf8');
} catch (err) {
  answer({ ok: false, why: '读不了文件：' + err.message });
  process.exit(0);
}
try {
  const value = yaml.load(text, { schema });
  if (!Array.isArray(value)) {
    answer({
      ok: false,
      why: value === null || value === undefined
        ? '整份文件没有内容（只剩注释或空行）—— 它不再是条目数组，dsh 会拒绝启动这个 profile'
        : '顶层不是条目数组',
    });
  } else {
    answer({ ok: true, entries: value.length });
  }
} catch (err) {
  answer({ ok: false, why: String((err && err.message) || err).split('\n')[0] });
}
"""


def find_js_yaml(home, pkg_dir, install_dir=None) -> str | None:
    """找**宿主自己那份** js-yaml —— dsh 读补丁层用的就是它。

    三种装法的位置不一样（实测）：
      · npm 全局装：`<...>/node_modules/@deepseek-ai/dsh/node_modules/js-yaml`
      · lane 的 prefix 装：提升到 `<安装树>/node_modules/js-yaml`（dsh 包内**没有**）
      · pnpm 装的 profile：在虚拟 store `<profile>/node_modules/.pnpm/js-yaml@*/...` 里
    """
    pkg = Path(pkg_dir)
    raw = [
        pkg / "node_modules" / "js-yaml",
        pkg.parent / "js-yaml",
        pkg.parent.parent / "js-yaml",
        Path(install_dir) / "node_modules" / "js-yaml" if install_dir else None,
        Path(home) / "profiles" / "node_modules" / "js-yaml",
        profile_dir(home) / "node_modules" / "js-yaml",
        profile_dir(home) / "node_modules" / "dshmarket" / "node_modules" / "js-yaml",
    ]
    for cand in raw:
        if cand is not None and (cand / "package.json").is_file():
            return str(cand)
    for pattern in (
        "profiles/node_modules/.pnpm/js-yaml@*/node_modules/js-yaml",
        "profiles/*/node_modules/.pnpm/js-yaml@*/node_modules/js-yaml",
    ):
        for hit in sorted(Path(home).glob(pattern)):
            if (hit / "package.json").is_file():
                return str(hit)
    return None


def judge_patch_yaml(cfg: dict, home, pkg_dir, path: Path, install_dir=None) -> tuple[str, str]:
    """用宿主自己的解析器真解析一遍补丁层 → ("ok"|"bad"|"skip", 说明)。"""
    node = find_node(cfg)
    if not node:
        return "skip", "本机找不到 node"
    yaml_dir = find_js_yaml(home, pkg_dir, install_dir)
    if not yaml_dir:
        return "skip", "找不到宿主自带的那份 js-yaml"
    env = dict(os.environ)
    env["DSH_LANES_JUDGE_YAML"] = yaml_dir
    env["DSH_LANES_JUDGE_FILE"] = str(path)
    try:
        proc = subprocess.run(
            [node, "-e", YAML_JUDGE_JS],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env=env,
        )
        payload = ""
        for line in reversed((proc.stdout or "").splitlines()):
            if line.strip().startswith("{"):
                payload = line.strip()
                break
        if not payload:
            tail = ((proc.stderr or "").strip().splitlines() or ["(没有任何输出)"])[-1]
            return "skip", f"判决脚本没给出结论（{tail[:120]}）"
        data = json.loads(payload)
    except Exception as exc:  # noqa: BLE001 —— 判不了就说判不了，不假装通过
        return "skip", f"外部判据跑不起来（{type(exc).__name__}: {exc}）"
    if data.get("ok"):
        return "ok", f"宿主自己的 js-yaml 解析通过（{data.get('entries')} 个条目）"
    return "bad", str(data.get("why") or "解析失败")


def market_blind_rows(text: str) -> tuple[list[str], list[str]]:
    """市场（逐行扫）读不到的 `- id:` 行 → (带开关的坏行, 其余读不到的行)。

    市场规则：`^- id: ([A-Za-z0-9_.-]+)\\s*$` + 下一行 `^ {2}disabled: (true|false)$`。
    行尾写 `# 包名`、给 id 加引号、行内多余空格 —— 市场一律读不到。
    """
    toggle_blind: list[str] = []
    other_blind: list[str] = []
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        if not MARKET_LOOSE_ID_RE.match(raw):
            continue
        if MARKET_ID_LINE_RE.match(raw):
            continue
        nxt = lines[index + 1] if index + 1 < len(lines) else ""
        label = f"第 {index + 1} 行 `{raw.strip()[:60]}`"
        if MARKET_DISABLED_LINE_RE.match(nxt):
            toggle_blind.append(label)
        else:
            other_blind.append(label)
    return toggle_blind, other_blind


def report_patch_layer(cfg: dict, home, pkg_dir, path: Path, problems: list[str],
                       install_dir=None) -> None:
    """体检一份补丁层：宿主判据 + 市场判据。只读。"""
    relative = path
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"补丁层读不出来：{relative}（{type(exc).__name__}: {exc}）")
        return
    info(f"  补丁层    : {relative}")
    info(col(f"              {len(text.splitlines())} 行 / {len(text.encode('utf-8'))} 字节", C.GRAY))
    verdict, why = judge_patch_yaml(cfg, home, pkg_dir, path, install_dir)
    if verdict == "ok":
        ok(f"  {why}")
    elif verdict == "bad":
        problems.append(
            f"补丁层 {relative} 解析不过：{why} —— 宿主会**拒绝启动**这个 profile。"
            "（改法：条目是一个缩进树，不要按固定行数删；只就地改 `disabled:` 这一个键，"
            "或直接删整个条目 = id 行 + 它下面缩进更深的所有行）"
        )
    else:
        info(col(f"  {why} —— 这次只做了本启动器的形状检查（不够，见 README）", C.GRAY))

    toggle_blind, other_blind = market_blind_rows(text)
    if toggle_blind:
        problems.append(
            f"补丁层 {relative} 里有 {len(toggle_blind)} 行的开关**插件市场读不到**"
            f"（行尾带了注释或引号）：{'；'.join(toggle_blind[:3])} —— "
            "在「插件市场」页面上这些行会显示成没被改过"
        )
    if other_blind:
        warn(f"有 {len(other_blind)} 行 `- id:` 市场读不到（它们没带开关，只影响市场页面的显示）："
             f"{'；'.join(other_blind[:3])}")
    if not (toggle_blind or other_blind):
        ok("  市场能读懂这份补丁层的每一行（`- id: X` 整行只有 id）")


def append_patch_entries(text: str, block: list[str]) -> tuple[bool, str, str]:
    """把若干 `- id: X` + `disabled:` 条目追加到补丁层末尾（安全处理 `[]` 占位符）。"""
    ok_dialect, why = patch_dialect_ok(text)
    if not ok_dialect:
        return False, why, text
    core = "\n".join(patch_content_lines(text)).strip()
    head = text
    if core in ("[]", "[ ]"):
        # 模板自带的空列表占位符：注释掉它再追加，否则一份文档里会出现两个顶层元素
        head = PLACEHOLDER_RE.sub("# []", text, count=1)
        if head == text:
            head = text.rstrip("\n") + "\n# []\n"
    if head and not head.endswith("\n"):
        head += "\n"
    return True, "", head + "\n".join(block) + "\n"


def remove_patch_row_entries(text: str, ids: list[str]) -> str:
    """删掉这些行 id 的**整个条目**：`- id: X` 那行 + 它下面缩进更深的行。

    ⚠️ 2026-09-27 的教训（用户日常那套被写坏、profile 起不来）：以前只按"两行"删
    （`- id: X` + `  disabled: ...`），而一个条目可以带 `config:` 这类后续行。只删两行，
    剩下的 `config:` 就落进了**上一条目**里，同一个映射里于是出现两个 `config:` 键：

        Error: dsh: failed to parse overlay ...cordis.patch.yml:
        YAMLException: duplicated mapping key (13:3)

    整个 profile 因此拒绝加载。我们自己的方言检查看不出来（它只判"是不是条目列表"），
    而插件市场自己的 `parsePatchFile()` 一读就判 REJECTED —— 已本地复现。
    """
    targets = set(ids)
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        raw = lines[index]
        match = re.match(r"^-\s+id:\s*['\"]?([A-Za-z0-9_.@/:-]+)", raw)
        if match is None or match.group(1) not in targets:
            out.append(raw)
            index += 1
            continue
        indent = len(raw) - len(raw.lstrip())
        index += 1
        while index < len(lines):
            nxt = lines[index]
            if nxt.strip() == "":
                look = index + 1
                while look < len(lines) and lines[look].strip() == "":
                    look += 1
                if look < len(lines) and (len(lines[look]) - len(lines[look].lstrip())) > indent:
                    index += 1
                    continue
                break
            if not nxt.lstrip().startswith("#") and (len(nxt) - len(nxt.lstrip())) > indent:
                index += 1
                continue
            break
    joined = "\n".join(out)
    return joined + ("\n" if text.endswith("\n") and joined else "")


def ensure_patch_array(text: str) -> str:
    """删完条目后如果只剩注释 —— 那不再是顶层数组，dsh 会拒绝启动整个 profile。补回 `[]`。"""
    if not text.strip():
        return "[]\n"
    if patch_content_lines(text):
        return text
    restored = COMMENTED_PLACEHOLDER_RE.sub("[]", text, count=1)
    if restored != text:
        return restored
    return text.rstrip("\n") + ("\n" if text.endswith("\n") else "\n") + "[]\n"


def set_patch_row(text: str, row_id: str, disabled: bool) -> tuple[bool, str, str]:
    """把一行的开关写成一条 `disabled: true|false`（先删同 id 的旧条目，保证只有一条）。"""
    if not ROW_ID_OK_RE.match(row_id):
        return False, f"行 id「{row_id}」含特殊字符，不能写进补丁层", text
    base = remove_patch_row_entries(text, [row_id])
    block = [f"- id: {row_id}", f"  disabled: {'true' if disabled else 'false'}"]
    return append_patch_entries(base, block)


def enable_patch_row(text: str, row_id: str, force: bool = False) -> tuple[bool, str, str, str]:
    """打开一行。返回 (ok, 说明, 新文本, 动作)。

    动作分三种，这个区分很重要：
    · removed —— 补丁层里本来有 `disabled: true`，删掉它就回到"下层说了算"，这才是"打开"；
    · forced  —— 补丁层里没有禁用条目，说明是**组合包自己的补丁**压着它。这时只有写
                 `disabled: false` 能强行打开（插件市场就是这么做的）。它可能带来副作用
                 （比如某行被压着正是因为"已经有别的插件提供了同一个服务"），所以默认不写，
                 要用户明确加 --force。
    · nothing —— 本来就没被禁用，一个字都不用写。
    """
    if not ROW_ID_OK_RE.match(row_id):
        return False, f"行 id「{row_id}」含特殊字符，不能写进补丁层", text, "nothing"
    had = len(re.findall(
        rf"(?m)^- id: ['\"]?{re.escape(row_id)}['\"]?\r?\n  disabled: true\r?\n", text))
    if had:
        # 删掉最后一条之后可能只剩注释：那份文件不再是顶层数组，**dsh 会拒绝启动这个 profile**
        #（市场源码里的 withPlaceholderRestored 讲的就是这件事：关一个插件再打开，profile 就废了）。
        # 实测：plugin-on 之后补丁层变成"只有注释"，紧接着 --verify 的离线组合直接失败。
        return True, "", ensure_patch_array(remove_patch_row_entries(text, [row_id])), "removed"
    if not force:
        return True, "本来就没被禁用", text, "nothing"
    ok_row, why, new_text = set_patch_row(text, row_id, False)
    return ok_row, why, new_text, "forced"


def write_patch_text(path: Path, text: str) -> tuple[bool, str]:
    """原子写：先写临时文件再替换（运行中的 DSH 正盯着这个文件做热重载）。"""
    tmp = path.with_name(path.name + ".dsh-lanes.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return False, str(exc)
    return True, ""


def backup_patch_text(path: Path, text: str) -> tuple[bool, str]:
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        backup.write_text(text, encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    return True, str(backup)


def profile_dependencies(home, profile: str = PROFILE_NAME) -> dict:
    manifest = profile_dir(home, profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return {}
    deps = data.get("dependencies") or {}
    return {k: str(v) for k, v in deps.items()}


def package_manifest(home, pkg: str, profile: str = PROFILE_NAME) -> dict:
    path = profile_dir(home, profile) / "node_modules" / pkg / "package.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def package_kind(home, pkg: str, rows: list[str], profile: str = PROFILE_NAME) -> str:
    """这个插件是哪种形态 —— 决定它的开关该写在哪一层。

    · bundle    ：有补丁行 → 开关写补丁层（热生效，不用重启）。
    · client-only：只有 `dsh.client` 没有 `dsh.bundle` → **补丁层没有它的行**，
                   它的开关在插件市场自己的禁用表里（市场关掉它，或直接卸载）。
    · norows    ：两者都没有 → 没有任何行可开关。
    """
    if rows:
        return "bundle"
    manifest = package_manifest(home, pkg, profile)
    dsh = manifest.get("dsh") or {}
    if dsh.get("client") and not dsh.get("bundle"):
        return "client-only"
    return "norows"


def lane_skipped_bundles(cfg: dict, lane: str) -> list[str]:
    """从这条 lane 的启动日志里捡出"被跳过的组合包"（例如版本不兼容）。"""
    log = paths(cfg)["logs"] / f"{lane}.log"
    if not log.is_file():
        return []
    out: list[str] = []
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for match in SKIPPED_RE.finditer(text):
        name = match.group(1)
        if name not in out:
            out.append(name)
    return out


def plugin_listing(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """**文件真值**的插件清单：不跑 `--dump-config`，所以是即时的（毫秒级）。

    开关要的是"补丁层现在怎么说"，那件事只取决于文件；`--dump-config` 是**验证**
    手段（证明组合结果真的变了），不是开关的前置条件。
    """
    home = lane_home(cfg, lane)
    bundles = read_profile_bundles(home, profile)
    deps = profile_dependencies(home, profile)
    skip = lane_skipped_bundles(cfg, lane)
    patch_file = plugin_patch_path(cfg, lane, profile)
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    state = patch_row_state(text)
    launcher_lines = strip_launcher_blocks(text.splitlines())
    launcher_text = "\n".join(launcher_lines)
    launcher_state = patch_row_state(launcher_text)
    disabled = set(state["disables"])
    forced = set(state["forced"])
    own_disabled = set(launcher_state["disables"])

    names: list[str] = []
    for name in list(deps.keys()) + list(bundles):
        if name not in names:
            names.append(name)
    items = []
    for pkg in names:
        official = is_official_bundle(pkg)
        rows = bundle_row_ids(home, pkg, profile)
        kind = package_kind(home, pkg, rows, profile)
        row_state = {}
        for row_id in rows:
            row_state[row_id] = (
                "禁用" if row_id in disabled else ("强制打开" if row_id in forced else "启用")
            )
        off_rows = [r for r in rows if r in disabled]
        items.append({
            "pkg": pkg,
            "spec": deps.get(pkg, ""),
            "official": official,
            "in_bundles": pkg in bundles,
            "rows": rows,
            "kind": kind,
            "row_state": row_state,
            "disabled_rows": off_rows,
            "forced_rows": [r for r in rows if r in forced],
            "own_disabled": sorted(set(rows) & own_disabled),
            "protected": is_protected_module(pkg),
            "skipped": pkg in skip,
            "installed": (profile_dir(home, profile) / "node_modules" / pkg).is_dir(),
        })
    return {
        "lane": lane,
        "home": home,
        "profile": profile,
        "patch_file": patch_file,
        "patch_text": text,
        "patch_state": state,
        "has_block": bool(find_block(text.splitlines())),
        "has_market_block": bool(find_block(text.splitlines(), MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)),
        "bundles": items,
        "skipped": skip,
        "dialect_ok": patch_dialect_ok(text)[0],
        "dialect_why": patch_dialect_ok(text)[1],
        # 文件存在但内容行是空的（只剩注释）→ 不是顶层数组，dsh 会拒绝启动这条 lane
        "array_ok": (not patch_file.is_file()) or bool(patch_content_lines(text)),
    }


def resolve_plugin_target(listing: dict, target: str) -> tuple[dict | None, str]:
    """把用户写的名字认成一个插件：包名、行 id、包名的一部分都行。"""
    target = (target or "").strip()
    if not target:
        return None, "没写插件名"
    exact = [i for i in listing["bundles"] if i["pkg"] == target]
    if exact:
        return exact[0], ""
    by_row = [i for i in listing["bundles"] if target in i["rows"]]
    if by_row:
        return by_row[0], ""
    lower = target.lower()
    partial = [
        i for i in listing["bundles"]
        if lower in i["pkg"].lower() or any(lower in r.lower() for r in i["rows"])
    ]
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, "这个名字对上多个插件：" + "、".join(i["pkg"] for i in partial[:6])
    return None, f"没找到叫「{target}」的插件（用 plugins {listing['lane']} 看都有哪些）"


def apply_plugin_states(cfg: dict, lane: str, changes: list[tuple[str, bool]],
                        profile: str = PROFILE_NAME, force: bool = False) -> dict:
    """按**行**写入插件开关（一次写完，只做一次读-改-写）。

    changes: [(包名, True=打开 / False=关掉)]。返回值里逐条说清楚写了什么、跳过了什么。
    关掉 = 写一条 `- id: <行 id>` + `disabled: true`（插件市场读得懂的同一套行格式）；
    打开 = 删掉那条禁用条目；只有加 force 才会写 `disabled: false` 去压组合包自己的禁用。
    """
    home = lane_home(cfg, lane)
    patch_file = plugin_patch_path(cfg, lane, profile)
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    original = text
    written: list[tuple[str, str, str]] = []
    skipped: list[str] = []
    for pkg, want_on in changes:
        rows = bundle_row_ids(home, pkg, profile)
        if is_protected_module(pkg):
            skipped.append(f"{pkg}：属于宿主基础设施，禁止开关（会破坏热加载/传输/存储链）")
            continue
        if not rows:
            kind = package_kind(home, pkg, rows, profile)
            if kind == "client-only":
                skipped.append(
                    f"{pkg}：纯客户端插件（只有 dsh.client、没有 dsh.bundle）——补丁层里没有它的行，"
                    "开关在插件市场自己的页面里"
                )
            else:
                skipped.append(f"{pkg}：找不到属于它的补丁行，无法用补丁层开关")
            continue
        for row_id in rows:
            if not want_on:
                ok_row, why, text = set_patch_row(text, row_id, True)
                action = "禁用"
            else:
                ok_row, why, text, act = enable_patch_row(text, row_id, force=force)
                action = {"removed": "打开（删掉禁用条目）", "forced": "强制打开（写 disabled: false）",
                          "nothing": "打开（本来就没被禁用，没动文件）"}.get(act, "打开")
            if not ok_row:
                skipped.append(f"{pkg}（行 {row_id}）：{why}")
                continue
            written.append((pkg, row_id, action))
    if text == original:
        # 没有条目变化，但文件本身可能已经坏了（只剩注释 = 不是顶层数组，dsh 拒绝启动）。
        # 这种"上一版留下的坏文件"必须顺手治好，否则用户再点一次开关也修不回来。
        healed = ensure_patch_array(text)
        if healed == text:
            return {"ok": True, "changed": False, "written": written, "skipped": skipped,
                    "patch_file": patch_file}
        text = healed
        skipped.append("补丁层原本只剩注释（不是顶层数组，dsh 会拒绝启动这条 lane）——已补回 `[]`")
    # 兜底：不论走了哪条分支，写出去的文件都必须是"顶层数组"（只剩注释就补回 `[]`）
    text = ensure_patch_array(text)
    backup_ok, backup_info = backup_patch_text(patch_file, original)
    if not backup_ok:
        return {"ok": False, "changed": False, "error": f"备份失败，没敢写：{backup_info}",
                "written": [], "skipped": skipped, "patch_file": patch_file}
    ok_write, why_write = write_patch_text(patch_file, text)
    if not ok_write:
        return {"ok": False, "changed": False, "error": f"写入失败：{why_write}",
                "written": [], "skipped": skipped, "patch_file": patch_file}
    return {"ok": True, "changed": True, "written": written, "skipped": skipped,
            "patch_file": patch_file, "backup": backup_info, "text": text}



def plugin_overview(cfg: dict, lane: str, profile: str = PROFILE_NAME,
                    dump: dict | None = None) -> dict:
    """一眼看清这条 lane 的插件格局：组合包 → 行 id → 现在到底关没关。"""
    data = cfg["lanes"][lane]
    home = Path(data.get("home") or (paths(cfg)["homes"] / lane))
    bundles = read_profile_bundles(home, profile)
    dump = dump if dump is not None else run_dump_config(cfg, lane, profile)
    rows = dump.get("rows") or {}
    patch_file = plugin_patch_path(cfg, lane, profile)
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    span = find_block(text.splitlines())
    items = []
    for pkg in bundles:
        ids = bundle_row_ids(home, pkg, profile)
        present = [i for i in ids if i in rows]
        off = [i for i in present if rows[i]["disabled"]]
        items.append(
            {
                "pkg": pkg,
                "official": is_official_bundle(pkg),
                "rows": ids,
                "present": present,
                "disabled": off,
                "managed": bool(span) and any(i in text for i in ids) if ids else False,
            }
        )
    return {
        "home": home,
        "profile": profile,
        "patch_file": patch_file,
        "bundles": items,
        "has_block": bool(span),
        "user_disabled": user_disabled_ids(text),
        "unmatched": dump.get("unmatched") or [],
        "skipped": dump.get("skipped") or [],
        "dump_ok": bool(dump.get("ok")),
        "dump_error": dump.get("error"),
    }


def cmd_plugins_write_disabled(cfg: dict, args) -> int:
    """写补丁层来开关插件 —— 这个功能**已停用**（2026-09-27）。

    为什么停用：用户日常那套 profile 的补丁层被写坏过一次，直接起不来：
        YAMLException: duplicated mapping key (13:3)
    根因是"按两行删条目"，而条目可以带 `config:` 后续行，剩下的部分落进上一条目形成重复键
    （已本地复现，插件市场自己的 `parsePatchFile()` 判 REJECTED）。修好删除逻辑只是止血——
    这条路还依赖"行 id 归属""宿主保护名单""纯客户端插件管不了"等一堆判断，风险与收益不成比例。
    所以**写开关一律停用**，只保留只读清单；真要开关插件，用 DSH 自己的插件市场页面。
    """
    lane = getattr(args, "lane", "") or "<lane>"
    me = ME_NAME
    warn("「用启动器写补丁层来开关插件」已经停用（2026-09-27）。")
    info(col("  原因：2026-09-27 11:00 那次开关把 profile 的补丁层写坏了，DSH 直接拒绝启动该 profile：", C.GRAY))
    info(col("        YAMLException: duplicated mapping key (13:3)", C.GRAY))
    info(col("  根因：条目不只两行（`- id: X` + `disabled:`），还可能带 `config:` 等后续行；", C.GRAY))
    info(col("        只删前两行，剩下的 `config:` 会落进上一条目 → 同一个映射出现两个 config: 键。", C.GRAY))
    info("")
    info(col(f"  只想看状态（只读，安全）： py {me} plugins {lane}", C.CYAN))
    info(col("  真要开关插件：用 DSH 自己的插件市场页面（它有自己的写法与保护）。", C.CYAN))
    return 1


def cmd_plugins(cfg: dict, args) -> int:
    """列出这条 lane 的插件格局：插件 → 真实行 id → 现在关没关，**谁能关它**。

    默认**不跑组合**（毫秒级返回）：开关要的"补丁层现在怎么说"只取决于文件。
    加 `--dump` 才额外离线组合一次，用来核对"文件说的"和"组合结果"是不是一致。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    listing = plugin_listing(cfg, lane)
    me = ME_NAME
    info(col(f"── lane「{lane}」的插件开关（profile {listing['profile']}）──　"
             "（只读；写开关已停用，见 README）", C.BOLD))
    info(f"  DSH_HOME : {listing['home']}")
    info(f"  补丁层   : {listing['patch_file']}"
         + ("（有一键禁用块）" if listing["has_block"] else "")
         + ("（有插件市场开关块）" if listing["has_market_block"] else ""))
    if not listing["dialect_ok"]:
        warn(f"  这份补丁层现在追加不了条目：{listing['dialect_why']}")
    if not listing["array_ok"]:
        warn("  这份补丁层只剩注释 —— 不是顶层数组，dsh 会**拒绝启动**这条 lane；"
             "随便开关一次插件就会自动补回 `[]`")
    info("")
    third_party = [i for i in listing["bundles"] if not i["official"]]
    if not third_party:
        info(col("  这条 lane 没有第三方插件（只有官方组合包），没有可开关的东西", C.GRAY))
    for item in listing["bundles"]:
        if item["official"]:
            continue
        if item["disabled_rows"] and len(item["disabled_rows"]) == len(item["rows"]):
            state, mark = col("已禁用", C.GREEN), "×"
        elif item["disabled_rows"]:
            state, mark = col(f"部分禁用 {len(item['disabled_rows'])}/{len(item['rows'])}", C.YELLOW), "~"
        else:
            state, mark = "启用中", "√"
        if item["kind"] == "bundle":
            where = "有补丁行 → 到「插件市场」页面里开关"
        elif item["kind"] == "client-only":
            where = col("纯客户端：补丁层没有它的行，去「插件市场…」里关", C.YELLOW)
        else:
            where = col("没有可开关的行", C.GRAY)
        if item["skipped"]:
            where = col("启动时被跳过（版本不兼容）", C.RED)
        info(f"  [{mark}] {item['pkg']}   {state}")
        info(col(f"        行 id：{'、'.join(item['rows']) or '（无）'}　→　{where}", C.GRAY))
        if item["forced_rows"]:
            info(col(f"        其中 {len(item['forced_rows'])} 行是强制打开（disabled: false）："
                     f"{'、'.join(item['forced_rows'])}", C.GRAY))
    official = [i["pkg"] for i in listing["bundles"] if i["official"]]
    client_only = [i["pkg"] for i in listing["bundles"] if not i["official"] and i["kind"] == "client-only"]
    norows = [i["pkg"] for i in listing["bundles"] if not i["official"] and i["kind"] == "norows"]
    info("")
    info(col(f"  官方组合包（不会被一键禁用动）：{'、'.join(official) or '无'}", C.GRAY))
    if client_only:
        info(col(f"  纯客户端插件（补丁层关不掉，要去插件市场里关）：{'、'.join(client_only)}", C.GRAY))
    if norows:
        info(col(f"  没有补丁行、也没有客户端部分：{'、'.join(norows)}", C.GRAY))
    if listing["skipped"]:
        warn(f"启动时被跳过的组合包：{'、'.join(listing['skipped'])}")
    malformed = [row for row in listing["patch_state"]["disables"] if row not in
                 {r for i in listing["bundles"] for r in i["rows"]}]
    if malformed:
        warn(f"补丁层里有 {len(malformed)} 条禁用行没对上任何已装插件（写错了或目标已移除），"
             "这类覆盖**不会报错**，只是静默失效：")
        info(col("      " + "、".join(malformed), C.GRAY))
    info("")
    info(col(f"  只读清单（写开关已停用）： py {me} plugins {lane}", C.CYAN))
    info(col("  要开关插件：用 DSH 自己的插件市场页面", C.GRAY))
    if getattr(args, "dump", False):
        info("")
        info(col("  离线组合一次，核对文件与组合结果是否一致…", C.GRAY))
        dump = run_dump_config(cfg, lane)
        if not dump.get("ok"):
            fail(f"组合检查失败（{dump.get('error') or '退出码非 0'}）")
            return 1
        rows = dump["rows"]
        problems = 0
        for item in listing["bundles"]:
            if item["official"] or item["kind"] != "bundle":
                continue
            live = [r for r in item["rows"] if r in rows]
            if not live:
                warn(f"{item['pkg']}：补丁层写了 {len(item['rows'])} 行，组合结果里一行都没有"
                     "（被跳过/不兼容/行 id 不对）")
                problems += 1
                continue
            for row_id in live:
                composed_off = bool(rows[row_id].get("disabled"))
                layer_off = row_id in set(listing["patch_state"]["disables"])
                if composed_off != layer_off:
                    warn(f"{item['pkg']} 行 {row_id}：补丁层说 {'禁用' if layer_off else '启用'}，"
                         f"组合结果说 {'禁用' if composed_off else '启用'}（下层还有别的补丁在压它）")
                    problems += 1
        if dump.get("unmatched"):
            warn(f"组合时 {len(dump['unmatched'])} 条覆盖没匹配上：{'、'.join(dump['unmatched'])}")
        if not problems:
            ok("核对通过：补丁层说的和组合结果一致")
    return 0


def cmd_plugin_switch(cfg: dict, args, want_on: bool) -> int:
    """单插件开关：`plugin-off <lane> <插件名>` / `plugin-on <lane> <插件名> [--force]`。

    写法照插件市场（dshmarket/lib/patch.js）：往用户层补丁写/删一条
    `- id: <真实行 id>` + `disabled: …`，而且**行尾不带任何注释**——市场是逐行扫这份
    文件的，行尾多一个 `# 包名` 它就看不见，页面上会显示成"启用中"。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    listing = plugin_listing(cfg, lane)
    item, why = resolve_plugin_target(listing, args.target)
    if item is None:
        fail(why)
        return 1
    name = item["pkg"]
    me = ME_NAME
    if item["official"]:
        fail(f"{name} 是官方组合包，不归这个开关管（关它等于把 DSH 自己的部件摘掉）")
        return 1
    if item["protected"]:
        fail(f"{name} 属于宿主基础设施，禁止开关（关它会破坏热加载/传输/存储链）")
        return 1
    if not item["rows"]:
        if item["kind"] == "client-only":
            warn(f"{name} 是**纯客户端插件**（只有 dsh.client、没有 dsh.bundle）：")
            info(col("      补丁层里没有它的行，所以写补丁层对它无效——它的开关在插件市场"
                     "自己的页面里（市场会记住关掉的清单并在每次启动时重放）。", C.GRAY))
            info(col(f"      要在这里彻底关掉它，只能卸载： py {me} market … 或 dsh plugin remove", C.GRAY))
        else:
            warn(f"{name} 没有任何补丁行，也没有客户端部分 —— 没有可开关的东西。")
        if item["skipped"]:
            info(col("      （它启动时被跳过了：版本不兼容）", C.GRAY))
        return 1
    running = lane_runtime(cfg, lane)
    changes = [(name, want_on)]
    result = apply_plugin_states(cfg, lane, changes, force=bool(getattr(args, "force", False)))
    info("")
    if not result["ok"]:
        fail(result.get("error") or "写入失败")
        for note in result["skipped"]:
            info(col(f"      {note}", C.GRAY))
        return 1
    for pkg, row_id, action in result["written"]:
        info(f"  {action:<28}{pkg:<30}行 id = {row_id}")
    for note in result["skipped"]:
        info(col(f"      {note}", C.GRAY))
    if not result["changed"]:
        info("")
        if want_on:
            warn("补丁层里没有禁用它的条目，所以文件一个字都没动（它现在就是启用的状态）")
            info(col(f"      如果它在页面上仍是关的，说明是**组合包自己的补丁**压着它；"
                     f"要强行打开： py {me} plugin-on {lane} {name} --force", C.GRAY))
        else:
            info(col("文件没有变化（这些行本来就是禁用的）", C.GRAY))
        return 0
    info("")
    info(col(f"  备份      {result.get('backup')}", C.GRAY))
    if running:
        ok(f"已写入。这条 lane 正在运行：补丁层是 DSH 热重载**正盯着**的文件（@deepseek-ai/dsh-hmr），"
           "不用重启——实测两个方向都是 ~2.3 秒生效。")
    else:
        info(col("这条 lane 没在运行：下次 open 时生效。想先核对组合结果： "
                 f"py {me} plugins {lane} --dump", C.GRAY))
    if getattr(args, "verify", False):
        info("")
        info(col("  离线组合一次核对…", C.GRAY))
        dump = run_dump_config(cfg, lane)
        if not dump.get("ok"):
            warn("组合检查没跑起来，改动是否生效未经验证")
            return 0
        rows_now = dump["rows"]
        bad = [row for _p, row, _a in result["written"]
               if row in rows_now and bool(rows_now[row].get("disabled")) != (not want_on)]
        gone = [row for _p, row, _a in result["written"] if row not in rows_now]
        if gone:
            warn(f"这些行在组合结果里找不到：{'、'.join(gone)}")
        if bad:
            fail(f"这些行的组合状态与刚写的不一致：{'、'.join(bad)}")
        if not gone and not bad:
            ok(f"核对通过：{len(result['written'])} 行的组合状态已随开关改变")
    return 0


def cmd_plugin_off(cfg: dict, args) -> int:
    return cmd_plugin_switch(cfg, args, False)


def cmd_plugin_on(cfg: dict, args) -> int:
    return cmd_plugin_switch(cfg, args, True)


def _collect_targets(cfg, lane, keep, profile, dump_rows) -> tuple[list[tuple[str, str]], list[str]]:
    """算出该禁哪些行：[(组合包, 行 id)]。只保留**确实出现在组合结果里**的行。"""
    view = plugin_overview(cfg, lane, profile, dump={"rows": dump_rows, "ok": True})
    home = lane_home(cfg, lane)
    targets: list[tuple[str, str]] = []
    notes: list[str] = []
    for item in view["bundles"]:
        if item["official"]:
            continue
        if item["pkg"] in keep:
            continue
        if is_protected_module(item["pkg"]):
            notes.append(f"{item['pkg']}：属于宿主基础设施（热加载/传输/存储链），禁止开关")
            continue
        if not item["present"]:
            kind = package_kind(home, item["pkg"], item["rows"], profile)
            if kind == "client-only":
                notes.append(f"{item['pkg']}：纯客户端插件（只有 dsh.client）——补丁层里没有它的行，"
                             "要去插件市场自己的页面里关，这里跳过")
            else:
                notes.append(f"{item['pkg']}：当前没有生效的行（被跳过或不兼容），跳过")
            continue
        for row_id in item["present"]:
            targets.append((item["pkg"], row_id))
    return targets, notes


def cmd_plugins_off(cfg: dict, args) -> int:
    """一键禁用**自己装的**（第三方）插件；官方组合包不动。

    只写用户层 `cordis.patch.yml` 里一段带标记的 `disabled: true` 覆盖：
    组合包仍然装着、仍然在 `dsh.profile.bundles` 里，所以撤销就是删掉这段（`plugins-on`）。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    keep = set()
    for item in getattr(args, "keep", None) or []:
        keep.update(x.strip() for x in str(item).split(",") if x.strip())

    patch_file = plugin_patch_path(cfg, lane)
    if not patch_file.is_file():
        fail(f"找不到用户层补丁文件：{patch_file}")
        return 1
    original = patch_file.read_text(encoding="utf-8")

    # 插件市场有自己的单独开关。它已经被那个开关关掉时，这里就别再写一遍
    #（同一行写两次覆盖不会报错，但配置会变得难读，而且两个块的撤销会互相踩）。
    market_note = ""
    if find_block(original.splitlines(), MARKET_BLOCK_BEGIN, MARKET_BLOCK_END) and MARKET_PKG not in keep:
        keep.add(MARKET_PKG)
        market_note = (
            f"{MARKET_PKG}：插件市场已经由它的单独开关关着，这里跳过"
            "（要连市场一起恢复，先 market-on）"
        )

    if lane_runtime(cfg, lane):
        info(col(f"lane「{lane}」正在运行：补丁层是 DSH 热重载盯着的文件，写进去大约 2 秒生效"
                 "（实测 2.3 秒），不用重启；只有启动冒烟跑不了——那条要停一次。", C.GRAY))

    info(col(f"── 一键禁用第三方插件：lane「{lane}」──", C.BOLD))
    info(col("  正在离线组合一次 profile（dsh --dump-config）拿真实行清单…", C.GRAY))
    dump = run_dump_config(cfg, lane)
    if not dump.get("ok"):
        fail(f"组合检查失败（{dump.get('error') or '退出码 ' + str(dump.get('rc'))}），为了不瞎写就不继续了")
        if dump.get("stderr"):
            for line in str(dump["stderr"]).splitlines()[:6]:
                info(col(f"      {line}", C.GRAY))
        return 1
    rows = dump["rows"]
    targets, notes = _collect_targets(cfg, lane, keep, PROFILE_NAME, rows)
    if market_note:
        notes.append(market_note)
    if not targets:
        warn("没有可禁用的第三方插件行")
        for note in notes:
            info(col(f"      {note}", C.GRAY))
        return 0

    already = user_disabled_ids(original)
    to_write: list[tuple[str, str]] = []
    for pkg, row_id in targets:
        if row_id in already:
            notes.append(f"{row_id}：你自己的配置里已经禁用，不动它（撤销后仍会是关的）")
            continue
        to_write.append((pkg, row_id))
    if not to_write:
        warn("目标行都已经处于禁用状态，没有需要写的")
        for note in notes:
            info(col(f"      {note}", C.GRAY))
        return 0

    backup = patch_file.with_suffix(patch_file.suffix + ".bak")
    try:
        backup.write_text(original, encoding="utf-8")
    except OSError as exc:
        fail(f"备份失败，放弃写入：{exc}")
        return 1

    # 头部**原样保留**（一字不改），禁用块追加在后面；撤销时按标记整段删回去。
    # 只摘掉启动器自己那段（保留它后面可能存在的"插件市场开关"块），不能像以前那样
    # `lines[:span[0]]` 一刀切——那样会把排在这段后面的市场开关块整段丢掉。
    # 行格式照插件市场的规矩（包名独立注释行，`- id:` 整行只有 id），并且用
    # append_patch_entries 处理掉 `[]` 占位符——不然模板 profile 上会写成两个顶层元素。
    lines = original.splitlines()
    span = find_block(lines)
    kept = lines[: span[0]] + lines[span[1] + 1:] if span else lines
    base = "\n".join(kept)
    if base.strip() and not base.endswith("\n"):
        base += "\n"
    base = remove_patch_row_entries(base, sorted({row_id for _pkg, row_id in to_write}))
    block = [BLOCK_BEGIN]
    for pkg, row_id in sorted(set(to_write)):
        block.append(f"# {pkg}")
        block.append(f"- id: {row_id}")
        block.append("  disabled: true")
    block.append(BLOCK_END)
    ok_append, why_append, new_text = append_patch_entries(base, block)
    if not ok_append:
        fail(f"补丁层现在追加不了条目，没敢写：{why_append}")
        return 1
    write_ok, why_write = write_patch_text(patch_file, new_text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1

    info("")
    for pkg, row_id in sorted(set(to_write)):
        info(f"  已禁用    {pkg:<34}行 id = {row_id}")
    for note in notes:
        info(col(f"      {note}", C.GRAY))
    info("")
    info(col(f"  备份      {backup}", C.GRAY))

    if getattr(args, "no_verify", False):
        return 0
    info(col("  再组合一次，核对禁用到没到生效…", C.GRAY))
    after = run_dump_config(cfg, lane)
    if not after.get("ok"):
        warn("复核没能跑起来，禁用的是否生效**未经验证**，建议手动打开这条 lane 看一眼")
        return 0
    rows_after = after["rows"]
    bad = [row_id for _pkg, row_id in to_write if not (rows_after.get(row_id) or {}).get("disabled")]
    dead = [row_id for _pkg, row_id in to_write if row_id not in rows_after]
    if dead:
        fail(f"有 {len(dead)} 个行 id 在组合结果里找不到（写错了？）：{'、'.join(sorted(set(dead)))}")
    if bad:
        fail(f"有 {len(bad)} 个行写进去了但仍然是启用状态：{'、'.join(sorted(set(bad)))}")
    still = [
        item["pkg"]
        for item in plugin_overview(cfg, lane, PROFILE_NAME, dump=after)["bundles"]
        if not item["official"] and item["present"] and len(item["disabled"]) != len(item["present"])
    ]
    if not dead and not bad:
        ok(f"核对通过：{len(to_write)} 个行已全部禁用"
           + (f"，仍有启用的第三方组合包：{'、'.join(still)}" if still else "，第三方插件已全部关闭"))
    problems = bool(dead or bad)

    # 启动冒烟：**只有真起来过才算数**。实测踩到：把某个组合包插入的 `web` 行一起禁掉后，
    # 7 个官方行会一直等 `web` 服务、应用根本不出 URL——这种失败在离线组合结果里完全看不出来。
    # 所以这一步是默认开的；起不来就**自动撤销**，不留一个起不来的环境给你。
    if getattr(args, "no_boot_check", False):
        info(col("  （按你的要求跳过了启动冒烟）", C.GRAY))
    elif lane_runtime(cfg, lane):
        info(col("  这条 lane 正在运行：补丁层已经热生效，跳过启动冒烟（那条要停一次才能跑）。"
                 "想连「下次启动能不能起来」一起验：先 stop 再跑一次。", C.GRAY))
    else:
        info(col("  启动冒烟：确认全禁用状态下还能起来…", C.GRAY))
        rc = cmd_open(
            cfg,
            argparse.Namespace(
                target=lane, port=None, cwd=None, timeout=90,
                no_browser=True, detach=True, adopt_only=False,
            ),
        )
        if rc == 0:
            cmd_stop(cfg, argparse.Namespace(target=[lane]))
            ok("启动冒烟通过（已把它停回去）")
        else:
            problems = True
            fail("全禁用状态下**起不来**——已自动撤销回你原来的配置")
            try:
                patch_file.write_text(original, encoding="utf-8")
                ok(f"已还原 {patch_file.name}（备份留在 {backup.name}）")
            except OSError as exc:
                fail(f"自动还原失败，请手动把备份复制回去：{backup} → {patch_file}（{exc}）")
            warn("通常是因为某个组合包**提供的服务**被一起关掉了"
                 "（本机就是 @liustack/modsearch 插入的 `web` 行）。排除它再试：")
            info(col(f"      py {ME_NAME} plugins-off {lane} --keep @liustack/modsearch", C.CYAN))
    info("")
    info(col(f"  撤销： py {ME_NAME} plugins-on {lane}", C.CYAN))
    if lane_runtime(cfg, lane):
        info(col("  生效： 已经热生效了（这条 lane 正在运行，补丁层是热重载盯着的文件）", C.CYAN))
    else:
        info(col(f"  生效： py {ME_NAME} open {lane}（下次启动时应用）", C.CYAN))
    return 1 if problems else 0


def cmd_plugins_on(cfg: dict, args) -> int:
    """撤销一键禁用：只删启动器写的那一段，你自己的配置一个字不动。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    patch_file = plugin_patch_path(cfg, lane)
    if not patch_file.is_file():
        fail(f"找不到用户层补丁文件：{patch_file}")
        return 1
    original = patch_file.read_text(encoding="utf-8")
    lines = original.splitlines()
    span = find_block(lines)
    if not span:
        warn("这段禁用块本来就不存在（没有可撤销的东西）")
        return 0
    kept = lines[: span[0]] + lines[span[1] + 1:]
    # 删完可能只剩注释 —— 那就不是顶层数组了，dsh 拒绝启动这个 profile，补回 `[]`
    text = ensure_patch_array("\n".join(kept))
    if text and not text.endswith("\n"):
        text += "\n"
    # 逐字节还原：如果"手术式删除"的结果与备份只差结尾换行，就直接用备份，保证一字不差
    backup = patch_file.with_suffix(patch_file.suffix + ".bak")
    verdict = ""
    if backup.is_file():
        try:
            bak_text = backup.read_text(encoding="utf-8")
        except OSError:
            bak_text = ""
        if bak_text:
            if text == bak_text:
                verdict = "与备份逐字节一致"
            elif text.rstrip("\n") == bak_text.rstrip("\n"):
                text = bak_text
                verdict = "与备份逐字节一致（按备份补齐了结尾换行）"
            elif user_layer_text(text) == user_layer_text(bak_text):
                verdict = "与备份一致（差异只是启动器写的另一个开关块；你自己的配置没动）"
            else:
                verdict = "与备份不同（你在这期间手改过；备份里是禁用前的原文）"
    write_ok, why_write = write_patch_text(patch_file, text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1
    ok(f"已撤销 lane「{lane}」的一键禁用（删掉 {span[1] - span[0] - 1} 行覆盖，你自己的配置没动）")
    if verdict:
        info(col(f"  校验：{verdict}", C.GRAY))
    if lane_runtime(cfg, lane):
        info(col("这条 lane 正在运行：删掉的那几行会被热重载立刻重放（不用重启）", C.GRAY))
    if not getattr(args, "no_verify", False):
        listing = plugin_listing(cfg, lane)
        enabled = [
            i["pkg"] for i in listing["bundles"]
            if not i["official"] and i["kind"] == "bundle" and not i["disabled_rows"]
        ]
        info(col(f"  当前仍在启用中的第三方插件：{'、'.join(enabled) or '（无）'}", C.GRAY))
        if listing["has_market_block"]:
            info(col(f"  注意：插件市场还挂着自己的开关块（要恢复用 market-on）", C.GRAY))
    return 0


# ======================= 插件市场单独开关 / 更新 =======================


def market_package_dir(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> Path:
    return profile_dir(lane_home(cfg, lane), profile) / "node_modules" / MARKET_PKG


def market_declared_spec(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> str | None:
    manifest = profile_dir(lane_home(cfg, lane), profile) / "package.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = (data.get("dependencies") or {}).get(MARKET_PKG)
    return str(value) if value else None


def market_block_rows(text: str) -> list[str]:
    """插件市场开关块里写了哪些行 id。"""
    lines = text.splitlines()
    span = find_block(lines, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)
    if not span:
        return []
    out: list[str] = []
    for raw in lines[span[0] + 1: span[1]]:
        match = PATCH_ID_RE.match(raw.split(" #", 1)[0].rstrip())
        if match and match.group(1) not in out:
            out.append(match.group(1))
    return out


def _fmt_age(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        stamp = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        delta = datetime.datetime.now(datetime.timezone.utc) - stamp
    except Exception:
        return ""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 0)} 分钟前发布"
    hours = minutes / 60
    if hours < 48:
        return f"{int(hours)} 小时前发布"
    return f"{int(hours // 24)} 天前发布"


def market_snapshots(cfg: dict, lane: str) -> list[dict]:
    root = paths(cfg)["backups"] / "market" / lane
    items: list[dict] = []
    if not root.is_dir():
        return items
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        manifest = path / "manifest.json"
        item = {"name": path.name, "dir": path, "created": "", "version": "", "spec": "",
                "files": 0, "bytes": 0, "ok": manifest.is_file()}
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                for key in ("created", "version", "spec", "files", "bytes"):
                    if key in data:
                        item[key] = data[key]
            except Exception:
                pass
        items.append(item)
    return items


def prune_market_snapshots(cfg: dict, lane: str, keep: int = 5) -> list[str]:
    """只保留最近 N 份插件市场快照。

    每份只有 3.8 MB，但"更新一次存一份"会一直涨；留最近 5 份够回退，
    更老的清掉（只动 `<root>/backups/market/<lane>` 下、带 manifest.json 的目录）。
    """
    root = (paths(cfg)["backups"] / "market" / lane).resolve()
    if not root.is_dir():
        return []
    victims: list[str] = []
    for item in market_snapshots(cfg, lane)[keep:]:
        path = Path(item["dir"]).resolve()
        if path.parent != root or not item["ok"]:
            continue
        try:
            shutil.rmtree(path)
            victims.append(item["name"])
        except OSError:
            pass
    return victims


def market_status(cfg: dict, lane: str, profile: str = PROFILE_NAME,
                  with_registry: bool = False) -> dict:
    """插件市场的现状：装的是哪个版本、行 id 是什么、被谁关着。

    `market` 命令**故意不跑** `--dump-config`（那要十几秒），状态直接读
    profile 的 package.json / cordis.patch.yml 推出来——够快，GUI 打开对话框时不会卡。
    """
    home = lane_home(cfg, lane)
    pdir = profile_dir(home, profile)
    patch_file = pdir / "cordis.patch.yml"
    text = patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
    lines = text.splitlines()
    pkgdir = pdir / "node_modules" / MARKET_PKG
    out = {
        "lane": lane,
        "home": home,
        "profile": profile,
        "profile_dir": pdir,
        "patch_file": patch_file,
        "package_dir": pkgdir,
        "installed": _manifest_version(pkgdir),
        "spec": market_declared_spec(cfg, lane, profile),
        "rows": bundle_row_ids(home, MARKET_PKG, profile),
        "in_bundles": MARKET_PKG in read_profile_bundles(home, profile),
        "has_block": bool(find_block(lines, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)),
        "block_rows": market_block_rows(text),
        "general_block": bool(find_block(lines, BLOCK_BEGIN, BLOCK_END)),
        "user_disabled": user_disabled_ids(text),
        "snapshots": market_snapshots(cfg, lane),
        "latest": None,
        "published": None,
        "registry": None,
        "registry_error": None,
        "versions": [],
    }
    if with_registry:
        try:
            data, reg = fetch_index(cfg, MARKET_PKG)
            tags = data.get("dist-tags") or {}
            out["registry"] = reg
            out["latest"] = tags.get("latest") or None
            out["published"] = (data.get("time") or {}).get(out["latest"] or "")
            out["versions"] = list((data.get("versions") or {}).keys())
        except Exception as exc:  # noqa: BLE001
            out["registry_error"] = f"{type(exc).__name__}: {exc}"
    return out


def market_disabled_rows(st: dict) -> list[str]:
    """现在被关着的行 id（启动器的开关块 + 用户自己写的禁用）。"""
    return [row for row in st["rows"] if row in st["block_rows"] or row in st["user_disabled"]]


def _compose_with_block(text: str, begin: str, end: str, rows: list[tuple[str, str]]) -> tuple[bool, str, str]:
    """把一块 `disabled: true` 覆盖追加到用户层末尾，返回 (ok, 原因, 新文本)。

    先摘掉同一种块（避免写两份），但**保留另一种块**——启动器有两种块
    （一键禁用 / 插件市场开关），一刀切会把另一个块整段丢掉。

    行格式照插件市场的规矩：包名写成**独立注释行**，`- id:` 那行整行只有 id。
    写成行尾注释 `- id: X  # 包名` 市场就看不见（实测读到 0 条禁用），
    于是"启动器关掉的插件"在市场页面上还显示"启用中"。
    """
    lines = text.splitlines()
    span = find_block(lines, begin, end)
    kept = lines[: span[0]] + lines[span[1] + 1:] if span else lines
    base = "\n".join(kept)
    if base.strip() and not base.endswith("\n"):
        base += "\n"
    # 这些行 id 的旧条目（不管谁写的、写的是 true 还是 false）先删掉：
    # 同一行同时存在 `disabled: true` 和 `disabled: false` 两条，谁说了算就成了谜。
    base = remove_patch_row_entries(base, sorted({row for _pkg, row in rows}))
    block = [begin]
    for pkg, row_id in sorted(set(rows)):
        block.append(f"# {pkg}")
        block.append(f"- id: {row_id}")
        block.append("  disabled: true")
    block.append(end)
    return append_patch_entries(base, block)



def boot_smoke(cfg: dict, lane: str, timeout: int = 90) -> bool:
    """真起一次再停回去：只有"起来过"才能证明这套配置能跑。

    实测教训（一键禁用那一步踩到的）：把某个组合包插入的 `web` 行一起禁掉后，
    离线组合结果**完全正常**，但应用不出 URL，日志只有一句含糊的 `entries did not activate`。
    所以"配置写对了"和"系统还能跑"是两个不同的断言，只有启动能证明后者。
    """
    rc = cmd_open(
        cfg,
        argparse.Namespace(target=lane, port=None, cwd=None, timeout=timeout,
                           no_browser=True, detach=True, adopt_only=False),
    )
    if rc == 0:
        cmd_stop(cfg, argparse.Namespace(target=[lane]))
        return True
    return False


def cmd_market(cfg: dict, args) -> int:
    """看一眼插件市场：装的是哪个版本、开还是关、registry 上有没有新版。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    st = market_status(cfg, lane, with_registry=not getattr(args, "offline", False))
    me = ME_NAME
    info(col(f"── 插件市场（{MARKET_PKG}）：lane「{lane}」──", C.BOLD))
    installed = st["installed"]
    info(f"  装在哪    {st['package_dir']}")
    info(f"  已装版本  {installed or '（没装）'}"
         + (f"      profile 里写的是 {st['spec']}" if st["spec"] else ""))
    info(f"  行 id     {'、'.join(st['rows']) or '(没有补丁行)'}")
    off = market_disabled_rows(st)
    if not st["spec"] and not st["package_dir"].is_dir():
        state = col("这条 lane 没装插件市场", C.YELLOW)
    elif not st["rows"]:
        state = col("装了，但它没有补丁行（可能被跳过/不兼容）", C.YELLOW)
    elif off and len(off) == len(st["rows"]):
        who = "启动器开关" if st["has_block"] else "你自己的配置"
        state = col(f"已关掉（{who}）", C.GREEN)
    elif off:
        state = col(f"部分关着（{ '、'.join(off) }）", C.YELLOW)
    else:
        state = "启用中"
    info(f"  当前状态  {state}")
    if st["latest"]:
        age = _fmt_age(st["published"])
        same = st["latest"] == installed
        info(f"  registry  {st['latest']}"
             + (f"（{age}）" if age else "")
             + ("      ← 就是你现在这个" if same else "      ← 比你现在的新"))
    elif st["registry_error"]:
        warn(f"没查到 registry：{st['registry_error']}")
    snaps = st["snapshots"]
    if snaps:
        newest = snaps[0]
        info(f"  更新快照  {len(snaps)} 个，最新 {newest['name']}"
             f"（快照里是 {newest['version'] or '?'}）")
        for item in snaps[1:4]:
            info(col(f"            {item['name']}（{item['version'] or '?'}）", C.GRAY))
    if st["general_block"]:
        info(col("  注意      这条 lane 还挂着一键禁用块（plugins-on 撤销）", C.GRAY))
    if not (st["spec"] or st["package_dir"].is_dir()):
        return 0
    info("")
    info(col(f"  关掉它 : py {me} market-off {lane}        （写完立刻热生效，不用重启）", C.CYAN))
    info(col(f"  打开它 : py {me} market-on {lane}", C.CYAN))
    info(col(f"  更新   : py {me} market-update {lane}        （点名版本 + 启动冒烟，失败自动回滚）", C.CYAN))
    if snaps:
        info(col(f"  退回   : py {me} market-rollback {lane}", C.CYAN))
    return 0


def cmd_market_off(cfg: dict, args) -> int:
    """单独关掉插件市场：官方组合包和其它插件都不动。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    me = ME_NAME
    st = market_status(cfg, lane)
    if not st["spec"] and not st["package_dir"].is_dir():
        fail(f"lane「{lane}」的 profile 里没有插件市场（{MARKET_PKG}）")
        return 1
    if st["has_block"]:
        warn("插件市场本来就已经关着了（启动器开关块还在）")
        info(col(f"  要打开它： py {me} market-on {lane}", C.CYAN))
        return 0
    if lane_runtime(cfg, lane):
        info(col(f"lane「{lane}」正在运行：补丁层是 DSH 热重载盯着的文件，写进去大约 2 秒生效（不用重启）",
                 C.GRAY))

    info(col(f"── 关掉插件市场：lane「{lane}」──", C.BOLD))
    info(col("  先离线组合一次，确认它的行现在真的在生效…", C.GRAY))
    dump = run_dump_config(cfg, lane)
    if not dump.get("ok"):
        fail(f"组合检查失败（{dump.get('error') or '退出码 ' + str(dump.get('rc'))}），不继续")
        return 1
    rows = [row for row in st["rows"] if row in dump["rows"]]
    if not rows:
        fail("插件市场在组合结果里没有生效的行（补丁里的行 id："
             f"{'、'.join(st['rows']) or '无'}）——它可能被跳过或不兼容")
        return 1

    patch_file = st["patch_file"]
    original = patch_file.read_text(encoding="utf-8")
    already = user_disabled_ids(original)
    to_write = [(MARKET_PKG, row) for row in rows if row not in already]
    notes = [f"{row}：你自己的配置里已经禁用它了，启动器不再写一遍" for row in rows if row in already]
    if not to_write:
        warn("它的行本来就已经是禁用状态（你自己配的），启动器不需要再写")
        for note in notes:
            info(col(f"      {note}", C.GRAY))
        return 0

    ok_compose, why_compose, new_text = _compose_with_block(
        original, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END, to_write)
    if not ok_compose:
        fail(f"补丁层现在追加不了条目，没敢写：{why_compose}")
        return 1
    backup = patch_file.with_suffix(patch_file.suffix + ".bak-market")
    backup_ok, backup_info = backup_patch_text(patch_file, original)
    if not backup_ok:
        fail(f"备份失败（{backup_info}），没敢写")
        return 1
    write_ok, why_write = write_patch_text(patch_file, new_text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1
    info("")
    ok(f"已关掉插件市场（行 id：{'、'.join(rows)}）")
    for note in notes:
        info(col(f"      {note}", C.GRAY))
    info(col(f"  备份      {backup}", C.GRAY))

    if getattr(args, "no_verify", False):
        return 0
    info(col("  再组合一次，核对禁用到没到生效…", C.GRAY))
    after = run_dump_config(cfg, lane)
    if not after.get("ok"):
        warn("复核没能跑起来，禁用的是否生效**未经验证**")
        return 0
    bad = [row for row in rows if not (after["rows"].get(row) or {}).get("disabled")]
    if bad:
        fail(f"写进去了但仍然是启用状态：{'、'.join(bad)}")
    else:
        ok(f"核对通过：{'、'.join(rows)} 已经是禁用状态")

    problems = bool(bad)
    if getattr(args, "no_boot_check", False):
        info(col("  （按你的要求跳过了启动冒烟）", C.GRAY))
    elif lane_runtime(cfg, lane):
        warn("这条 lane 正在运行，跳过启动冒烟——补丁层已经热生效，但"
             "「下次启动能不能起来」这一步没验（想验就 stop 后再跑一次）")
    else:
        info(col("  启动冒烟：确认关掉插件市场后还能起来…", C.GRAY))
        if boot_smoke(cfg, lane):
            ok("启动冒烟通过（已把它停回去）")
        else:
            problems = True
            fail("关掉插件市场后**起不来**——已自动还原回你原来的配置")
            try:
                patch_file.write_text(original, encoding="utf-8")
                ok(f"已还原 {patch_file.name}（备份留在 {backup.name}）")
            except OSError as exc:
                fail(f"自动还原失败，请手动把备份复制回去：{backup} → {patch_file}（{exc}）")
    info("")
    info(col(f"  打开它： py {me} market-on {lane}", C.CYAN))
    return 1 if problems else 0


def cmd_market_on(cfg: dict, args) -> int:
    """再打开插件市场：只删启动器写的那一小段，别的块（一键禁用）一个字不动。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    st = market_status(cfg, lane)
    if not st["has_block"]:
        warn("插件市场本来就没被启动器关着（没有可撤销的）")
        return 0
    patch_file = st["patch_file"]
    original = patch_file.read_text(encoding="utf-8")
    lines = original.splitlines()
    span = find_block(lines, MARKET_BLOCK_BEGIN, MARKET_BLOCK_END)
    removed = market_block_rows(original)
    kept = lines[: span[0]] + lines[span[1] + 1:]
    # 删完可能只剩注释 —— 那就不是顶层数组了，dsh 会拒绝启动这个 profile，补回 `[]`
    text = ensure_patch_array("\n".join(kept))
    if text and not text.endswith("\n"):
        text += "\n"
    backup = patch_file.with_suffix(patch_file.suffix + ".bak-market")
    verdict = ""
    if backup.is_file():
        try:
            bak_text = backup.read_text(encoding="utf-8")
        except OSError:
            bak_text = ""
        if bak_text:
            if text == bak_text:
                verdict = "与备份逐字节一致"
            elif text.rstrip("\n") == bak_text.rstrip("\n"):
                text = bak_text
                verdict = "与备份逐字节一致（按备份补齐了结尾换行）"
            elif user_layer_text(text) == user_layer_text(bak_text):
                verdict = "与备份一致（差异只是启动器写的另一个开关块；你自己的配置没动）"
            else:
                verdict = "与备份不同（你在这期间手改过；备份里是关闭前的原文）"
    write_ok, why_write = write_patch_text(patch_file, text)
    if not write_ok:
        fail(f"写入失败：{why_write}")
        return 1
    ok(f"已打开插件市场（删掉 {span[1] - span[0] - 1} 行覆盖"
       + (f"：{'、'.join(removed)}" if removed else "") + "）")
    if verdict:
        info(col(f"  校验：{verdict}", C.GRAY))
    if lane_runtime(cfg, lane):
        info(col("这条 lane 正在运行：删掉的那几行会被热重载立刻重放（不用重启）", C.GRAY))
    if not getattr(args, "no_verify", False):
        after = run_dump_config(cfg, lane)
        if not after.get("ok"):
            warn("复核没能跑起来，是否恢复未经验证")
        else:
            still = [row for row in (removed or st["rows"]) if (after["rows"].get(row) or {}).get("disabled")]
            if still:
                info(col(f"  仍在禁用中的行：{'、'.join(still)}（你自己的配置写着禁用，不归启动器管）", C.GRAY))
            else:
                ok("核对通过：它的行已经重新启用")
    return 0


def pnpm_store_dir(home, profile: str = PROFILE_NAME) -> str | None:
    """读 profile 的 `node_modules/.modules.yaml`，取它当年是用哪个 pnpm store 装的。

    为什么必须显式告诉 pnpm：pnpm 的 store 默认按**盘符**选（`<盘>\\.pnpm-store\\v11`），
    于是"副本在 D 盘、原件在 C 盘"必然撞上：

        ERR_PNPM_UNEXPECTED_STORE: The dependencies at "...\\node_modules" are currently
        linked from the store at "C:\\...\\pnpm\\store\\v11". pnpm now wants to use the
        store at "D:\\.pnpm-store\\v11".

    而且它**拒绝干活**（退出码 1）——也就是说：**复制出来的副本，没法用 pnpm 装/更新插件**，
    这正好砸在"复制一份去测试插件冲突"这个用途上。修法不是换 store（那要重装 636 MB 依赖），
    而是把 node_modules 里记录的那个 store 原样告诉它。
    """
    manifest = Path(home) / "profiles" / profile / "node_modules" / ".modules.yaml"
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r'"storeDir"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if not match:
        return None
    try:
        return json.loads(f'"{match.group(1)}"')
    except Exception:  # noqa: BLE001
        return match.group(1).replace("\\\\", "\\")


def pnpm_ledger_state(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """读 `node_modules/.modules.yaml`（pnpm 的账本）：storeDir / virtualStoreDir 各指哪。

    只读。`virtualStoreDir` 记的是绝对路径，所以**复制出来的副本会带着原件的路径**——
    这是副本独有的、安静的一类"指回原件"。
    """
    path = Path(lane_home(cfg, lane)) / "profiles" / profile / "node_modules" / ".modules.yaml"
    want = str(profile_dir(lane_home(cfg, lane), profile) / "node_modules" / ".pnpm")
    out = {"path": path, "exists": path.is_file(), "store": None, "virtual": None,
           "want": want, "outside": False, "error": ""}
    if not out["exists"]:
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        out["error"] = str(exc)
        return out
    for key in ("storeDir", "virtualStoreDir"):
        match = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if not match:
            continue
        try:
            value = json.loads(f'"{match.group(1)}"')
        except Exception:  # noqa: BLE001
            value = match.group(1).replace("\\\\", "\\")
        out["store" if key == "storeDir" else "virtual"] = value
    if out["virtual"]:
        prefix = _norm_win(want)
        out["outside"] = not (
            _norm_win(out["virtual"]) == prefix
            or _norm_win(out["virtual"]).startswith(prefix.rstrip("\\") + "\\")
        )
    return out


def repair_pnpm_virtual_store(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """修掉复制出来的 profile 里那个**指回原件**的 `virtualStoreDir`。

    实测（复制副本 → 更新插件市场时踩到）：`clone` 出来的 profile，`node_modules/.modules.yaml`
    里记的还是原件的

        "virtualStoreDir": "C:\\\\Users\\\\Administrator\\\\.dsh\\\\profiles\\\\web\\\\node_modules\\\\.pnpm"

    pnpm 一比对（记录值 vs 它自己算出来的值）就直接拒绝干活，退出码 1：

        ERR_PNPM_UNEXPECTED_VIRTUAL_STORE ... pnpm now wants to use the virtual store at
        "D:\\\\dsh-lanes\\\\homes\\\\global-copy\\\\profiles\\\\web\\\\node_modules\\\\.pnpm"

    后果是"副本里装不了 / 更新不了插件"——正好砸在"复制一份去测插件冲突"这个用途上。
    更糟的是它记的是**原件**的路径：真按记录值来，pnpm 会往你日常那套的 node_modules 里写。

    `storeDir` **故意不动**：那是全机共用的内容寻址缓存，共用是对的（`run_plugin_cmd` 会显式传回去）。
    """
    state = pnpm_ledger_state(cfg, lane, profile)
    out = {"path": state["path"], "changed": False, "recorded": state["virtual"] or "",
           "want": state["want"], "error": state["error"]}
    if not state["exists"] or not state["virtual"] or not state["outside"]:
        return out
    try:
        text = state["path"].read_text(encoding="utf-8")
        match = re.search(r'("virtualStoreDir"\s*:\s*")((?:[^"\\]|\\.)*)(")', text)
        if not match:
            return out
        new_text = text[: match.start(2)] + state["want"].replace("\\", "\\\\") + text[match.end(2):]
        shutil.copyfile(state["path"], state["path"].with_suffix(".yaml.bak"))
        state["path"].write_text(new_text, encoding="utf-8")
    except OSError as exc:
        out["error"] = str(exc)
        return out
    out["changed"] = True
    return out


def _is_abs_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith("/") or text.startswith("\\\\")


def pnpm_file_spec_state(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> list[dict]:
    """查 profile 里那些 `file:` 本地依赖，在**当前这个位置**还指不指得到东西。

    只读。为什么要有这个检查：pnpm 把 `file:` 依赖的解析结果记成**相对路径**，
    而相对路径只在原件那个深度成立（详见 `repair_pnpm_file_specs`）。
    """
    pdir = profile_dir(lane_home(cfg, lane), profile)
    manifest = pdir / "package.json"
    lock = pdir / "pnpm-lock.yaml"
    out: list[dict] = []
    try:
        deps = (json.loads(manifest.read_text(encoding="utf-8")).get("dependencies") or {})
        text = lock.read_text(encoding="utf-8") if lock.is_file() else ""
    except Exception:
        return out
    for name, spec in deps.items():
        if not str(spec).startswith("file:"):
            continue
        item = {"name": str(name), "specifier": str(spec), "lock": "", "resolution": "",
                "resolved": "", "broken": False}
        match = re.search(
            r"^\s{6}" + re.escape(str(name)) + r":\s*\n\s+specifier:.*\n\s+version:\s*(.+?)\s*$",
            text, re.M,
        )
        if match:
            item["lock"] = match.group(1).strip().strip("'\"")
        res_match = re.search(
            r"^\s{2}" + re.escape(str(name)) + r"@[^\n]*:\s*\n\s+resolution:\s*\{directory:\s*(.+?),\s*type:",
            text, re.M,
        )
        if res_match:
            item["resolution"] = res_match.group(1).strip().strip("'\"")
        for raw_spec in (item["lock"], item["resolution"]):
            if not raw_spec:
                continue
            raw = raw_spec[5:] if raw_spec.startswith("file:") else raw_spec
            try:
                candidate = Path(raw) if _is_abs_path(raw) else (pdir / raw)
                if not candidate.exists():
                    item["broken"] = True
                    item["resolved"] = item["resolved"] or str(candidate)
            except OSError:
                item["broken"] = True
            if not item["resolved"]:
                item["resolved"] = str(candidate)
        out.append(item)
    return out


def repair_pnpm_file_specs(cfg: dict, lane: str, profile: str = PROFILE_NAME) -> dict:
    """把复制后**深度变了**的 `file:` 依赖在 pnpm 账本里的相对路径换成绝对路径。

    实测（复制副本 → 更新插件市场时踩到，第三个坑）：

        [ENOENT] no such file or directory, scandir
        'D:\\dsh-lanes\\homes\\Desktop\\deepseek harness\\dsh-tool-result-trimmer'

    profile 里有个本地插件是按**绝对路径**装的
    （`"dsh-tool-result-trimmer": "file:C:/Users/Administrator/Desktop/deepseek harness/dsh-tool-result-trimmer"`），
    但 pnpm 把**解析结果**记成了相对路径：

        version: file:../../../Desktop/deepseek harness/dsh-tool-result-trimmer
        resolution: {directory: ../../../Desktop/deepseek harness/dsh-tool-result-trimmer, ...}

    相对路径只在原件那个深度成立：原件在 `C:\\Users\\Administrator\\.dsh\\profiles\\web`
    （往上三层正好是 `C:\\Users\\Administrator`），副本在 `D:\\dsh-lanes\\homes\\<lane>\\profiles\\web`
    （往上三层是 `D:\\dsh-lanes\\homes`）→ 于是 pnpm 跑去找
    `D:\\dsh-lanes\\homes\\Desktop\\...`，直接 ENOENT。

    这就是"复制"最典型的坑：**配置里混着相对路径，换个深度就悄悄指到别处**。

    两个必须做对的地方（都实测栽过）：
    · 相对路径在锁文件里出现**三次**，其中 `resolution: {directory: ...}` 那一次不带 `file:` 前缀，
      而 pnpm 解析时正是看它——只换 `file:` 那两处，报错会一模一样地再来一遍。
    · 还有**第三个文件**：`node_modules/.pnpm/lock.yaml`（虚拟仓库自己那份锁副本）。
    """
    out = {"changed": [], "skipped": [], "errors": []}
    pdir = profile_dir(lane_home(cfg, lane), profile)
    # 相对路径是"相对于安装时那个 profile 目录"算出来的，所以要把**源 lane 的 profile 目录**也算进来
    origins = [pdir]
    src = (cfg["lanes"].get(lane) or {}).get("clonedFrom")
    if src and src in cfg["lanes"]:
        origins.append(profile_dir(lane_home(cfg, src), profile))
    for item in pnpm_file_spec_state(cfg, lane, profile):
        raw = item["specifier"][5:]
        try:
            want = Path(raw) if _is_abs_path(raw) else (pdir / raw)
        except OSError:
            continue
        want_text = str(want).replace("\\", "/")
        candidates: list[str] = []
        for recorded in (item["lock"], item["resolution"]):
            if recorded.startswith("file:"):
                recorded = recorded[5:]
            if recorded and not _is_abs_path(recorded):
                candidates.append(recorded.replace("\\", "/"))
        for origin in origins:
            try:
                rel = os.path.relpath(str(want), str(origin)).replace("\\", "/")
            except ValueError:      # 跨盘符没有相对路径
                continue
            if not _is_abs_path(rel):
                candidates.append(rel)
        hit = False
        for candidate in dict.fromkeys(candidates):
            if not candidate or candidate == want_text:
                continue
            for fname in ("pnpm-lock.yaml", "node_modules/.pnpm/lock.yaml",
                          "node_modules/.modules.yaml"):
                path = pdir / fname
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                if candidate not in text:
                    continue
                try:
                    shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
                    path.write_text(text.replace(candidate, want_text), encoding="utf-8")
                except OSError as exc:
                    out["errors"].append(f"{fname}: {exc}")
                    continue
                out["changed"].append(f"{item['name']} → {want_text}（{fname}）")
                hit = True
        if not hit:
            out["skipped"].append(item["name"])
    return out


def run_plugin_cmd(cfg: dict, lane: str, pnpm_args: list[str], timeout: int = 1200,
                   label: str = "plugin") -> dict:
    """跑 `dsh plugin --profile web <pnpm 参数>`，把 pnpm 的输出落到文件再回读。

    三个实测细节：
    · 子进程 stdio 必须接**文件句柄**：某些沙箱下管道会被拒绝（EPERM），而 pnpm 的输出还很长。
    · `plugin` 是 dsh 自己的子命令，剩余参数**原样转发**给 pnpm（bin.js 里 plugin 命令
      `allowUnknownOption` + `[args...]`），所以 `--save-exact`、`--config.xxx` 这类 pnpm 旗标能直接用。
    · 必须把 profile 记录的那个 pnpm store 显式传回去，否则跨盘副本一律
      `ERR_PNPM_UNEXPECTED_STORE`（见 `pnpm_store_dir`）。
    """
    home = lane_home(cfg, lane)
    # 顺手修副本里两类"跟着复制一起变味"的东西，否则 pnpm 直接拒绝干活：
    #   ① .modules.yaml 里指回原件的 virtualStoreDir
    #   ② pnpm-lock.yaml 里深度相关的 `file:` 相对路径（本地插件）
    repairs = [
        repair_pnpm_virtual_store(cfg, lane, PROFILE_NAME),
        repair_pnpm_file_specs(cfg, lane, PROFILE_NAME),
    ]
    node = find_node(cfg)
    if not node:
        return {"ok": False, "rc": -1, "tail": [], "error": "找不到 node", "log": None,
                "seconds": 0, "repairs": repairs}
    entry = lane_entry_script(cfg, lane)
    logs = ensure_layout(cfg)["logs"]
    log_p = logs / f".{label}-{lane}.log"
    err_p = logs / f".{label}-{lane}.err"
    env = dict(os.environ)
    env["DSH_HOME"] = str(home)
    node_dir = str(Path(node).parent)
    if node_dir not in env.get("PATH", ""):
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    args = list(pnpm_args)
    store = pnpm_store_dir(home, PROFILE_NAME)
    if store:
        args.append(f"--config.store-dir={store}")
        # pnpm 11/12 读 PNPM_CONFIG_*（带盘符的路径写成命令行参数也能被某些版本忽略），两个都给最稳
        env["PNPM_CONFIG_STORE_DIR"] = store
    cmd = [str(node), str(entry), "plugin", "--profile", PROFILE_NAME, *args]
    rc = -1
    error = ""
    started = time.time()
    try:
        with open(log_p, "w", encoding="utf-8") as fo, open(err_p, "w", encoding="utf-8") as fe:
            rc = subprocess.run(cmd, stdout=fo, stderr=fe, env=env, timeout=timeout,
                                cwd=str(profile_dir(home, PROFILE_NAME))).returncode
    except subprocess.TimeoutExpired:
        rc, error = -2, f"超过 {timeout} 秒还没结束（已终止）"
    except Exception as exc:  # noqa: BLE001
        rc, error = -1, f"{type(exc).__name__}: {exc}"
    tail = [line for line in (tail_lines(log_p, 14) + tail_lines(err_p, 14)) if line.strip()]
    return {"ok": rc == 0, "rc": rc, "log": log_p, "err_log": err_p, "tail": tail[-14:],
            "error": error, "seconds": round(time.time() - started, 1), "repairs": repairs}


def market_snapshot(cfg: dict, lane: str, label: str = "", note: str = "",
                    profile: str = PROFILE_NAME) -> dict:
    """更新前先存一份：插件市场自己的包目录 + 三个清单文件。

    只存这三样就够：版本号在 package.json，解析结果在 pnpm-lock.yaml，
    "新版本按住名单"在 pnpm-workspace.yaml，代码在 node_modules 里。
    实测这个包只有 3.8 MB / 174 个文件，存一份是零成本的。
    """
    home = lane_home(cfg, lane)
    pdir = profile_dir(home, profile)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    dest = paths(cfg)["backups"] / "market" / lane / name
    (dest / "node_modules").mkdir(parents=True, exist_ok=True)
    config_files: dict[str, int] = {}
    for fname in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
        src = pdir / fname
        if src.is_file():
            shutil.copyfile(src, dest / fname)
            config_files[fname] = src.stat().st_size
    pkgdir = pdir / "node_modules" / MARKET_PKG
    stats = copy_tree(pkgdir, dest / "node_modules" / MARKET_PKG) if pkgdir.is_dir() else {
        "files": 0, "bytes": 0, "errors": [], "links": 0
    }
    manifest = {
        "lane": lane,
        "profile": profile,
        "home": str(home),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "package": MARKET_PKG,
        "version": _manifest_version(pkgdir),
        "spec": market_declared_spec(cfg, lane, profile),
        "files": stats.get("files", 0),
        "bytes": stats.get("bytes", 0),
        "errors": [str(err) for err in (stats.get("errors") or [])],
        "config_files": config_files,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["dir"] = dest
    manifest["pruned"] = prune_market_snapshots(cfg, lane)
    return manifest


def market_restore_snapshot(cfg: dict, lane: str, snap_dir, profile: str = PROFILE_NAME) -> bool:
    """把快照里的包目录与三个清单文件放回去（更新失败 / 起不来时用）。"""
    pdir = profile_dir(lane_home(cfg, lane), profile)
    snap = Path(snap_dir)
    if not (snap / "manifest.json").is_file():
        fail(f"快照不完整（缺 manifest.json）：{snap}")
        return False
    ok_all = True
    try:
        pkgdir = pdir / "node_modules" / MARKET_PKG
        snap_pkg = snap / "node_modules" / MARKET_PKG
        if snap_pkg.is_dir():
            if pkgdir.exists():
                shutil.rmtree(pkgdir)
            stats = copy_tree(snap_pkg, pkgdir)
            if stats.get("errors"):
                fail(f"还原包目录时有 {len(stats['errors'])} 个错误：{stats['errors'][:2]}")
                ok_all = False
        for fname in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml"):
            src = snap / fname
            if src.is_file():
                shutil.copyfile(src, pdir / fname)
    except OSError as exc:
        fail(f"还原失败：{exc}")
        return False
    return ok_all


def cmd_market_rollback(cfg: dict, args) -> int:
    """用启动器自动存的快照把插件市场退回上一版。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = market_snapshots(cfg, lane)
    if not snaps:
        fail(f"lane「{lane}」还没有插件市场快照（market-update 会每次自动存一份）")
        return 1
    target = None
    if getattr(args, "name", None):
        for item in snaps:
            if item["name"] == args.name:
                target = item
                break
        if target is None:
            fail(f"没有这个快照：{args.name}")
            return 1
    else:
        target = snaps[0]
    if not target["ok"]:
        fail(f"快照 {target['name']} 不完整（缺 manifest.json），不能用")
        return 1
    if lane_runtime(cfg, lane):
        warn(f"lane「{lane}」正在运行：还原的是磁盘上的文件，运行中的进程要重启才会用新的")
    info(col(f"── 退回插件市场：lane「{lane}」──", C.BOLD))
    info(f"  快照      {target['name']}（{target['created']}，里面是 {target['version'] or '?'}）")
    if not market_restore_snapshot(cfg, lane, target["dir"]):
        return 1
    now = _manifest_version(market_package_dir(cfg, lane))
    if now == target["version"]:
        ok(f"已退回 {now}")
    else:
        warn(f"退回后读到的是 {now or '（读不到）'}，和快照里的 {target['version'] or '?'} 不一致，请人工看一眼")
    info(col(f"  重启这条 lane 才会生效： py {ME_NAME} stop {lane} && py {ME_NAME} open {lane}", C.CYAN))
    return 0


def cmd_market_update(cfg: dict, args) -> int:
    """更新插件市场：**点名到版本**地装，装完做启动冒烟，失败自动回滚。

    实测（pnpm 11.22.0）的规矩是这样：`minimumReleaseAge` 只拦**自动升级**（解析 `@latest` 或版本范围），
    而**点名到具体版本的安装**它不但放行，还会自己往 `pnpm-workspace.yaml` 里记一条：

        Added 1 entry to minimumReleaseAgeExclude in pnpm-workspace.yaml ...
          dshmarket@1.66.1

    所以"更新"的关键不是绕过，而是**把版本号说出来**——写 `@latest` 才会被静默降级。
    只有 profile 显式打开了 `minimumReleaseAgeStrict`（连点名也拦）才需要 `--anyway`
    （带 pnpm 的一次性绕过 `--config.minimum-release-age=0`；这个拼法是 dshmarket 自己源码里的结论，
    `--config.minimumReleaseAge=0` 在 pnpm 12.3+ 上会被静默忽略）。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    me = ME_NAME
    info(col(f"── 更新插件市场：lane「{lane}」──", C.BOLD))
    if lane_runtime(cfg, lane) and not getattr(args, "force", False):
        fail(f"lane「{lane}」正在运行：更新会改它 profile 里的 node_modules，"
             "运行中的进程可能读到半截文件。先 stop 再更新（或加 --force 强来）")
        return 1
    st = market_status(cfg, lane, with_registry=not getattr(args, "offline", False))
    current = st["installed"]
    if not st["spec"] and not st["package_dir"].is_dir():
        fail(f"lane「{lane}」的 profile 里没有插件市场（{MARKET_PKG}），没有可更新的东西")
        return 1
    target = getattr(args, "version", None) or st["latest"]
    if not target:
        fail("不知道更新到哪个版本：registry 没查到"
             f"（{st['registry_error'] or '离线模式'}）。用 --version 指定")
        return 1
    versions = st.get("versions") or []
    if versions and getattr(args, "version", None) and target not in versions:
        near = sorted(versions, key=version_key)[-6:]
        fail(f"registry 上没有 {target} 这个版本（最近的几个：{'、'.join(near)}）")
        return 1
    if target == current and not getattr(args, "force", False):
        ok(f"已经是最新：{target}")
        return 0

    info(f"  当前版本  {current or '（没装）'}"
         + (f"      profile 里写的是 {st['spec']}" if st["spec"] else ""))
    info(f"  目标版本  {target}" + (f"（{_fmt_age(st['published'])}）" if st["published"] else ""))
    info(col("  规矩      只装点名的这个版本（pnpm 会自己把它记进 minimumReleaseAgeExclude）；"
             "它的「按住新版本」只拦**自动升级**，不拦点名安装", C.GRAY))
    if args.anyway:
        info(col("  --anyway   连「点名也要拦」的 profile（minimumReleaseAgeStrict）都硬闯", C.GRAY))
    snap = market_snapshot(cfg, lane, label=f"before-{target}", note=f"{current or '?'} → {target}")
    info(col(f"  已存快照  {snap['dir']}（{snap['files']} 个文件 / {human_size(snap['bytes'])}）", C.GRAY))
    if snap.get("pruned"):
        info(col(f"            顺手清掉更老的 {len(snap['pruned'])} 份快照（只留最近 5 份）", C.GRAY))
    info(col("  正在装（走 dsh plugin → pnpm，可能要一两分钟）…", C.GRAY))
    pnpm_args = ["add", f"{MARKET_PKG}@{target}"]
    if args.anyway:
        pnpm_args.append(MARKET_BYPASS_FLAG)
    res = run_plugin_cmd(cfg, lane, pnpm_args, label="market-add")
    for rep in res.get("repairs") or []:
        if isinstance(rep, dict) and rep.get("changed"):
            if isinstance(rep["changed"], list):
                for line in rep["changed"]:
                    info(col(f"  已修复    pnpm 账本里跟着复制变味的本地依赖：{line}", C.YELLOW))
            else:
                info(col(f"  已修复    pnpm 账本里的 virtualStoreDir 原本指回原件"
                         f"（{rep['recorded']}），已改成本 lane 自己的 {rep['want']}", C.YELLOW))
        elif isinstance(rep, dict) and rep.get("errors"):
            for line in rep["errors"]:
                info(col(f"  （pnpm 账本有一处没修上：{line}）", C.GRAY))
    if not res["ok"]:
        fail(f"pnpm 没装成（退出码 {res['rc']}{'：' + res['error'] if res['error'] else ''}）")
        for line in res["tail"]:
            info(col("      " + line, C.GRAY))
        if res.get("log"):
            info(col(f"      完整日志：{res['log']}", C.GRAY))
        info(col(f"      恢复现场： py {me} market-rollback {lane}", C.CYAN))
        return 1

    now = _manifest_version(market_package_dir(cfg, lane))
    if now != target:
        warn(f"pnpm 跑完了（退出码 0），但装上的还是 {now or '旧版本'}，不是 {target}")
        if not args.anyway:
            info(col("      点名了版本还是没装上，通常是这个 profile 打开了 minimumReleaseAgeStrict"
                     "（默认只拦自动升级，strict 连点名版本也拦）——pnpm 会安静地保留旧版本、返回 0。", C.GRAY))
            info(col(f"      要装就明确闯一次： py {me} market-update {lane} --version {target} --anyway", C.CYAN))
            info(col("      --anyway 会带上 pnpm 的一次性绕过 --config.minimum-release-age=0", C.GRAY))
        if market_restore_snapshot(cfg, lane, snap["dir"]):
            info(col("      已把刚才的改动还原回去", C.GRAY))
        else:
            info(col(f"      还原失败，请手动跑： py {me} market-rollback {lane}", C.CYAN))
        return 1
    ok(f"已更新：{current or '旧版本'} → {target}")
    info(col(f"      profile 里记的是 {market_declared_spec(cfg, lane) or '?'}"
             "（pnpm 更新已存在的依赖时保留它原来的 ^ 写法，装上的就是你点名那个版本）", C.GRAY))

    problems = False
    rows_new = bundle_row_ids(lane_home(cfg, lane), MARKET_PKG, PROFILE_NAME)
    if not getattr(args, "no_verify", False):
        info(col("  再组合一次，确认行还在、插件市场还能被加载…", C.GRAY))
        dump = run_dump_config(cfg, lane)
        if not dump.get("ok"):
            warn("组合复核没跑起来，更新的效果**未经验证**")
        else:
            present = [row for row in rows_new if row in dump["rows"]]
            if present:
                ok(f"组合核对通过：行 {'、'.join(present)} 在组合结果里"
                   + ("（当前是禁用状态）" if all((dump['rows'][r] or {}).get("disabled") for r in present) else ""))
            else:
                fail(f"更新后它的行（{'、'.join(rows_new) or '无'}）不在组合结果里了——新版本可能改了行 id 或不兼容")
                problems = True

    if getattr(args, "no_boot_check", False):
        info(col("  （按你的要求跳过了启动冒烟）", C.GRAY))
    elif lane_runtime(cfg, lane):
        warn("这条 lane 正在运行，跳过启动冒烟——更新要重启才生效")
    else:
        info(col("  启动冒烟：确认新版本还能起来…", C.GRAY))
        if boot_smoke(cfg, lane):
            ok("启动冒烟通过（已把它停回去）")
        else:
            problems = True
            fail("更新后**起不来**——正在回滚到更新前的版本")
            if market_restore_snapshot(cfg, lane, snap["dir"]):
                back = _manifest_version(market_package_dir(cfg, lane))
                ok(f"已回滚（现在是 {back or '?'}）")
            else:
                fail(f"自动回滚失败，请手动跑： py {me} market-rollback {lane}")
    info("")
    info(col(f"  重启生效： py {me} stop {lane} && py {me} open {lane}", C.CYAN))
    info(col(f"  反悔用  ： py {me} market-rollback {lane}", C.CYAN))
    return 1 if problems else 0


# ======================= 备份对话（sessions 快照） =======================


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            data = fh.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def tree_hashes(root) -> dict[str, list]:
    """整棵树的逐文件指纹：{相对路径: [字节数, sha256]}（路径统一用 `/`）。

    对话文件都是 .zstd（已经压过），所以"逐文件哈希"是唯一能证明
    "这份备份真的能用"的办法——文件数对上不代表内容没坏。
    """
    out: dict[str, list] = {}
    root = Path(root)
    if not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if _is_reparse(path):
                continue
            try:
                out[rel] = [path.stat().st_size, sha256_file(path)]
            except OSError:
                out[rel] = [-1, "read-error"]
    return out


def chat_lane_root(cfg: dict, lane: str) -> Path:
    return paths(cfg)["backups"] / "chat" / lane


def chat_snapshots(cfg: dict, lane: str) -> list[dict]:
    root = chat_lane_root(cfg, lane)
    items: list[dict] = []
    if not root.is_dir():
        return items
    for path in sorted(root.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        manifest = path / "manifest.json"
        item = {"name": path.name, "dir": path, "created": "", "sessions": 0, "files": 0,
                "bytes": 0, "version": "", "running": False, "note": "", "ok": manifest.is_file(),
                "size": 0}
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                for key in ("created", "sessions", "files", "bytes", "version", "running", "note"):
                    if key in data:
                        item[key] = data[key]
            except Exception:
                pass
        item["size"] = dir_size(path)
        items.append(item)
    return items


def sessions_snapshot(cfg: dict, lane: str, label: str = "", note: str = "") -> dict:
    """把 <HOME>/sessions 整棵复制成一份快照，并记录逐文件指纹。"""
    home = lane_home(cfg, lane)
    src = home / "sessions"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label}" if label else stamp
    dest = chat_lane_root(cfg, lane) / name
    if dest.exists():
        name = f"{name}-{int(time.time()) % 1000}"
        dest = chat_lane_root(cfg, lane) / name
    dest.mkdir(parents=True, exist_ok=True)
    running = bool(lane_runtime(cfg, lane))
    stats = copy_tree(src, dest / "sessions")
    hashes = tree_hashes(dest / "sessions")
    manifest = {
        "lane": lane,
        "home": str(home),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "source": str(src),
        "running": running,
        "version": cfg["lanes"].get(lane, {}).get("version") or "",
        "sessions": sum(1 for key in hashes if key.endswith(".zstd")),
        "files": stats.get("files", 0),
        "bytes": stats.get("bytes", 0),
        "errors": [str(err) for err in (stats.get("errors") or [])],
        "hashes": hashes,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest["dir"] = dest
    manifest["name"] = name
    return manifest


def _ask_yes(question: str) -> bool:
    try:
        answer = input(f"{question}（输入 y 回车确认，其它一律取消）：").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes", "是")


def cmd_chat_backup(cfg: dict, args) -> int:
    """给这条 lane 的对话记录打一份完整快照（升级 / 动插件之前先来一份）。"""
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    home = lane_home(cfg, lane)
    src = home / "sessions"
    if not src.is_dir():
        fail(f"这条 lane 还没有对话记录目录：{src}")
        return 1
    info(col(f"── 备份对话：lane「{lane}」──", C.BOLD))
    info(f"  来源      {src}")
    if lane_runtime(cfg, lane):
        warn("这条 lane 正在运行：正在写入的那个会话文件可能是半截的（其它会话不受影响）。"
             "要完全一致就先 stop 再备份。")
    info(col("  正在复制并逐文件算指纹…", C.GRAY))
    started = time.time()
    snap = sessions_snapshot(cfg, lane, label=getattr(args, "name", None) or "",
                             note="手动备份")
    took = round(time.time() - started, 1)
    info("")
    ok(f"已备份 {snap['sessions']} 个会话文件 / {snap['files']} 个文件 / {human_size(snap['bytes'])}"
       f"（{took} 秒）")
    info(f"  快照      {snap['dir']}")
    if snap["errors"]:
        fail(f"复制时有 {len(snap['errors'])} 个错误，这份备份**不可信**：{snap['errors'][:3]}")
        return 1
    me = ME_NAME
    info("")
    info(col(f"  看备份： py {me} chat-list {lane}", C.CYAN))
    info(col(f"  恢复  ： py {me} chat-restore {lane} {snap['name']}", C.CYAN))
    return 0


def cmd_chat_list(cfg: dict, args) -> int:
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = chat_snapshots(cfg, lane)
    me = ME_NAME
    info(col(f"── lane「{lane}」的对话备份 ──", C.BOLD))
    info(f"  目录      {chat_lane_root(cfg, lane)}")
    info(f"  会话目录  {lane_home(cfg, lane) / 'sessions'}"
         f"（现在有 {count_sessions(lane_home(cfg, lane))} 个会话文件）")
    if not snaps:
        info("")
        warn("还没有备份。先来一份：")
        info(col(f"      py {me} chat-backup {lane}", C.CYAN))
        return 0
    total = sum(int(item["size"] or 0) for item in snaps)
    info(f"  共 {len(snaps)} 份 / {human_size(total)}")
    info("")
    info(f"  {'名称':<32}{'时间':<20}{'会话':>5}{'大小':>12}  说明")
    info("  " + "─" * 86)
    for item in snaps:
        flag = "" if item["ok"] else "  ← 缺 manifest.json，不可用"
        note = item["note"] or ("运行时备份" if item["running"] else "")
        info(f"  {item['name'][:31]:<32}{str(item['created'])[:19]:<20}"
             f"{item['sessions']:>5}{human_size(item['size']):>12}  {note}{flag}")
    info("")
    info(col(f"  恢复： py {me} chat-restore {lane} <名称>      删除： py {me} chat-rm {lane} <名称>", C.CYAN))
    return 0


def cmd_chat_restore(cfg: dict, args) -> int:
    """把对话记录恢复到某个快照。

    动手之前做两件安全事：先校验**快照自身**是不是好的（坏的备份恢复只会更糟），
    再把**现状**另存一份（恢复错了还能回来）。
    """
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = chat_snapshots(cfg, lane)
    if not snaps:
        fail(f"lane「{lane}」还没有对话备份，先 chat-backup {lane}")
        return 1
    target = None
    if getattr(args, "name", None):
        for item in snaps:
            if item["name"] == args.name:
                target = item
                break
        if target is None:
            fail(f"没有这个快照：{args.name}（用 chat-list 看有哪些）")
            return 1
    else:
        target = snaps[0]
    if not target["ok"]:
        fail(f"快照 {target['name']} 不完整（缺 manifest.json），不能用")
        return 1
    home = lane_home(cfg, lane)
    live = home / "sessions"
    if live.parent != home or live.name != "sessions":
        fail("内部安全检查没通过（会话目录路径异常），不动手")
        return 1
    data = json.loads((target["dir"] / "manifest.json").read_text(encoding="utf-8"))
    expected = data.get("hashes") or {}

    info(col(f"── 恢复对话记录：lane「{lane}」──", C.BOLD))
    info(f"  快照      {target['name']}（{target['created']}，"
         f"{target['sessions']} 个会话文件 / {human_size(target['bytes'])}）")
    info(f"  恢复到    {live}")
    if data.get("running"):
        warn("这份快照是**运行时**存的，其中一个会话文件可能是半截的")
    # 没让启动器替你停就直接拒掉：别等校验完一遍（几百 MB）才说"车道在跑"。
    # 带 --stop 的话，校验放在停之前——万一备份本身是坏的，就不用白白停一次。
    if lane_runtime(cfg, lane) and not getattr(args, "stop", False):
        fail(f"lane「{lane}」正在运行：恢复会话必须先停它（或加 --stop 让启动器替你停）")
        return 1
    info("")
    info(col("  先校验快照自身（万一备份本身坏了，恢复只会更糟）…", C.GRAY))
    if expected:
        actual = tree_hashes(target["dir"] / "sessions")
        bad = [key for key, value in expected.items() if actual.get(key) != value]
        extra = [key for key in actual if key not in expected]
        if bad or extra:
            message = f"快照自身有 {len(bad)} 个文件对不上、{len(extra)} 个多出来"
            if getattr(args, "force", False):
                warn(message + "（按 --force 继续）")
            else:
                fail(message + "——先别恢复。确认没问题再加 --force")
                return 1
        else:
            ok(f"快照校验通过（{len(expected)} 个文件逐字节一致）")
    else:
        warn("这份快照没记逐文件指纹（老备份），只能核对文件数")

    if lane_runtime(cfg, lane):
        if not getattr(args, "stop", False):
            fail(f"lane「{lane}」正在运行：恢复会话必须先停它（或加 --stop 让启动器替你停）")
            return 1
        info(col("  正在停止这条 lane…", C.GRAY))
        cmd_stop(cfg, argparse.Namespace(target=[lane]))

    if not getattr(args, "yes", False) and not _ask_yes("确认恢复？当前的对话记录会先被另存一份"):
        info("已取消")
        return 0

    pre = None
    if live.is_dir() and any(live.iterdir()):
        info(col("  先把现状另存一份（恢复错了还能回来）…", C.GRAY))
        pre = sessions_snapshot(cfg, lane, label="恢复前", note=f"恢复到 {target['name']} 之前的现状")
        info(col(f"      已存：{pre['name']}", C.GRAY))
    info(col("  正在替换会话目录…", C.GRAY))
    try:
        if live.exists():
            shutil.rmtree(live)
        stats = copy_tree(target["dir"] / "sessions", live)
    except OSError as exc:
        fail(f"替换失败：{exc}")
        if pre:
            info(col(f"      现状还在快照 {pre['name']} 里，可以用 chat-restore {lane} {pre['name']} --yes 找回来", C.GRAY))
        return 1
    ok(f"已恢复 {stats.get('files', 0)} 个文件（{human_size(stats.get('bytes', 0))}）")
    if expected:
        after = tree_hashes(live)
        diff = [key for key, value in expected.items() if after.get(key) != value]
        if diff:
            fail(f"恢复后有 {len(diff)} 个文件对不上：{diff[:3]}")
            return 1
        ok(f"逐文件校验通过：{len(expected)} 个文件与快照完全一致")
    me = ME_NAME
    if pre:
        info(col(f"  反悔用  ： py {me} chat-restore {lane} {pre['name']} --yes", C.CYAN))
    info(col(f"  重启    ： py {me} open {lane}", C.CYAN))
    return 0


def cmd_chat_rm(cfg: dict, args) -> int:
    lane = args.lane
    if lane not in cfg.get("lanes", {}):
        fail(f"没有 lane「{lane}」")
        return 1
    snaps = chat_snapshots(cfg, lane)
    target = None
    for item in snaps:
        if item["name"] == args.name:
            target = item
            break
    if target is None:
        fail(f"没有这个快照：{args.name}")
        return 1
    root = chat_lane_root(cfg, lane).resolve()
    victim = Path(target["dir"]).resolve()
    if victim.parent != root:
        fail("内部安全检查没通过（快照不在备份目录里），不动手")
        return 1
    size = target["size"]
    if not getattr(args, "yes", False) and not _ask_yes(f"确认删除快照 {target['name']}（{human_size(size)}）"):
        info("已取消")
        return 0
    try:
        shutil.rmtree(victim)
    except OSError as exc:
        fail(f"删除失败：{exc}")
        return 1
    ok(f"已删除快照 {target['name']}（{human_size(size)}）")
    return 0


def lane_delete_plan(cfg: dict, lane: str, sizes: bool = True) -> dict:
    """算清"删掉这条 lane"具体会动哪些文件，供 GUI / CLI 展示与确认。

    两条安全线：
    · 只删 lane 根目录（<root>/versions、<root>/homes）里的东西；
      接管型 lane 的安装树在 npm 全局目录、HOME 可能是 %USERPROFILE%\\.dsh —— 默认都不碰。
    · 多个 lane 共用同一棵安装树（同版本）时，安装树不删。

    sizes=False 时跳过目录体积统计（那是全盘遍历，几百毫秒起步）——
    GUI 先拿这份即时结果把对话框画出来，体积再在后台线程里补。
    """
    data = cfg["lanes"][lane]
    p = paths(cfg)
    root = Path(cfg["root"])
    home = Path(data.get("home") or (p["homes"] / lane))
    install = lane_install_dir(cfg, lane)

    def inside(target: Path) -> bool:
        try:
            Path(target).resolve().relative_to(root.resolve())
            return True
        except Exception:
            return False

    # 副本的安装树是我们自己复制出来的（<root>/clones/<lane>），可以随 lane 一起删；
    # 接管型的安装树在 npm 全局目录 / DSH Desktop 之类的地方，默认不碰。
    owns_install = False
    if data.get("installDir"):
        try:
            Path(install).resolve().relative_to(p["clones"].resolve())
            owns_install = True
        except Exception:
            owns_install = False

    shared = [
        n
        for n, d in cfg.get("lanes", {}).items()
        if n != lane
        and isinstance(d, dict)
        and not d.get("installDir")
        and str(d.get("version")) == str(data.get("version"))
    ]
    return {
        "lane": lane,
        "data": data,
        "version": data.get("version"),
        "home": home,
        "install": install,
        "home_inside": inside(home),
        "install_inside": inside(install) and (not data.get("installDir") or owns_install),
        "adopted": bool(data.get("installDir")) and not owns_install,
        "owns_install": owns_install,
        "kind": data.get("kind") or ("adopted" if data.get("installDir") else "created"),
        "shared_with": [] if owns_install else shared,
        "log": p["logs"] / f"{lane}.log",
        "run": p["run"] / f"{lane}.json",
        "home_size": dir_size(home) if (sizes and home.is_dir()) else None,
        "install_size": dir_size(install) if (sizes and install.is_dir()) else None,
        "is_primary": primary_lane(cfg) == lane,
    }


def cmd_delete(cfg: dict, args) -> int:
    """删掉一套 DSH：登记 + 运行态 + 安装树 + DSH_HOME（后两项按安全线决定）。"""
    lane = args.lane
    data = cfg.get("lanes", {}).get(lane)
    if not isinstance(data, dict):
        fail(f"没有 lane「{lane}」")
        return 1

    plan = lane_delete_plan(cfg, lane)
    rt = lane_runtime(cfg, lane)
    del_home = (not args.keep_home) and (plan["home_inside"] or args.home_too)
    del_install = (not args.keep_install) and plan["install_inside"] and not plan["shared_with"]

    if not args.yes:
        info("")
        info(col(f"── 删除 lane「{lane}」──", C.BOLD))
        info(f"      版本      : {plan['version']}")
        if plan["is_primary"]:
            info("")
            warn("★ 这是你的【主要版本】—— 你日常真正在用的那一套。")
            warn("  删除后这里的会话记录 / 设置 / 插件组合会一并消失，无法恢复。")
            info("")
        info("  将被删除：")
        info(f"    · 登记信息    lane「{lane}」" + ("（含主要版本标记）" if plan["is_primary"] else ""))
        if plan["run"].is_file():
            info(f"    · 运行态      {plan['run']}")
        if plan["log"].is_file():
            info(f"    · 启动日志    {plan['log']}")
        if del_install:
            info(f"    · 安装树      {plan['install']}　({human_size(plan['install_size'])})")
        if del_home:
            info(f"    · DSH_HOME    {plan['home']}　({human_size(plan['home_size'])})")
        info("  将被保留：")
        if not del_install:
            why = (
                "接管型：删的是 npm 全局那份，本启动器不动它（要卸载用 npm uninstall -g）"
                if plan["adopted"]
                else f"其它 lane 还在用同一棵安装树：{'、'.join(plan['shared_with'])}"
            )
            info(f"    · 安装树      {plan['install']}")
            info(col(f"                  （{why}）", C.GRAY))
        if not del_home:
            if args.keep_home:
                why = "你选了保留（--keep-home）"
            else:
                why = "不在 lane 根目录里（像是你现有的 HOME），删它会连真实会话一起清掉"
            info(f"    · DSH_HOME    {plan['home']}　({human_size(plan['home_size'])})")
            info(col(f"                  （{why}）", C.GRAY))
            if not plan["home_inside"] and not args.keep_home:
                info(col("                  想连它一起删，加 --home-too（危险）", C.GRAY))
        if rt:
            if not args.stop:
                info("")
                warn(f"它现在正在运行（pid {rt.get('pid')}，端口 {rt.get('port')}）。")
                warn(f"加 --stop 让它先停下来再删：delete {lane} --stop")
                return 1
            info(col(f"      （会先停止 pid {rt.get('pid')}）", C.GRAY))
        info("")
        try:
            typed = input(f"  请输入「{lane}」以确认删除（其它任意输入＝取消）：").strip()
        except (EOFError, KeyboardInterrupt):
            typed = ""
        if typed != lane:
            warn("已取消，什么都没有删除。")
            return 1
    elif rt and not args.stop:
        fail(f"lane「{lane}」正在运行（pid {rt.get('pid')}）。先 stop，或加 --stop。")
        return 1

    if rt and args.stop:
        info(f"先停止 lane「{lane}」…")
        cmd_stop(cfg, argparse.Namespace(target=[lane]))
        cfg = load_config()

    info("")
    removed: list[str] = []
    if del_install and plan["install"].is_dir():
        try:
            shutil.rmtree(plan["install"])
            removed.append(f"安装树    {plan['install']}　({human_size(plan['install_size'])})")
        except Exception as exc:  # noqa: BLE001
            fail(f"删安装树失败（{plan['install']}）：{exc}")
    if del_home and plan["home"].is_dir():
        try:
            shutil.rmtree(plan["home"])
            removed.append(f"DSH_HOME  {plan['home']}　({human_size(plan['home_size'])})")
        except Exception as exc:  # noqa: BLE001
            fail(f"删 DSH_HOME 失败（{plan['home']}）：{exc}")
    for f in (plan["run"], plan["log"]):
        try:
            if Path(f).is_file():
                Path(f).unlink()
        except OSError:
            pass

    cfg = load_config()
    cfg.get("lanes", {}).pop(lane, None)
    was_primary = str(cfg.get("primary") or "") == lane
    if was_primary:
        cfg["primary"] = ""
    save_config(cfg)
    clear_run(cfg, lane)

    info("")
    ok(f"已删除 lane「{lane}」（版本 {plan['version']}）")
    for item in removed:
        info(f"      · {item}")
    if not removed:
        info(col("      （只清了登记与运行态，没删任何目录）", C.GRAY))
    left = ordered_lanes(cfg)
    if was_primary:
        warn("主要版本标记已一并清空。")
    if left and not primary_lane(cfg):
        info(col(f"  建议重新指定主要版本： py {ME_NAME} primary {left[0][0]}", C.CYAN))
    return 0


def cmd_logs(cfg: dict, args) -> int:
    log = paths(cfg)["logs"] / f"{args.lane}.log"
    if not log.is_file():
        fail(f"没有日志：{log}")
        return 1
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-args.lines:]
    info(col(f"── {log}（最后 {len(tail)} 行）──", C.BOLD))
    for line in tail:
        info(line)
    return 0


# ======================= 入口 =======================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsh_lanes",
        description="DSH 多版本启动器：创建 / 打开指定版本的 DeepSeek Harness（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  py dsh_lanes.py doctor\n"
            "  py dsh_lanes.py versions\n"
            "  py dsh_lanes.py create next 0.1.7-rc.2\n"
            "  py dsh_lanes.py open next\n"
            "  py dsh_lanes.py open 0.1.5-rc.3 --port 3082 --detach\n"
            "  py dsh_lanes.py stop next\n"
        ),
    )
    parser.add_argument("--root", help="覆盖 lane 根目录（也可写进 lanes.json）")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("doctor", help="环境自检：node / npm / root 可写 / registry 可达")
    p_ver = sub.add_parser("versions", help="查询 npm 上可用的 dsh 版本与 dist-tag")
    p_ver.add_argument("--all", action="store_true", help="列出全部版本（默认最近 15 个）")

    p_create = sub.add_parser("create", help="安装指定版本并登记为一条 lane")
    p_create.add_argument("lane", help="lane 名，例如 stable / next / last")
    p_create.add_argument("version", help="版本号或 dist-tag，例如 0.1.7-rc.2 / next / latest")
    p_create.add_argument("--port", type=int, help="指定端口（默认自动挑空闲端口）")
    p_create.add_argument("--force", action="store_true", help="lane 已存在时覆盖其版本")
    p_create.add_argument("--no-follow", dest="follow", action="store_false",
                          help="安装时不实时跟随日志")

    p_open = sub.add_parser("open", help="打开一条 lane（lane 名或已安装的版本号）")
    p_open.add_argument("target", help="lane 名或版本号")
    p_open.add_argument("--port", type=int, help="覆盖端口")
    p_open.add_argument("--cwd", help="覆盖工作区（影响 session 归属）")
    p_open.add_argument("--timeout", type=int, help="启动等待超时秒数")
    p_open.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_open.add_argument("--detach", action="store_true", help="启动后立即返回（后台运行）")
    p_open.add_argument(
        "--adopt-only",
        dest="adopt_only",
        action="store_true",
        help="端口已有实例时只接管、不打开浏览器",
    )

    sub.add_parser("list", help="列出全部 lane 及其运行状态")

    p_stop = sub.add_parser("stop", help="停止 lane（默认全部）")
    p_stop.add_argument("target", nargs="*", help="lane 名，缺省停止全部")

    p_logs = sub.add_parser("logs", help="查看某条 lane 的日志")
    p_logs.add_argument("lane")
    p_logs.add_argument("--lines", type=int, default=60, help="显示最后 N 行（默认 60）")

    sub.add_parser("instances", help="列出本机所有 DSH 实例（含不是本启动器启动的，如全局安装的 3080）")

    p_adopt = sub.add_parser(
        "adopt", help="把本机已有的安装（默认 npm 全局那份）注册成 lane，沿用它的 DSH_HOME"
    )
    p_adopt.add_argument("lane", help="lane 名，例如 global")
    p_adopt.add_argument("--install", help="指定安装树路径（默认取 npm 全局那份）")
    p_adopt.add_argument("--port", type=int, help="端口（默认 3080；被非 DSH 占用时自动顺延）")
    p_adopt.add_argument("--home", help="DSH_HOME（默认 %%USERPROFILE%%\\.dsh）")
    p_adopt.add_argument("--force", action="store_true", help="lane 已存在时覆盖")

    p_remove = sub.add_parser("remove", help="从登记表移除一条 lane（不动安装树与 DSH_HOME）")
    p_remove.add_argument("lane")
    p_remove.add_argument("--force", action="store_true", help="即使正在运行也注销登记")

    p_primary = sub.add_parser(
        "primary", help="标记/查看主要版本（常用版本）：新建 lane 会继承它的 API key"
    )
    p_primary.add_argument("lane", nargs="?", help="lane 名；缺省则显示当前主要版本")
    p_primary.add_argument("--clear", action="store_true", help="清除主要版本标记")

    p_sync = sub.add_parser(
        "sync-key", help="把主要版本的 API key 补齐到已有 lane（新建时已自动复制一次）"
    )
    p_sync.add_argument("lane", nargs="*", help="lane 名；缺省＝全部")

    p_delete = sub.add_parser(
        "delete", help="彻底删除一套 DSH：登记 + 运行态 + 安装树 + DSH_HOME"
    )
    p_delete.add_argument("lane", help="要删除的 lane 名")
    p_delete.add_argument("--stop", action="store_true", help="正在运行就先停止它再删")
    p_delete.add_argument("--keep-home", action="store_true", help="保留 DSH_HOME（只删安装树与登记）")
    p_delete.add_argument("--keep-install", action="store_true", help="保留安装树")
    p_delete.add_argument(
        "--home-too",
        action="store_true",
        help="连 lane 根目录之外的 DSH_HOME（如 %%USERPROFILE%%\\.dsh）一起删（危险）",
    )
    p_delete.add_argument("--yes", action="store_true", help="跳过交互确认（GUI / 脚本用）")

    p_clone = sub.add_parser(
        "clone",
        help="把一条 lane 连安装树带 DSH_HOME 完整复制成新 lane（升级/插件冲突测试用，原件不动）",
    )
    p_clone.add_argument("source", help="源 lane 名或版本号（建议填主要版本）")
    p_clone.add_argument("lane", help="新 lane 名，例如 test-next")
    p_clone.add_argument("--port", type=int, help="指定端口（默认自动挑空闲端口）")

    p_upgrade = sub.add_parser(
        "upgrade",
        help="把某条 lane 精确升到指定版本（装完核对 + 启动冒烟，起不来自动退回）",
    )
    p_upgrade.add_argument("lane")
    p_upgrade.add_argument("version", nargs="?", help="目标版本或 dist-tag（如 0.1.7-rc.2 / latest）")
    p_upgrade.add_argument("--rollback", action="store_true", help="退回上一次升级前的版本")
    p_upgrade.add_argument("--stop", action="store_true",
                           help="正在运行就先停掉（默认拒绝在运行时升级；升完还给你起回来）")
    p_upgrade.add_argument("--no-boot-check", dest="no_boot_check", action="store_true",
                           help="跳过「升完启动一次」的冒烟验证（默认会做，起不来就自动退回）")
    p_upgrade.add_argument("--dry-run", dest="dry_run", action="store_true",
                           help="只显示会做什么，不动任何东西")

    p_verify = sub.add_parser(
        "verify", help="核对一条 lane 的隔离与内容（clone 之后、升级之后再各跑一次）"
    )
    p_verify.add_argument("lane")
    p_verify.add_argument(
        "--fix", action="store_true", help="按源 lane 重连宿主依赖农场（修复中断/不完整的复制）"
    )

    p_plugins = sub.add_parser(
        "plugins", help="列出某条 lane 的插件开关（插件 → 真实行 id → 关没关、谁能关它）"
    )
    p_plugins.add_argument("lane")
    p_plugins.add_argument("--dump", action="store_true",
                           help="额外离线组合一次，核对「文件说的」与「组合结果」是否一致")

    p_plugin_off = sub.add_parser(
        "plugin-off", help="关掉一个插件（写它的真实行 id；运行中的话约 2 秒热生效，不用重启）"
    )
    p_plugin_off.add_argument("lane")
    p_plugin_off.add_argument("target", help="插件名 / 行 id / 名字的一部分")
    p_plugin_off.add_argument("--verify", action="store_true", help="写完额外离线组合一次核对")

    p_plugin_on = sub.add_parser(
        "plugin-on", help="打开一个插件（删掉禁用条目；--force 可强行压过组合包自己的禁用）"
    )
    p_plugin_on.add_argument("lane")
    p_plugin_on.add_argument("target", help="插件名 / 行 id / 名字的一部分")
    p_plugin_on.add_argument("--force", action="store_true",
                             help="没有禁用条目时也写一条 disabled: false 强行打开（可能有副作用）")
    p_plugin_on.add_argument("--verify", action="store_true", help="写完额外离线组合一次核对")

    p_off = sub.add_parser(
        "plugins-off", help="一次关掉所有自己装的（第三方）插件；官方组合包不动，可 plugins-on 撤销"
    )
    p_off.add_argument("lane")
    p_off.add_argument("--keep", action="append", help="保留某个组合包（可多次，也可逗号分隔）")
    p_off.add_argument("--no-verify", dest="no_verify", action="store_true", help="跳过写入后的组合复核")
    p_off.add_argument(
        "--no-boot-check",
        dest="no_boot_check",
        action="store_true",
        help="跳过「禁用后启动一次」的冒烟验证（默认会做，起不来就自动撤销）",
    )

    p_on = sub.add_parser("plugins-on", help="撤销一键禁用（只删启动器写的那一段覆盖）")
    p_on.add_argument("lane")
    p_on.add_argument("--no-verify", dest="no_verify", action="store_true", help="跳过撤销后的组合复核")

    p_market = sub.add_parser(
        "market", help="插件市场（dshmarket）现状：装的是哪个版本、开还是关、registry 上有没有新版"
    )
    p_market.add_argument("lane")
    p_market.add_argument("--offline", action="store_true", help="不查 registry（离线看本地状态）")

    p_market_off = sub.add_parser(
        "market-off", help="单独关掉插件市场（其它插件一个都不动；market-on 撤销）"
    )
    p_market_off.add_argument("lane")
    p_market_off.add_argument("--no-verify", dest="no_verify", action="store_true",
                              help="跳过写入后的组合复核")
    p_market_off.add_argument("--no-boot-check", dest="no_boot_check", action="store_true",
                              help="跳过「关掉后启动一次」的冒烟验证（默认会做，起不来就自动还原）")

    p_market_on = sub.add_parser("market-on", help="再打开插件市场（只删启动器写的那一小段开关）")
    p_market_on.add_argument("lane")
    p_market_on.add_argument("--no-verify", dest="no_verify", action="store_true",
                             help="跳过撤销后的组合复核")

    p_market_up = sub.add_parser(
        "market-update",
        help="更新插件市场（点名到版本装 + 启动冒烟，起不来自动回滚到更新前的快照）",
    )
    p_market_up.add_argument("lane")
    p_market_up.add_argument("--version", help="目标版本（默认取 registry 上的 latest）")
    p_market_up.add_argument("--anyway", action="store_true",
                             help="连 minimumReleaseAgeStrict（点名版本也要拦）都硬闯："
                                  "带 pnpm 的一次性绕过 --config.minimum-release-age=0")
    p_market_up.add_argument("--force", action="store_true", help="这条 lane 正在运行也强行更新")
    p_market_up.add_argument("--offline", action="store_true", help="不查 registry（必须配 --version）")
    p_market_up.add_argument("--no-verify", dest="no_verify", action="store_true",
                             help="跳过更新后的组合复核")
    p_market_up.add_argument("--no-boot-check", dest="no_boot_check", action="store_true",
                             help="跳过更新后的启动冒烟（默认会做，起不来就自动回滚）")

    p_market_rb = sub.add_parser("market-rollback", help="用启动器自动存的快照把插件市场退回上一版")
    p_market_rb.add_argument("lane")
    p_market_rb.add_argument("--name", help="指定快照名（默认用最新的那份）")

    p_chat_bk = sub.add_parser(
        "chat-backup", help="把这条 lane 的对话记录（sessions）整份备份，并逐文件算指纹"
    )
    p_chat_bk.add_argument("lane")
    p_chat_bk.add_argument("--name", help="给这份快照起个后缀名，例如 before-upgrade")

    p_chat_ls = sub.add_parser("chat-list", help="列出某条 lane 的对话备份")
    p_chat_ls.add_argument("lane")

    p_chat_rs = sub.add_parser(
        "chat-restore", help="把对话记录恢复到某个快照（恢复前会先校验快照、再另存现状）"
    )
    p_chat_rs.add_argument("lane")
    p_chat_rs.add_argument("name", nargs="?", help="快照名（缺省＝最新那份）")
    p_chat_rs.add_argument("--stop", action="store_true", help="正在运行就先替你停掉它")
    p_chat_rs.add_argument("--yes", action="store_true", help="跳过确认（GUI / 脚本用）")
    p_chat_rs.add_argument("--force", action="store_true", help="快照自身校验不过也照恢复")

    p_chat_rm = sub.add_parser("chat-rm", help="删掉一份对话备份")
    p_chat_rm.add_argument("lane")
    p_chat_rm.add_argument("name")
    p_chat_rm.add_argument("--yes", action="store_true", help="跳过确认（GUI / 脚本用）")

    return parser


def main() -> int:
    setup_terminal()
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 0

    cfg = load_config()
    if getattr(args, "root", None):
        cfg["root"] = args.root

    handlers = {
        "doctor": cmd_doctor,
        "versions": cmd_versions,
        "create": cmd_create,
        "open": cmd_open,
        "list": cmd_list,
        "stop": cmd_stop,
        "logs": cmd_logs,
        "instances": cmd_instances,
        "adopt": cmd_adopt,
        "remove": cmd_remove,
        "primary": cmd_primary,
        "sync-key": cmd_sync_key,
        "delete": cmd_delete,
        "clone": cmd_clone,
        "upgrade": cmd_upgrade,
        "verify": cmd_verify,
        "plugins": cmd_plugins,
        "plugin-off": cmd_plugins_write_disabled,
        "plugin-on": cmd_plugins_write_disabled,
        "plugins-off": cmd_plugins_write_disabled,
        "plugins-on": cmd_plugins_write_disabled,
        "market": cmd_market,
        "market-off": cmd_market_off,
        "market-on": cmd_market_on,
        "market-update": cmd_market_update,
        "market-rollback": cmd_market_rollback,
        "chat-backup": cmd_chat_backup,
        "chat-list": cmd_chat_list,
        "chat-restore": cmd_chat_restore,
        "chat-rm": cmd_chat_rm,
    }
    try:
        return handlers[args.command](cfg, args)
    except KeyboardInterrupt:
        info("")
        return 130
    except Exception as exc:  # noqa: BLE001
        fail(f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        # 任何命令跑完都把 TCP 表缓存作废，别让下一次读拿到过期状态
        invalidate_tcp_cache()


if __name__ == "__main__":
    sys.exit(main())
