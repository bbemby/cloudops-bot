#!/usr/bin/env python3
"""容器健康检查（Dockerfile 的 ``HEALTHCHECK`` 调用）。

只检查本机状态，不打外部 API —— 云厂商抖动不该导致容器被反复重启。
判定为健康的条件：

1. ``DATABASE_PATH`` 指向的文件存在；
2. 它是一个能只读打开的 SQLite 库，且已建表（``schema_meta`` 可查询）。

失败时把原因写到 stderr 并退出 1，``docker inspect`` 的 ``State.Health``
里能看到这条输出，便于判断是"卷没挂上"还是"库损坏"。

用法::

    python deploy/healthcheck.py          # 退出码 0 健康 / 1 不健康
"""

from __future__ import annotations

import os
import sqlite3
import sys

DEFAULT_PATH = "/app/data/cloudops.db"


def main() -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
