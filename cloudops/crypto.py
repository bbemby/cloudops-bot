"""凭证加密（论文 4.2：凭证与环境管理需"安全存储"）。

采用 ``cryptography`` 的 Fernet（AES-128-CBC + HMAC-SHA256，加密并认证）：

* 若 ``SECRET_KEY`` 本身是合法 Fernet key（44 字符 base64），直接使用；
* 若用户填的是任意口令，则用 HKDF-SHA256 从口令派生 32 字节密钥 —— 便于
  用户"随便写个字符串就能跑"，同时保留升级为正式密钥的路径。

明文凭证永远不会写入数据库或日志，只在内存中短暂存在。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import ConfigError
from .utils import dumps, loads

# 口令派生用的固定 salt（升级派生算法时需同步修改版本号）
_HKDF_SALT = b"cloudops-bot/credential-encryption/v1"
_HKDF_INFO = b"cloudops-bot"


def is_fernet_key(value: str) -> bool:
    """判断字符串是否是合法的 Fernet key。"""
    if not value or len(value) != 44:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.encode("utf-8"))
    except Exception:
        return False
    return len(decoded) == 32


def derive_key(passphrase: str) -> bytes:
    """从任意口令派生 Fernet key。"""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=_HKDF_SALT, info=_HKDF_INFO)
    return base64.urlsafe_b64encode(hkdf.derive(passphrase.encode("utf-8")))


def generate_key() -> str:
    """生成一个新的 Fernet key（``python manage.py gen-secret``）。"""
    return Fernet.generate_key().decode("utf-8")


def fingerprint(secret: str) -> str:
    """凭证指纹，用于日志中区分不同密钥而不泄露内容。"""
    if not secret:
        return "-"
    digest = hmac.new(b"cloudops-fingerprint", secret.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:8]


class SecretBox:
    """凭证加解密器。"""

    def __init__(self, secret: str, *, strict: bool = False) -> None:
        if not secret:
            raise ConfigError(
                "缺少 SECRET_KEY，请先执行 `python manage.py gen-secret` 生成后写入 .env",
                key="error.missing_secret_key",
            )
        if is_fernet_key(secret):
            key = secret.encode("utf-8")
        else:
            if strict:
                raise ConfigError(
                    "SECRET_KEY 不是合法的 Fernet key（应为 44 字符 base64）",
                    key="error.invalid_secret_key",
                )
            key = derive_key(secret)
        self._fernet = Fernet(key)
        self._key_source = "fernet" if is_fernet_key(secret) else "passphrase"
        self.fingerprint = fingerprint(secret)

    @property
    def key_source(self) -> str:
        return self._key_source

    # ------------------------------------------------------------------ #
    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:  # pragma: no cover - 依赖运行时数据
            raise ConfigError(
                "凭证解密失败：SECRET_KEY 可能已变更，请重新绑定云平台凭证",
                key="error.decrypt_failed",
            ) from exc

    def encrypt_dict(self, payload: Dict[str, Any]) -> str:
        """凭证（如 AWS 的 ak/sk/region）整体加密为单列文本。"""
        return self.encrypt(dumps(payload))

    def decrypt_dict(self, token: str) -> Dict[str, Any]:
        data = loads(self.decrypt(token), default={})
        if not isinstance(data, dict):
            raise ConfigError("凭证数据损坏：期望 JSON 对象", key="error.decrypt_failed")
        return data


def build_secret_box(secret: Optional[str], *, strict: bool = False) -> SecretBox:
    return SecretBox(secret or "", strict=strict)
