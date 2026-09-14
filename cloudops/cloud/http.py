"""云厂商适配器的公共 HTTP 客户端。

统一处理：超时、重试（含 429 限流与 5xx 抖动）、User-Agent、错误归一化，
让每个适配器只关心"参数怎么拼、响应怎么解"。
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import requests

from ..errors import CloudError

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpClient:
    """带退避重试的 requests 封装。"""

    def __init__(self, *, provider: str, user_agent: str, timeout: float = 30.0,
                 retries: int = 3, proxy: Optional[str] = None,
                 session: Optional[requests.Session] = None) -> None:
        self.provider = provider
        self.timeout = timeout
        self.retries = max(0, retries)
        self.user_agent = user_agent
        self._session = session or requests.Session()
        if proxy:
            self._session.proxies.update({"http": proxy, "https": proxy})

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        try:
            self._session.close()
        except Exception:  # pragma: no cover
            pass

    def _sleep(self, attempt: int, response: Optional[requests.Response] = None) -> None:
        """指数退避 + 抖动；若服务端给出 Retry-After 则优先遵守。"""
        delay = min(8.0, 0.6 * (2 ** attempt)) + random.uniform(0, 0.4)
        if response is not None:
            header = response.headers.get("Retry-After") or response.headers.get("retry-after")
            if header:
                try:
                    delay = max(delay, min(30.0, float(header)))
                except (TypeError, ValueError):
                    pass
        time.sleep(delay)

    def request(self, method: str, url: str, *, headers: Optional[Mapping[str, str]] = None,
                params: Optional[Mapping[str, Any]] = None, json_body: Any = None,
                data: Any = None, expect: Optional[Iterable[int]] = None,
                allow_status: Sequence[int] = ()) -> requests.Response:
        """发起请求；非预期状态码会抛出 :class:`CloudError`。

        :param expect: 视为成功的状态码集合，默认 2xx。
        :param allow_status: 额外允许（不抛异常）的状态码，由调用方自行解释响应。
        """
        expected = set(expect) if expect is not None else None
        allowed = set(allow_status)
        request_headers: Dict[str, str] = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update({k: v for k, v in headers.items()})

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            response: Optional[requests.Response] = None
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json_body,
                    data=data,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_error = exc
                if attempt < self.retries:
                    self._sleep(attempt)
                    continue
                raise CloudError(
                    f"{self.provider} 接口请求超时（{self.timeout:.0f}s）：{url}",
                    provider=self.provider, retryable=True,
                ) from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    self._sleep(attempt)
                    continue
                raise CloudError(
                    f"{self.provider} 网络异常：{exc}",
                    provider=self.provider, retryable=True,
                ) from exc

            status = response.status_code
            if status in RETRY_STATUS and attempt < self.retries:
                self._sleep(attempt, response)
                continue
            if expected is not None and status not in expected and status not in allowed:
                raise CloudError(
                    self._describe_error(response),
                    provider=self.provider,
                    status_code=status,
                    retryable=status in RETRY_STATUS,
                )
            if expected is None and not (200 <= status < 300) and status not in allowed:
                raise CloudError(
                    self._describe_error(response),
                    provider=self.provider,
                    status_code=status,
                    retryable=status in RETRY_STATUS,
                )
            return response

        raise CloudError(  # pragma: no cover - 循环内已抛错，此处兜底
            f"{self.provider} 请求失败：{last_error}", provider=self.provider, retryable=True
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _describe_error(response: requests.Response) -> str:
        """把云厂商的错误响应转成单行可读信息。"""
        detail = ""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            errors = payload.get("errors") or payload.get("error") or payload.get("message")
            if isinstance(errors, list) and errors:
                detail = "; ".join(
                    item.get("message", str(item)) if isinstance(item, dict) else str(item)
                    for item in errors
                )
            elif isinstance(errors, dict):
                detail = errors.get("message") or str(errors)
            elif isinstance(errors, str):
                detail = errors
            elif payload.get("Message"):
                detail = str(payload["Message"])
        if not detail:
            detail = HttpClient._describe_xml_error(response)
        if not detail:
            detail = (response.text or "").strip()[:300]
        return f"HTTP {response.status_code}: {detail or '无响应体'}"

    # ------------------------------------------------------------------ #
    @staticmethod
    def _describe_xml_error(response: requests.Response) -> str:
        """AWS 查询协议的错误体是 XML（``<Error><Code/><Message/></Error>``）。"""
        text = (response.text or "").lstrip()
        if not text.startswith("<"):
            return ""
        try:
            import xml.etree.ElementTree as ET

            root = ET.fromstring(response.content)
        except Exception:
            return ""
        code = root.findtext(".//Code") or root.findtext(".//code") or ""
        message = root.findtext(".//Message") or root.findtext(".//message") or ""
        combined = " ".join(part for part in (code, message) if part).strip()
        return combined[:300]

    @staticmethod
    def json(response: requests.Response) -> Any:
        """解析 JSON，失败时抛出 CloudError。"""
        try:
            return response.json()
        except ValueError as exc:
            raise CloudError(
                f"响应不是合法 JSON（HTTP {response.status_code}）",
                provider="http",
                status_code=response.status_code,
            ) from exc
