"""Web 管理面板（论文之外的扩展：给 ChatOps 补一个图形入口）。

模块划分：

* :mod:`cloudops.web.auth` —— 口令哈希、会话签名、CSRF 与登录限速；
* :mod:`cloudops.web.service` —— 业务门面（凭证、实例、用户、审计）；
* :mod:`cloudops.web.server` —— aiohttp 路由与安全中间件。
"""

from __future__ import annotations

from .auth import LoginThrottle, Session, SessionManager, hash_password, verify_password
from .server import WebServer
from .service import PanelService

__all__ = [
    "LoginThrottle",
    "PanelService",
    "Session",
    "SessionManager",
    "WebServer",
    "hash_password",
    "verify_password",
]
