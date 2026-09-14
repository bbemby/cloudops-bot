"""CloudOps Bot —— 基于即时通讯机器人的多云自动化运维系统。

模块划分（对应论文 5.1 总体架构设计的三层结构）：

* ``cloudops.bot``      —— 交互层：Telegram Bot 长轮询客户端、指令路由、消息格式化
* ``cloudops.commands`` —— 业务逻辑层：权限校验、指令处理器、多云调度编排
* ``cloudops.cloud``    —— 数据与接口层：各云厂商 API 适配器 + 工厂模式调度引擎
* ``cloudops.db``       —— 数据与接口层：SQLite 持久化（用户 / 凭证 / 操作日志）

author: CloudOps Bot contributors
license: MIT
"""

__version__ = "1.0.0"
__project__ = "cloudops-bot"
__homepage__ = "https://github.com/bbemby/cloudops-bot"

__all__ = ["__version__", "__project__", "__homepage__"]
