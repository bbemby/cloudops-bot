"""AWS Signature Version 4 签名实现（论文 2.2：SigV4 认证机制）。

为什么手写而不依赖 botocore？

* 论文 2.3 要求运行环境极简，botocore + boto3 会给 Alpine 镜像增加十几 MB
  的依赖；而 SigV4 本身只是一段 HMAC 链，用标准库 100 行内即可实现；
* 论文 2.2 也明确给出了"封装 Boto3 核心逻辑**或直接构建鉴权头**"两条路径，
  本项目选择后者，并把签名逻辑做成可独立测试的纯函数。

正确性由 AWS 官方签名测试套件（``tests/data/sigv4``，vendored）与
botocore 交叉校验（可选依赖）双重保证。
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Dict, Mapping, Optional, Tuple

ALGORITHM = "AWS4-HMAC-SHA256"
_UNRESERVED_SAFE = "-_.~"
_EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class AwsCredentials:
    """AWS 访问凭证（长期 AK/SK 或 STS 临时凭证）。"""

    access_key_id: str
    secret_access_key: str
    session_token: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("AWS 凭证缺少 access_key_id / secret_access_key")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """RFC 3986 编码：保留 ``A-Za-z0-9-_.~``，其余百分号编码。"""
    safe = _UNRESERVED_SAFE if encode_slash else _UNRESERVED_SAFE + "/"
    return urllib.parse.quote(str(value), safe=safe)


def canonical_query_string(params: Optional[Mapping[str, Any]]) -> str:
    """按 key、value 字典序排序并编码的查询串。"""
    if not params:
        return ""
    pairs = []
    for key, value in params.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            pairs.append((uri_encode(key), uri_encode(item)))
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def _normalize_header_value(value: str) -> str:
    """折叠多余空白（AWS 规范：连续空格归一为单个空格）。"""
    return " ".join(str(value).split())


def canonical_headers(headers: Mapping[str, Any]) -> Tuple[str, str]:
    """返回 (规范化头部文本, 参与签名的头名列表)。"""
    normalized = {str(key).lower().strip(): _normalize_header_value(value)
                  for key, value in headers.items()}
    normalized.pop("authorization", None)  # 重新签名时不参与
    names = sorted(normalized)
    text = "".join(f"{name}:{normalized[name]}\n" for name in names)
    return text, ";".join(names)


def build_canonical_request(method: str, path: str, query: str, headers: Mapping[str, Any],
                            payload_hash: str) -> Tuple[str, str]:
    """构造 CanonicalRequest，返回 (文本, signed_headers)。"""
    canonical_uri = uri_encode(path or "/", encode_slash=False) or "/"
    header_text, signed_headers = canonical_headers(headers)
    request = "\n".join([
        method.upper(),
        canonical_uri,
        query,
        header_text,
        signed_headers,
        payload_hash,
    ])
    return request, signed_headers


def derive_signing_key(secret_access_key: str, datestamp: str, region: str, service: str) -> bytes:
    """kSecret → kDate → kRegion → kService → kSigning。"""
    key = ("AWS4" + secret_access_key).encode("utf-8")
    key = hmac.new(key, datestamp.encode("utf-8"), hashlib.sha256).digest()
    key = hmac.new(key, region.encode("utf-8"), hashlib.sha256).digest()
    key = hmac.new(key, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(key, b"aws4_request", hashlib.sha256).digest()


def build_string_to_sign(amz_date: str, scope: str, canonical_request: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, sha256_hex(canonical_request.encode("utf-8"))])


def sign_request(*, method: str, url: str, region: str, service: str,
                 credentials: AwsCredentials, headers: Optional[Mapping[str, Any]] = None,
                 body: Any = b"", amz_date: Optional[str] = None,
                 content_sha256_header: bool = True) -> Dict[str, str]:
    """对请求签名，返回应当随请求发送的完整头部字典。

    签名覆盖的头部集合 = 传入 headers ∪ {host, x-amz-date}
    ∪ {x-amz-content-sha256}（``content_sha256_header=True``，EC2 Query API 允许携带）
    ∪ {x-amz-security-token}（临时凭证时）。

    ``content_sha256_header=False`` 用于与 AWS 官方签名测试套件
    （``tests/test_sigv4.py`` 的已知答案用例）逐字节对齐——那些用例的
    SignedHeaders 只有 ``host;x-amz-date``。
    """
    parsed = urllib.parse.urlsplit(url)
    payload = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    payload_hash = sha256_hex(payload)

    request_headers: Dict[str, Any] = {"host": parsed.netloc}
    if headers:
        request_headers.update({str(key): value for key, value in headers.items()
                                if str(key).lower() not in ("host", "authorization")})

    timestamp = (
        amz_date
        or request_headers.get("x-amz-date")
        or request_headers.get("X-Amz-Date")
        or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    )
    amz_date = str(timestamp)
    request_headers["x-amz-date"] = amz_date
    if content_sha256_header:
        request_headers.setdefault("x-amz-content-sha256", payload_hash)
    if credentials.session_token:
        request_headers.setdefault("x-amz-security-token", credentials.session_token)

    query = canonical_query_string(dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)))
    canonical_request, signed_headers = build_canonical_request(
        method, parsed.path, query, request_headers, payload_hash
    )

    datestamp = amz_date[:8]
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    string_to_sign = build_string_to_sign(amz_date, scope, canonical_request)
    signing_key = derive_signing_key(credentials.secret_access_key, datestamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    result: Dict[str, str] = {
        str(key): str(value) for key, value in request_headers.items()
    }
    result["Authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return result


class SigV4Signer:
    """面向单个 (region, service) 的签名器，便于在客户端里复用。"""

    def __init__(self, credentials: AwsCredentials, region: str, service: str) -> None:
        self.credentials = credentials
        self.region = region
        self.service = service

    def sign(self, method: str, url: str, *, headers: Optional[Mapping[str, Any]] = None,
             body: Any = b"", amz_date: Optional[str] = None,
             content_sha256_header: bool = True) -> Dict[str, str]:
        return sign_request(
            method=method,
            url=url,
            region=self.region,
            service=self.service,
            credentials=self.credentials,
            headers=headers,
            body=body,
            amz_date=amz_date,
            content_sha256_header=content_sha256_header,
        )
