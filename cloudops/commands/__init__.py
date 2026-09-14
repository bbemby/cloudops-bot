"""``cloudops.commands`` —— 业务逻辑层的指令处理器集合。

:func:`register_all` 把所有处理器挂到 :class:`~cloudops.bot.dispatcher.Dispatcher` 上，
新增一组指令只需在此处多 import 一个模块。
"""

from __future__ import annotations

from typing import Any

from . import basic, credentials, instances, lifecycle

MODULES = (basic, instances, lifecycle, credentials)


def register_all(dispatcher: Any) -> Any:
    """注册全部内置指令，返回 dispatcher 以便链式调用。"""
    for module in MODULES:
        module.register(dispatcher)
    return dispatcher
