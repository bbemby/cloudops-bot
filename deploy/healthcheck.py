#!/usr/bin/env python3
"""容器健康检查（Dockerfile 的 ``HEALTHCHECK`` 调用）。

只检查本机状态，不打外部 API —— 云厂商抖动不该导致容器被反复重启。
判定为健康的条件：

1. ``DATABASE_PATH`` 指向的文件存在；
2. 它是一个能只读打开的 SQLite 库，且已建表（``schema_meta`` 可查询）；
3. 若 ``WEB_ENABLED`` 不是 false，则本机管理面板要能应答 ``/healthz``
   （只看"端口通不通、进程活没活"，不校验口令，也不泄漏任何状态）。

失败时把原因写到 stderr 并退出 1，``docker inspect`` 的 ``State.Health``
里能看到这条输出，便于判断是"卷没挂上""库损坏"还是"面板没起来"。

用法::

    python deploy/healthcheck.py          # 退出码 0 健康 / 1 不健康
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

DEFAULT_PATH = "/app/data/cloudops.db"
DEFAULT_PANEL_PORT = 9878
FALSEY = {"0", "false", "no", "off", ""}


def check_database() -> int:
    path = os.environ.get("DATABASE_PATH") or DEFAULT_PATH

    if not os.path.exists(path):
        print(f"unhealthy: 数据库文件不存在：{path}"
              "（数据卷是否挂载到 /app/data？）", file=sys.stderr)
        return 1

    try:
        # mode=ro 保证健康检查本身不会创建/修改文件
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        try:
            row = connection.execute("SELECT 1 FROM schema_meta LIMIT 1").fetchone()
        finally:
            connection.close()
    except sqlite3.DatabaseError as exc:
        print(f"unhealthy: 数据库无法读取：{path}（{exc}）", file=sys.stderr)
        return 1

    if row is None:
        print(f"unhealthy: 数据库尚未初始化（schema_meta 为空）：{path}", file=sys.stderr)
        return 1
    return 0


def check_panel() -> int:
    """面板存活探测：容器内走 127.0.0.1，避免依赖 WEB_HOST 的绑定地址。"""
    host = os.environ.get("WEB_HEALTH_HOST", "127.0.0.1")
    port = os.environ.get("WEB_PORT") or DEFAULT_PANEL_PORT
    url = f"http://{host}:{port}/healthz"
    request = urllib.request.Request(url, headers={"User-Agent": "cloudops-healthcheck/1"})
    try:
        with urllib.request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"unhealthy: 管理面板无响应 {url}（{exc}）"
              "（面板没起来？端口映射写错？还是 WEB_ENABLED=false 忘了配？）", file=sys.stderr)
        return 1
    if payload.get("status") != "ok":
        print(f"unhealthy: 管理面板 /healthz 返回异常：{payload}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    database = check_database()
    if database:
        return database
    if os.environ.get("WEB_ENABLED", "true").strip().lower() in FALSEY:
        return 0
    return check_panel()


if __name__ == "__main__":
    raise SystemExit(main())
