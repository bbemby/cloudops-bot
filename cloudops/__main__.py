"""``python -m cloudops`` 入口：等价于 ``python main.py``。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
