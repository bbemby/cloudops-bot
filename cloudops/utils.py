"""通用工具：时间、脱敏、文本切分、.env 解析等。

刻意只依赖标准库，保持 Alpine 镜像的精简（论文 2.3 轻量级运行环境）。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import string
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """当前 UTC 时间（带 tzinfo）。"""
    return datetime.now(UTC)


def to_iso(value: Optional[datetime] = None) -> str:
    """序列化为数据库存储用的 ISO8601（秒级精度 + Z）。"""
    value = value or now_utc()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    """当前 UTC 时间的 ISO8601 串（数据库写入用，等价 ``to_iso()``）。"""
    return to_iso()


def parse_iso(text: Optional[str]) -> Optional[datetime]:
    """解析 ISO8601 / RFC3339 字符串，失败返回 ``None``。"""
    if not text:
        return None
    raw = text.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def get_timezone(name: Optional[str]):
    """按名称取时区，取不到则回退 UTC。"""
    if not name or ZoneInfo is None:
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:
        return UTC


def format_local(text: Optional[str], tz_name: Optional[str] = None,
                 fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把数据库里的 UTC 时间串转换为展示用时区字符串。"""
    parsed = parse_iso(text)
    if parsed is None:
        return "-"
    return parsed.astimezone(get_timezone(tz_name)).strftime(fmt)


def humanize_age(text: Optional[str], tz_name: Optional[str] = None) -> str:
    """把时间串渲染成"N 分钟前"这类相对描述。"""
    parsed = parse_iso(text)
    if parsed is None:
        return "-"
    delta = now_utc() - parsed
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    if seconds < 86400 * 30:
        return f"{seconds // 86400} 天前"
    return format_local(text, tz_name, "%Y-%m-%d")


def humanize_duration(seconds: float) -> str:
    """把秒数渲染为紧凑时长，例如 ``1h02m`` / ``45s``。"""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m"


# --------------------------------------------------------------------------- #
# 脱敏 / 文本
# --------------------------------------------------------------------------- #
def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """凭证脱敏展示：``wJalrXUtnFEMI...EXAMPLEKEY`` → ``wJal****KEY``。"""
    if not value:
        return "-"
    text = str(value)
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}{'*' * 4}{text[-keep:]}"


def mask_mapping(values: Dict[str, Any], secret_fields: Sequence[str] = ()) -> Dict[str, str]:
    """按字段名决定是否脱敏。"""
    result: Dict[str, str] = {}
    for key, value in values.items():
        result[key] = mask_secret(str(value)) if key in secret_fields else str(value)
    return result


def truncate(text: Optional[str], limit: int = 120, suffix: str = "…") -> str:
    if not text:
        return ""
    text = str(text).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - len(suffix)] + suffix


def split_message(text: str, limit: int = 3800) -> List[str]:
    """按 Telegram 4096 字上限切分长消息，尽量在换行处断开。"""
    if not text:
        return [""]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    chunks.append(remaining)
    return chunks


def unique(items: Iterable[Any]) -> List[Any]:
    """去重且保持原顺序。"""
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def random_code(length: int = 6) -> str:
    """生成易读的确认码（剔除易混淆字符 0/O/1/I）。"""
    alphabet = "ACDEFGHJKLMNPQRTUVWXY3456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def random_suffix(length: int = 4) -> str:
    """实例名后缀。"""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def looks_like_ipv4(text: str) -> bool:
    """判断字符串是否形如 IPv4 地址。"""
    if not text:
        return False
    parts = text.strip().split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 255:
            return False
    return True


def looks_like_ipv6(text: str) -> bool:
    if not text or ":" not in text:
        return False
    try:
        import ipaddress

        ipaddress.IPv6Address(text.strip())
        return True
    except Exception:
        return False


def is_ip_address(text: str) -> bool:
    return looks_like_ipv4(text) or looks_like_ipv6(text)


def dumps(data: Any) -> str:
    """稳定的 JSON 序列化（用于凭证与任务负载）。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: Optional[str], default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return default


# --------------------------------------------------------------------------- #
# .env 解析（替代 python-dotenv，少一个依赖）
# --------------------------------------------------------------------------- #
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def parse_dotenv(text: str) -> Dict[str, str]:
    """解析 .env 文本，支持引号、行内注释与 ``export`` 前缀。"""
    values: Dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if value and value[0] in ("'", '"') and len(value) >= 2:
            quote = value[0]
            end = value.rfind(quote)
            value = value[1:end] if end > 0 else value[1:]
            # 双引号内的内容支持 \n 等转义
            if quote == '"':
                value = value.encode("utf-8").decode("unicode_escape")
        elif " #" in value:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def load_dotenv_file(path: Path, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """把 .env 载入进程环境；已存在的环境变量优先（容器场景更符合直觉）。"""
    environ = environ if environ is not None else os.environ
    if not path.exists():
        return {}
    try:
        parsed = parse_dotenv(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    applied: Dict[str, str] = {}
    for key, value in parsed.items():
        if key in environ and environ[key] != "":
            continue
        environ[key] = value
        applied[key] = value
    return applied


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


_SAFE_TOKENS = re.compile(r"(?i)(token|secret|password|passwd|access_key|apikey|api_key)")


def redact_mapping(values: Dict[str, Any]) -> Dict[str, str]:
    """日志用：把疑似密钥的字段值替换为 ``***``。"""
    result: Dict[str, str] = {}
    for key, value in values.items():
        if _SAFE_TOKENS.search(key):
            result[key] = "***"
        else:
            result[key] = str(value)
    return result


def envelope(line: str, width: int = 64) -> str:
    """给日志/消息加一条分隔线。"""
    line = line.strip()
    return f"{line}\n{'-' * min(width, max(8, len(line)))}"
