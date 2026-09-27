#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
启动器自检（selftest）
=====================
对已登记的 lane 依次做：open --detach → **认证握手**（token → cookie → 200）→ stop。

为什么握手要带 cookie jar
------------------------
`dsh-client-connection` 的规则是：根路径带正确 token 的 GET 会下发 cookie 并 302 到 `./`，
此后靠 cookie 放行，否则一律 401。所以**不带 cookie jar 的裸请求必然 401**，
不能拿它当"服务没起来"的证据。这个探针同时验证：进程活着、token 是本次的、静态资源可服务。

用法
----
    py selftest.py                # 自检全部已登记 lane
    py selftest.py next stable    # 只自检指定 lane
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "dsh_lanes.py"
CONFIG = HERE / "lanes.json"


def load_lanes() -> tuple[dict, Path]:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    root = Path(cfg.get("root") or "D:/dsh-lanes")
    return cfg.get("lanes", {}), root


def run(args: list[str], timeout: int = 600) -> int:
    print(f"\n$ py dsh_lanes.py {' '.join(args)}", flush=True)
    p = subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if p.stdout.strip():
        print(p.stdout.rstrip(), flush=True)
    if p.stderr.strip():
        print("STDERR:\n" + p.stderr.rstrip(), flush=True)
    print(f"[exit {p.returncode}]", flush=True)
    return p.returncode


def probe(url: str, timeout: float = 45.0) -> tuple[bool, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    deadline = time.time() + timeout
    last = "未知"
    while time.time() < deadline:
        try:
            with opener.open(url, timeout=8) as resp:
                if resp.status == 200 and len(jar) > 0:
                    return True, f"HTTP {resp.status} + cookie → {resp.url}"
                last = f"HTTP {resp.status}（未下发 cookie）"
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.8)
    return False, last


def check(lane: str, root: Path) -> bool:
    print("=" * 72, flush=True)
    print(f"### lane={lane}", flush=True)
    rc = run(["open", lane, "--detach", "--no-browser"])
    if rc != 0:
        return False
    try:
        data = json.loads((root / "run" / f"{lane}.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"读运行态失败：{exc}", flush=True)
        return False
    url = data.get("url", "")
    print(f"运行态: port={data.get('port')} pid={data.get('pid')} url={url}", flush=True)
    if "token=" not in url:
        print("URL 缺少 token —— 抓取逻辑可能抓到了旧日志行", flush=True)
        run(["stop", lane])
        return False
    good, detail = probe(url)
    print(f"握手结果: {detail}", flush=True)
    # 再开一次应当识别"已在运行"，而不是起第二个进程
    run(["open", lane, "--detach", "--no-browser"])
    run(["stop", lane])
    return bool(good)


def main() -> int:
    lanes, root = load_lanes()
    targets = sys.argv[1:] or list(lanes.keys())
    if not targets:
        print("lanes.json 里还没有 lane；先 create 一条。", flush=True)
        return 1
    results = {lane: check(lane, root) for lane in targets}
    print("\n>>> 汇总: " + json.dumps(results, ensure_ascii=False), flush=True)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
