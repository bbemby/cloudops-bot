"""日志初始化。

默认输出到 stdout（容器场景由 Docker 收集），可选同时写文件并做滚动，
避免长时间运行时把磁盘写满。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: Optional[Path] = None, *,
                  quiet_libraries: bool = True) -> None:
    """配置根日志器。"""
    handlers: list = [logging.StreamHandler(stream=sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=LOG_FORMAT,
        datefmt=DATE_FORMAT,
        handlers=handlers,
        force=True,
    )
    if quiet_libraries:
        for name in ("aiohttp.access", "urllib3", "asyncio"):
            logging.getLogger(name).setLevel(logging.WARNING)
