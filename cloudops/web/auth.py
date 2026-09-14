"""Web 面板的认证与会话。

设计取舍（面板能改凭证、能销毁机器，认证不能马虎，但也不想为此引入
Flask-Login + itsdangerous 这类依赖，容器里少一个包就少一份 CVE 面）：

* **口令**用 ``hashlib.scrypt`` 存哈希（自带随机盐），比对走 ``compare_digest``，
  不存明文、不做可逆加密；
* **会话**是一个 HMAC-SHA256 签名的 JSON（密钥从 ``SECRET_KEY`` 派生，
  带域分隔前缀），浏览器只拿到签名后的值，改一个字节就会失效；
* **口令变更即失效**：会话里记着口令指纹，换掉 ``WEB_ADMIN_PASSWORD`` 后
  旧 cookie 立即作废，不需要额外的会话表；
* **CSRF**：每个会话带一个随机 token，写操作必须回填，避免"面板开着时
  被别的页面带着 cookie 打接口"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote

#: 会话 cookie 名
COOKIE_NAME = "cloudops_session"

#: CSRF 校验时接受的请求头（表单回填字段名为 csrf_token）
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"

#: scrypt 参数：n=2**14 在容器里约 30ms，兼顾抗爆破与启动开销
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

#: 派生会话签名密钥时的域分隔串（与加密凭证的密钥用途区分开）
_KEY_CONTEXT = b"cloudops-web-session-v1"


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """把口令哈希成 ``scrypt$n$r$p$salt$hash`` 形式的字符串。"""
    if not password:
        raise ValueError("口令不能为空")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                            r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return "$".join([
        "scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    """校验口令（任何格式问题都视为不通过，不抛异常）。"""
    if not password or not stored:
        return False
    try:
        algorithm, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n),
                                r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def password_fingerprint(stored_hash: str) -> str:
    """口令指纹：进会话，用于"换口令即踢下线"。"""
    return hashlib.sha256(stored_hash.encode("utf-8")).hexdigest()[:16]


def generate_password(length: int = 16) -> str:
    """生成一个人工可抄的口令（去掉容易混淆的 0/O/1/l/I）。"""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(max(8, length)))


@dataclass
class Session:
    """一次已认证的会话。"""

    subject: str
    csrf_token: str
    expires_at: float
    raw: str

    @property
    def remaining(self) -> float:
        return max(0.0, self.expires_at - time.time())


class SessionManager:
    """签发/校验会话 cookie。"""

    def __init__(self, secret_key: str, *, ttl: int = 8 * 3600,
                 secure: bool = False, cookie_name: str = COOKIE_NAME,
                 subject: str = "admin") -> None:
        if not secret_key:
            raise ValueError("SECRET_KEY 不能为空（会话签名依赖它）")
        self._key = hmac.new(secret_key.encode("utf-8"), _KEY_CONTEXT,
                             hashlib.sha256).digest()
        self.ttl = max(60, int(ttl))
        self.secure = secure
        self.cookie_name = cookie_name
        self.subject = subject

    # -- 签名 ------------------------------------------------------------ #
    def _sign(self, payload: bytes) -> str:
        signature = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")

    def issue(self, stored_hash: str) -> Session:
        """签发新会话（登录成功时调用）。"""
        expires_at = time.time() + self.ttl
        csrf = secrets.token_urlsafe(24)
        body: Dict[str, Any] = {
            "v": 1,
            "sub": self.subject,
            "exp": int(expires_at),
            "pv": password_fingerprint(stored_hash),
            "csrf": csrf,
        }
        payload = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        raw = f"{token}.{self._sign(payload)}"
        return Session(subject=self.subject, csrf_token=csrf, expires_at=expires_at, raw=raw)

    def verify(self, raw: Optional[str], stored_hash: str) -> Optional[Session]:
        """校验 cookie；任何不合规都返回 ``None``（不区分原因，避免喂信息给探测者）。"""
        if not raw or "." not in raw:
            return None
        token, _, signature = raw.partition(".")
        try:
            padding = "=" * (-len(token) % 4)
            payload = base64.urlsafe_b64decode(token + padding)
        except (ValueError, TypeError):
            return None
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        try:
            body = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict) or body.get("v") != 1:
            return None
        if body.get("sub") != self.subject:
            return None
        if int(body.get("exp", 0)) < time.time():
            return None
        if not hmac.compare_digest(str(body.get("pv", "")), password_fingerprint(stored_hash)):
            return None                      # 口令被改过，旧会话作废
        return Session(subject=self.subject, csrf_token=str(body.get("csrf", "")),
                       expires_at=float(body.get("exp", 0)), raw=raw)

    # -- 浏览器侧 -------------------------------------------------------- #
    def set_cookie_header(self, session: Session) -> str:
        parts = [
            f"{self.cookie_name}={session.raw}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={self.ttl}",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    def clear_cookie_header(self) -> str:
        parts = [f"{self.cookie_name}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def read_cookie(cookie_header: Optional[str], name: str = COOKIE_NAME) -> Optional[str]:
        """从 ``Cookie:`` 头里取出指定 cookie（容错解析，不引 CookieJar）。"""
        if not cookie_header:
            return None
        for chunk in cookie_header.split(";"):
            key, _, value = chunk.strip().partition("=")
            if key == name:
                return value or None
        return None

    @staticmethod
    def quote(value: str) -> str:
        return quote(value, safe="")


class LoginThrottle:
    """登录失败节流：同一来源在窗口内失败次数超限后拒绝，成功则清零。"""

    def __init__(self, *, max_attempts: int = 5, window: float = 300.0) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.window = max(1.0, float(window))
        self._failures: Dict[str, list] = {}

    def _prune(self, key: str, now: float) -> list:
        stamps = [stamp for stamp in self._failures.get(key, []) if now - stamp < self.window]
        self._failures[key] = stamps
        return stamps

    def blocked(self, key: str) -> float:
        """返回还需等待的秒数（0 表示可以继续尝试）。"""
        now = time.time()
        stamps = self._prune(key, now)
        if len(stamps) < self.max_attempts:
            return 0.0
        return max(0.0, self.window - (now - stamps[0]))

    def record_failure(self, key: str) -> None:
        now = time.time()
        stamps = self._prune(key, now)
        stamps.append(now)
        self._failures[key] = stamps

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
