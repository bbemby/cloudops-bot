"""配置层：所有"需要用户提供的东西"都来自环境变量 / ``.env``。

设计原则（论文 4.2 凭证与环境管理）：

1. 代码里不出现任何真实密钥，仓库可直接开源；
2. 所有可变项集中在此模块，并有 ``.env.example`` 与 README 对照说明；
3. 缺失关键项时给出**可执行的修复提示**，而不是抛裸异常。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .errors import ConfigError
from .utils import load_dotenv_file, redact_mapping

DEFAULT_ENV_FILE = ".env"

_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _env_get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value != "" else default


def _env_str(name: str, default: str) -> str:
    return _env_get(name, default) or default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_get(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    raw = _env_get(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env_get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_list(name: str, default: Sequence[str] = ()) -> List[str]:
    raw = _env_get(name)
    if raw is None:
        return list(default)
    items = [item.strip() for item in raw.replace(";", ",").split(",")]
    return [item for item in items if item]


def _env_ids(name: str) -> Tuple[int, ...]:
    """解析逗号分隔的 Telegram 数字 ID 列表，忽略非法项。"""
    result: List[int] = []
    for item in _env_list(name):
        token = item.lstrip("@")
        if token.lstrip("-").isdigit():
            value = int(token)
            if value not in result:
                result.append(value)
    return tuple(result)


@dataclass
class TelegramSettings:
    """交互层参数。"""

    token: str = ""
    api_base: str = "https://api.telegram.org"
    proxy: Optional[str] = None
    poll_timeout: int = 30
    request_timeout: float = 35.0
    retries: int = 3
    show_typing: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.token)


@dataclass
class DigitalOceanDefaults:
    region: str = "sgp1"
    size: str = "s-1vcpu-1gb"
    image: str = "debian-12-x64"
    ssh_keys: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    ipv6: bool = False
    monitoring: bool = True
    backups: bool = False


@dataclass
class AwsDefaults:
    region: str = "us-east-1"
    regions: List[str] = field(default_factory=list)
    instance_type: str = "t3.micro"
    ami: Optional[str] = None
    ami_ssm_parameter: str = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
    key_name: Optional[str] = None
    security_group_ids: List[str] = field(default_factory=list)
    subnet_id: Optional[str] = None
    iam_instance_profile: Optional[str] = None
    associate_public_ip: bool = True

    @property
    def region_list(self) -> List[str]:
        """需要遍历的区域列表：显式配置优先，否则用默认区域。"""
        return self.regions or [self.region]


@dataclass
class BootstrapCredential:
    """首次启动时自动写入数据库的默认凭证（来自 .env，可留空）。"""

    label: str = "default"
    digitalocean_token: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_session_token: Optional[str] = None
    aws_region: Optional[str] = None

    @property
    def has_digitalocean(self) -> bool:
        return bool(self.digitalocean_token)

    @property
    def has_aws(self) -> bool:
        return bool(self.aws_access_key_id and self.aws_secret_access_key)


@dataclass
class Settings:
    """全局配置。"""

    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    admin_ids: Tuple[int, ...] = ()
    readonly_ids: Tuple[int, ...] = ()
    secret_key: str = ""
    database_path: Path = Path("./data/cloudops.db")
    default_provider: str = "digitalocean"
    language: str = "zh"
    log_level: str = "INFO"
    log_file: Optional[Path] = None
    display_timezone: str = "Asia/Shanghai"
    default_instance_name: str = "auto-ops-node"
    rate_limit_per_minute: int = 20
    confirm_ttl_seconds: int = 300
    create_timeout_seconds: int = 900
    create_poll_interval: float = 8.0
    create_max_count: int = 3
    http_timeout: float = 30.0
    http_retries: int = 3
    user_agent: str = f"cloudops-bot/{__version__}"
    enable_mock_provider: bool = False
    mock_state_path: Path = Path("./data/mock-cloud.json")
    notify_admins_on_denied: bool = False
    banner: bool = True
    digitalocean: DigitalOceanDefaults = field(default_factory=DigitalOceanDefaults)
    aws: AwsDefaults = field(default_factory=AwsDefaults)
    bootstrap: BootstrapCredential = field(default_factory=BootstrapCredential)
    env_file: Optional[Path] = None

    # ------------------------------------------------------------------ #
    # 装配
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env_file: Optional[str] = DEFAULT_ENV_FILE,
                 environ: Optional[Dict[str, str]] = None) -> "Settings":
        """从 ``.env`` + 进程环境装配配置。

        ``.env`` 只填充"尚未显式设置"的变量，因此在 Docker/K8s 中
        直接注入环境变量依然拥有更高优先级。
        """
        env_path = Path(env_file) if env_file else None
        if env_path is not None:
            load_dotenv_file(env_path, environ)

        db_path = Path(_env_str("DATABASE_PATH", "./data/cloudops.db")).expanduser()
        log_file = _env_get("LOG_FILE")

        settings = cls(
            telegram=TelegramSettings(
                token=_env_str("TELEGRAM_BOT_TOKEN", ""),
                api_base=_env_str("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
                proxy=_env_get("TELEGRAM_PROXY"),
                poll_timeout=_env_int("TELEGRAM_POLL_TIMEOUT", 30),
                request_timeout=_env_float("TELEGRAM_REQUEST_TIMEOUT", 35.0),
                retries=_env_int("TELEGRAM_RETRIES", 3),
                show_typing=_env_bool("TELEGRAM_SHOW_TYPING", True),
            ),
            admin_ids=_env_ids("BOT_ADMIN_IDS"),
            readonly_ids=_env_ids("BOT_READONLY_IDS"),
            secret_key=_env_str("SECRET_KEY", ""),
            database_path=db_path,
            default_provider=_env_str("DEFAULT_PROVIDER", "digitalocean").lower(),
            language=_env_str("BOT_LANGUAGE", "zh").lower(),
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
            log_file=Path(log_file).expanduser() if log_file else None,
            display_timezone=_env_str("DISPLAY_TIMEZONE", "Asia/Shanghai"),
            default_instance_name=_env_str("DEFAULT_INSTANCE_NAME", "auto-ops-node"),
            rate_limit_per_minute=max(0, _env_int("RATE_LIMIT_PER_MINUTE", 20)),
            confirm_ttl_seconds=max(30, _env_int("CONFIRM_TTL_SECONDS", 300)),
            create_timeout_seconds=max(60, _env_int("CREATE_TIMEOUT_SECONDS", 900)),
            create_poll_interval=max(2.0, _env_float("CREATE_POLL_INTERVAL_SECONDS", 8.0)),
            create_max_count=max(1, _env_int("CREATE_MAX_COUNT", 3)),
            http_timeout=max(5.0, _env_float("HTTP_TIMEOUT_SECONDS", 30.0)),
            http_retries=max(0, _env_int("HTTP_RETRIES", 3)),
            user_agent=_env_str("USER_AGENT", f"cloudops-bot/{__version__}"),
            enable_mock_provider=_env_bool("ENABLE_MOCK_PROVIDER", False),
            mock_state_path=Path(_env_str("MOCK_STATE_PATH", "./data/mock-cloud.json")).expanduser(),
            notify_admins_on_denied=_env_bool("NOTIFY_ADMINS_ON_DENIED", False),
            banner=_env_bool("STARTUP_BANNER", True),
            digitalocean=DigitalOceanDefaults(
                region=_env_str("DO_DEFAULT_REGION", "sgp1"),
                size=_env_str("DO_DEFAULT_SIZE", "s-1vcpu-1gb"),
                image=_env_str("DO_DEFAULT_IMAGE", "debian-12-x64"),
                ssh_keys=_env_list("DO_SSH_KEYS"),
                tags=_env_list("DO_TAGS"),
                ipv6=_env_bool("DO_ENABLE_IPV6", False),
                monitoring=_env_bool("DO_ENABLE_MONITORING", True),
                backups=_env_bool("DO_ENABLE_BACKUPS", False),
            ),
            aws=AwsDefaults(
                region=_env_str("AWS_REGION", _env_str("AWS_DEFAULT_REGION", "us-east-1")),
                regions=_env_list("AWS_REGIONS"),
                instance_type=_env_str("AWS_DEFAULT_INSTANCE_TYPE", "t3.micro"),
                ami=_env_get("AWS_DEFAULT_AMI"),
                ami_ssm_parameter=_env_str(
                    "AWS_AMI_SSM_PARAMETER",
                    "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
                ),
                key_name=_env_get("AWS_KEY_NAME"),
                security_group_ids=_env_list("AWS_SECURITY_GROUP_IDS"),
                subnet_id=_env_get("AWS_SUBNET_ID"),
                iam_instance_profile=_env_get("AWS_IAM_INSTANCE_PROFILE"),
                associate_public_ip=_env_bool("AWS_ASSOCIATE_PUBLIC_IP", True),
            ),
            bootstrap=BootstrapCredential(
                label=_env_str("BOOTSTRAP_CREDENTIAL_LABEL", "default"),
                digitalocean_token=_env_get("DIGITALOCEAN_TOKEN"),
                aws_access_key_id=_env_get("AWS_ACCESS_KEY_ID"),
                aws_secret_access_key=_env_get("AWS_SECRET_ACCESS_KEY"),
                aws_session_token=_env_get("AWS_SESSION_TOKEN"),
                aws_region=_env_get("AWS_REGION") or _env_get("AWS_DEFAULT_REGION"),
            ),
            env_file=env_path,
        )
        return settings

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def missing_requirements(self) -> List[str]:
        """返回缺失项的**修复提示**列表（空列表表示配置完整）。"""
        problems: List[str] = []
        if not self.telegram.token:
            problems.append("TELEGRAM_BOT_TOKEN 未设置：向 @BotFather 申请 Bot Token 后写入 .env")
        if not self.admin_ids:
            problems.append(
                "BOT_ADMIN_IDS 未设置：先把你的 Telegram 数字 ID（可向 @userinfobot 查询）填入 .env"
            )
        if not self.secret_key:
            problems.append("SECRET_KEY 未设置：执行 `python manage.py gen-secret` 生成后写入 .env")
        return problems

    def validate(self, *, require_telegram: bool = True) -> None:
        """校验配置，缺失时抛出带修复建议的 :class:`ConfigError`。"""
        problems = self.missing_requirements()
        if not require_telegram:
            problems = [p for p in problems if not p.startswith("TELEGRAM_BOT_TOKEN")]
        if problems:
            raise ConfigError(
                "配置校验失败：\n" + "\n".join(f"  • {item}" for item in problems),
                key="error.config",
            )

    def as_log_fields(self) -> Dict[str, str]:
        """启动日志用：脱敏后的配置快照。"""
        fields = {
            "env_file": str(self.env_file) if self.env_file else "-",
            "database_path": str(self.database_path),
            "default_provider": self.default_provider,
            "language": self.language,
            "log_level": self.log_level,
            "timezone": self.display_timezone,
            "admins": ",".join(str(i) for i in self.admin_ids) or "-",
            "readonly": ",".join(str(i) for i in self.readonly_ids) or "-",
            "telegram_api_base": self.telegram.api_base,
            "telegram_bot_token": self.telegram.token,
            "secret_key": self.secret_key,
            "bootstrap_digitalocean_token": self.bootstrap.digitalocean_token,
            "bootstrap_aws_access_key_id": self.bootstrap.aws_access_key_id,
            "mock_provider": str(self.enable_mock_provider),
        }
        return redact_mapping(fields)

    def ensure_directories(self) -> None:
        """确保数据库/状态文件所在目录存在。"""
        for path in (self.database_path, self.mock_state_path, self.log_file):
            if path is None:
                continue
            parent = path.parent
            if str(parent) not in ("", "."):
                parent.mkdir(parents=True, exist_ok=True)
