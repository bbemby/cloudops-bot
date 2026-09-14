"""统一异常体系。

设计目标：让"业务层抛异常 → 交互层自动回吐可读消息"成为一条默认链路，
处理器里因此几乎不需要 ``try/except`` 拼字符串。

所有异常的文案都通过 i18n key 表达（见 ``cloudops.i18n``），
这样新增语言时无需改动业务代码。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


class CloudOpsError(Exception):
    """所有"预期内错误"的基类。

    :param message: 直接可展示的文案（优先级高于 ``key``）。
    :param key: i18n key，例如 ``"error.bad_arguments"``。
    :param params: 渲染 i18n 模板时的占位参数。
    """

    default_key = "error.internal"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        key: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.message = message
        self.key = key or self.default_key
        self.params: Dict[str, Any] = dict(params or {})
        super().__init__(message or self.key)

    def render(self, translate) -> str:
        """渲染为用户可见文案。``translate`` 形如 ``lambda key, **kw: str``。

        两个兜底保证"用户永远不会看到 i18n key 或未填充的占位符"：

        * 模板里需要 ``{detail}`` 而调用方没给（例如只抛了默认 key 的
          :class:`ValidationError`）时，用已有参数拼一句可读的说明；
        * key 在词表里不存在时，退回 ``error.generic``。
        """
        if self.message:
            return self.message
        params = dict(self.params)
        if "detail" not in params:
            params["detail"] = self.detail_text()
        rendered = translate(self.key, **params)
        if rendered == self.key:  # 没有该 key 的文案
            return translate("error.generic", detail=self.detail_text())
        return rendered

    def detail_text(self) -> str:
        """在没有 ``detail`` 参数时，用已有参数拼出人能看懂的一句话。"""
        value = self.params.get("value")
        parameter = self.params.get("parameter")
        options = self.params.get("options")
        if value is not None:
            head = f"{parameter} {value!r}" if parameter else repr(value)
            return f"{head} → 可选：{options}" if options else head
        if self.params:
            return " ".join(f"{key}={value}" for key, value in self.params.items())
        return self.message or self.key

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return self.message or self.key


class ConfigError(CloudOpsError):
    """配置缺失或非法（启动阶段）。"""

    default_key = "error.config"


class ValidationError(CloudOpsError):
    """用户输入非法。"""

    default_key = "error.bad_arguments"


class PermissionDenied(CloudOpsError):
    """权限校验不通过（论文用例 TC-04：越权操作拦截）。"""

    default_key = "error.permission_denied"


class RateLimited(CloudOpsError):
    """触发频率限制。"""

    default_key = "error.rate_limited"

    def __init__(self, retry_after: float, **kwargs: Any) -> None:
        super().__init__(params={"seconds": max(1, int(round(retry_after)))}, **kwargs)
        self.retry_after = retry_after


class NotFound(CloudOpsError):
    """目标资源不存在。"""

    default_key = "error.not_found"


class CredentialError(CloudOpsError):
    """凭证相关问题：缺失、失效、校验失败。"""

    default_key = "error.credential"


class CloudError(CloudOpsError):
    """云厂商 API 调用失败。

    :param provider: 云厂商标识（digitalocean / aws / mock ...）。
    :param status_code: HTTP 状态码，便于判断是否可重试。
    :param retryable: 是否属于瞬时故障（限流、5xx、网络抖动）。
    """

    default_key = "error.cloud"

    def __init__(
        self,
        message: Optional[str] = None,
        *,
        provider: Optional[str] = None,
        status_code: Optional[int] = None,
        retryable: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable


class CloudTimeout(CloudError):
    """等待云资源就绪超时（例如实例创建 10 分钟仍未拿到公网 IP）。"""

    default_key = "error.cloud_timeout"

    def __init__(self, message: Optional[str] = None, *, provider: Optional[str] = None,
                 params: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, provider=provider, params=params)


class ConfirmationRequired(CloudOpsError):
    """危险操作需要二次确认（论文 4.3：销毁云服务器需二次确认）。"""

    default_key = "error.confirmation_required"

    def __init__(self, code: str, action: str, ttl_seconds: int) -> None:
        super().__init__(
            params={"code": code, "action": action, "ttl": ttl_seconds},
            key="confirm.required",
        )
        self.code = code
        self.action = action


class ConfirmationInvalid(CloudOpsError):
    """确认码无效或已过期。"""

    default_key = "confirm.invalid"


class JobCancelled(CloudOpsError):
    """后台任务被取消。"""

    default_key = "error.job_cancelled"
