#!/usr/bin/env python3
"""CloudOps Bot 启动入口（薄封装）。

真正的实现在 ``cloudops/cli.py``，这样三种启动方式共享同一套逻辑：

    python main.py            # 克隆即用
    python -m cloudops        # 模块方式
    cloudops-bot              # pip install 后的 console script

参数说明见 ``python main.py --help``。
"""

from __future__ import annotations

import sys

from cloudops.cli import main

if __name__ == "__main__":
    sys.exit(main())
