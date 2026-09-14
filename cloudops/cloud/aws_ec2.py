"""AWS EC2 适配器（论文 2.2：SigV4 签名 + 跨区域调度计算资源）。

EC2 的"查询协议"（Query Protocol）与现代 REST-JSON 接口不同：请求是
``application/x-www-form-urlencoded`` 的 Action 调用，响应是 XML。
本模块只依赖标准库的 ``xml.etree``，配合 :mod:`cloudops.cloud.aws_sigv4`
完成鉴权，从而避免引入 boto3/botocore（论文 2.3 的极简运行环境诉求）。
"""

from __future__ import annotations

import base64
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from ..config import Settings
from ..errors import CloudError, CredentialError, NotFound, ValidationError
from .aws_sigv4 import AwsCredentials, SigV4Signer
from .base import CloudProvider, CreateSpec, CredentialField, Instance
from .http import HttpClient
from .registry import register_provider

EC2_API_VERSION = "2016-11-15"
SSM_API_VERSION = "2014-11-06"
STS_API_VERSION = "2011-06-15"

#: 常见实例规格（仅用于错误提示，避免用户填错时无从下手）
COMMON_INSTANCE_TYPES = ("t3.micro", "t3.small", "t3.medium", "t4g.micro", "c7i.large", "m7i.large")

#: 镜像别名 → SSM 公共参数路径
AMI_ALIASES: Dict[str, str] = {
    "al2023": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "amazon-linux": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "amazonlinux": "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    "ubuntu": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
    "ubuntu24": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
    "ubuntu22": "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
    "debian": "/aws/service/debian/release/12/latest/amd64",
    "debian12": "/aws/service/debian/release/12/latest/amd64",
}

#: EC2 状态 → 统一状态
STATUS_MAP = {
    "pending": "provisioning",
    "running": "running",
    "rebooting": "rebooting",
    "stopping": "stopping",
    "stopped": "stopped",
    "shutting-down": "stopping",
    "terminated": "terminated",
}

_TERMINAL_STATES = {"terminated", "shutting-down"}
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*\.[a-z0-9]+$")

CREDENTIAL_FIELDS = (
    CredentialField("access_key_id", "AWS Access Key ID", example="AKIAXXXXXXXXXXXXXXXX"),
    CredentialField("secret_access_key", "AWS Secret Access Key"),
    CredentialField("session_token", "STS 临时凭证的 Session Token（长期 IAM 凭证可省略）",
                    required=False),
    CredentialField("region", "默认区域", required=False, secret=False, example="us-east-1"),
    CredentialField("regions", "需要一并遍历的其它区域，逗号分隔", required=False, secret=False,
                    example="ap-northeast-1,eu-west-1"),
)


class AwsQueryClient:
    """极简的 AWS 查询协议客户端（SigV4 签名 + XML 解析）。"""

    def __init__(self, credentials: AwsCredentials, region: str, *, service: str,
                 api_version: str, settings: Settings, http: Optional[HttpClient] = None) -> None:
        self.credentials = credentials
        self.region = region
        self.service = service
        self.api_version = api_version
        self.settings = settings
        self.endpoint = f"https://{service}.{region}.amazonaws.com/"
        self._http = http or HttpClient(
            provider="aws",
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            retries=settings.http_retries,
        )
        self._signer = SigV4Signer(credentials, region, service)

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ #
    def call(self, action: str, params: Optional[Dict[str, Any]] = None) -> ET.Element:
        """执行一次 Action 调用，返回 XML 根节点。"""
        payload: Dict[str, Any] = {"Action": action, "Version": self.api_version}
        payload.update(params or {})
        # AWS 要求 RFC3986 编码：空格必须是 %20，不能用 urlencode 默认的 '+'
        body = urllib.parse.urlencode(payload, quote_via=urllib.parse.quote, safe="")
        content_type = "application/x-www-form-urlencoded; charset=utf-8"
        signed = self._signer.sign("POST", self.endpoint, headers={"content-type": content_type},
                                   body=body)
        response = self._http.request(
            "POST",
            self.endpoint,
            headers=signed,
            data=body.encode("utf-8"),
            expect={200},
        )
        try:
            return _strip_namespace(ET.fromstring(response.content))
        except ET.ParseError as exc:
            raise CloudError(
                f"AWS 响应解析失败（{action}）：{exc}",
                provider="aws",
                status_code=response.status_code,
            ) from exc

    def caller_identity(self) -> Dict[str, str]:
        root = self.call("GetCallerIdentity")
        return {
            "account": root.findtext(".//Account") or "",
            "arn": root.findtext(".//Arn") or "",
            "user_id": root.findtext(".//UserId") or "",
        }

    def get_parameter(self, name: str) -> Optional[str]:
        root = self.call("GetParameter", {"Name": name, "WithDecryption": "false"})
        return root.findtext(".//Parameter/Value")

    def describe_regions(self) -> List[str]:
        root = self.call("DescribeRegions")
        return [item.findtext("regionName") or "" for item in root.findall(".//regionInfo/item")]


def _strip_namespace(root: ET.Element) -> ET.Element:
    """去掉 XML 命名空间。

    EC2/SSM/STS 查询协议的响应形如
    ``<DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">``，
    子元素全部落在该默认命名空间下；而 ``findall("reservationSet/item")`` 这类
    不带前缀的路径**匹配不到**带命名空间的元素（返回空列表）。统一剥掉命名空间，
    可以让后续解析代码保持简洁可读。
    """
    for element in root.iter():
        tag = element.tag
        if isinstance(tag, str) and tag.startswith("{"):
            element.tag = tag.split("}", 1)[1]
    return root


def _tags_of(instance: ET.Element) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for tag in instance.findall("./tagSet/item"):
        key = tag.findtext("key")
        if key:
            tags[key] = tag.findtext("value") or ""
    return tags


def _region_from_az(az: Optional[str], fallback: str) -> str:
    if not az:
        return fallback
    match = re.match(r"^([a-z]{2}(?:-[a-z]+)+-\d)[a-z]$", az)
    return match.group(1) if match else fallback


@register_provider
class AwsEc2Provider(CloudProvider):
    """AWS EC2 多云适配器。"""

    name = "aws"
    display_name = "AWS EC2"
    docs_url = "https://docs.aws.amazon.com/AWSEC2/latest/APIReference/"
    aliases = ("aws", "ec2", "amazon")
    credential_fields = CREDENTIAL_FIELDS
    capabilities = frozenset({"list", "create", "destroy", "status"})

    def __init__(self, secrets, settings: Settings, *, label: str = "default", meta=None) -> None:
        super().__init__(secrets, settings, label=label, meta=meta)
        self.warnings: List[str] = []
        self._clients: Dict[str, AwsQueryClient] = {}
        self._region_cache: Optional[List[str]] = None

    # ------------------------------------------------------------------ #
    # 客户端
    # ------------------------------------------------------------------ #
    @property
    def credentials(self) -> AwsCredentials:
        self.require_credentials()
        return AwsCredentials(
            access_key_id=str(self.secrets.get("access_key_id", "")).strip(),
            secret_access_key=str(self.secrets.get("secret_access_key", "")).strip(),
            session_token=(str(self.secrets["session_token"]).strip()
                           if self.secrets.get("session_token") else None),
        )

    def client(self, *, service: str = "ec2", region: Optional[str] = None) -> AwsQueryClient:
        region = region or self.default_region()
        cache_key = f"{service}:{region}"
        if cache_key not in self._clients:
            api_version = {"ec2": EC2_API_VERSION, "sts": STS_API_VERSION,
                           "ssm": SSM_API_VERSION}.get(service, EC2_API_VERSION)
            self._clients[cache_key] = AwsQueryClient(
                self.credentials, region, service=service, api_version=api_version,
                settings=self.settings,
            )
        return self._clients[cache_key]

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ------------------------------------------------------------------ #
    # 鉴权校验
    # ------------------------------------------------------------------ #
    def validate_credentials(self) -> str:
        try:
            identity = self.client(service="sts").caller_identity()
        except CloudError as exc:
            text = str(exc)
            lowered = text.lower()
            if "invalidclienttokenid" in lowered or "signature" in lowered or "authfailure" in lowered:
                raise CredentialError(
                    params={"provider": self.display_name, "detail": text},
                    key="error.invalid_credential",
                ) from exc
            raise
        account = identity.get("account") or "unknown"
        arn = identity.get("arn") or ""
        role = "STS 临时凭证" if self.secrets.get("session_token") else "长期 IAM 凭证"
        return f"AWS 账号 {account}（{role}）\n  身份: {arn}" if arn else f"AWS 账号 {account}"

    # ------------------------------------------------------------------ #
    # 区域
    # ------------------------------------------------------------------ #
    def available_regions(self) -> List[str]:
        if self._region_cache is None:
            try:
                self._region_cache = sorted(self.client().describe_regions())
            except CloudError:
                self._region_cache = []
        return self._region_cache

    def target_regions(self) -> List[str]:
        """需要遍历的区域：凭证里写的 → env 配置的 → 默认区域。"""
        raw = str(self.secrets.get("regions") or "").strip()
        if raw:
            return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
        if self.meta.get("regions"):
            value = self.meta["regions"]
            return list(value) if isinstance(value, (list, tuple)) else [str(value)]
        if self.settings.aws.regions:
            return list(self.settings.aws.regions)
        return [self.default_region()]

    def default_region(self) -> str:
        return str(self.secrets.get("region") or self.meta.get("region") or self.settings.aws.region)

    def default_size(self) -> str:
        return str(self.meta.get("size") or self.settings.aws.instance_type)

    def default_image(self) -> str:
        return str(self.meta.get("image") or self.settings.aws.ami
                   or self.settings.aws.ami_ssm_parameter)

    def resolve_region(self, token: Optional[str]) -> str:
        raw = (token or "").strip()
        if not raw:
            return self.default_region()
        if not _REGION_RE.match(raw):
            raise ValidationError(
                params={"value": raw, "parameter": "区域（形如 us-east-1）",
                        "options": ", ".join(self.available_regions()[:12]) or "us-east-1, ap-northeast-1"},
                key="error.unknown_option",
            )
        return raw

    def resolve_size(self, token: Optional[str]) -> str:
        raw = (token or "").strip()
        if not raw:
            return self.default_size()
        if not _TYPE_RE.match(raw):
            raise ValidationError(
                params={"value": raw, "parameter": "实例规格（形如 t3.micro）",
                        "options": ", ".join(COMMON_INSTANCE_TYPES)},
                key="error.unknown_option",
            )
        return raw

    def resolve_image(self, token: Optional[str]) -> str:
        """支持 3 种写法：``ami-xxx`` / SSM 参数路径 / 别名（ubuntu、al2023…）。"""
        raw = (token or "").strip()
        if not raw:
            configured = self.settings.aws.ami
            if configured:
                return configured
            return self._resolve_ami_from_ssm(self.settings.aws.ami_ssm_parameter)
        if raw.startswith("ami-"):
            return raw
        lowered = raw.lower()
        parameter = AMI_ALIASES.get(lowered)
        if parameter is None and raw.startswith("/"):
            parameter = raw
        if parameter is None:
            raise ValidationError(
                params={"value": raw, "parameter": "镜像（ami-xxx / SSM 参数路径 / 别名）",
                        "options": ", ".join(sorted(AMI_ALIASES))},
                key="error.unknown_option",
            )
        return self._resolve_ami_from_ssm(parameter)

    def _resolve_ami_from_ssm(self, parameter: str) -> str:
        """通过 SSM 公共参数解析"最新 AMI"，免去手工查镜像 ID。"""
        try:
            value = self.client(service="ssm").get_parameter(parameter)
        except CloudError as exc:
            # 常见原因：IAM 策略缺少 ssm:GetParameter，或该区域没有公共参数
            self.warnings.append(f"SSM 解析 AMI 失败（{parameter}）：{exc}")
            value = None
        if not value:
            raise ValidationError(
                params={"value": parameter, "parameter": "AMI",
                        "options": "ami-xxxxxxxx（或在 .env 中设置 AWS_DEFAULT_AMI）"},
                key="error.ami_lookup_failed",
            )
        return value

    def describe_options(self) -> str:
        return "\n".join([
            super().describe_options(),
            f"遍历区域 : {', '.join(self.target_regions())}",
            f"常用规格 : {', '.join(COMMON_INSTANCE_TYPES)}",
            "镜像写法 : ami-xxxxxxxx 或 ubuntu / al2023 / debian（自动解析 SSM 公共参数）",
            "用法示例 : /create aws us-east-1 t3.micro ubuntu  |  /create aws size=t3.small image=ami-0123",
        ])

    # ------------------------------------------------------------------ #
    # XML → 统一模型
    # ------------------------------------------------------------------ #
    def _to_instance(self, node: ET.Element, queried_region: str) -> Instance:
        tags = _tags_of(node)
        az = node.findtext("placement/availabilityZone")
        state = node.findtext("instanceState/name") or "unknown"
        return Instance(
            provider=self.name,
            id=str(node.findtext("instanceId") or ""),
            name=tags.get("Name") or str(node.findtext("instanceId") or ""),
            status=STATUS_MAP.get(state, state),
            region=_region_from_az(az, queried_region),
            public_ip=node.findtext("ipAddress") or None,
            private_ip=node.findtext("privateIpAddress") or None,
            ipv6=node.findtext("ipv6Address") or None,
            size=node.findtext("instanceType"),
            image=node.findtext("imageId"),
            created_at=node.findtext("launchTime"),
            tags=[value for key, value in tags.items() if key != "Name"],
            raw={"availability_zone": az, "vpc_id": node.findtext("vpcId")},
        )

    def _describe(self, region: str, params: Optional[Dict[str, Any]] = None) -> List[Instance]:
        root = self.client(region=region).call("DescribeInstances", params or {})
        instances: List[Instance] = []
        for reservation in root.findall("./reservationSet/item"):
            for node in reservation.findall("./instancesSet/item"):
                instance = self._to_instance(node, region)
                if instance.status == "terminated":
                    continue
                instances.append(instance)
        return instances

    # ------------------------------------------------------------------ #
    # 实例生命周期
    # ------------------------------------------------------------------ #
    def list_instances(self) -> List[Instance]:
        self.require_credentials()
        instances: List[Instance] = []
        errors: List[str] = []
        params = {
            "Filter.1.Name": "instance-state-name",
            "Filter.1.Value.1": "pending",
            "Filter.1.Value.2": "running",
            "Filter.1.Value.3": "stopping",
            "Filter.1.Value.4": "stopped",
        }
        for region in self.target_regions():
            try:
                instances.extend(self._describe(region, dict(params)))
            except CloudError as exc:
                errors.append(f"{region}: {exc}")
        if errors and not instances:
            raise CloudError(
                params={"provider": self.display_name, "detail": "; ".join(errors)},
                key="error.all_regions_failed",
                provider=self.name,
            )
        if errors:
            self.warnings.extend(f"区域 {item}" for item in errors)
        return instances

    def get_instance(self, instance_id: str) -> Instance:
        target = str(instance_id)
        errors: List[str] = []
        for region in self.target_regions():
            try:
                root = self.client(region=region).call(
                    "DescribeInstances", {"InstanceId.1": target}
                )
            except CloudError as exc:
                errors.append(f"{region}: {exc}")
                continue
            for reservation in root.findall("./reservationSet/item"):
                for node in reservation.findall("./instancesSet/item"):
                    if str(node.findtext("instanceId")) == target:
                        return self._to_instance(node, region)
        if errors:
            raise CloudError(
                params={"provider": self.display_name, "detail": "; ".join(errors)},
                key="error.all_regions_failed",
                provider=self.name,
            )
        raise NotFound(params={"target": target, "provider": self.display_name},
                       key="error.instance_not_found")

    def create_instance(self, spec: CreateSpec) -> Instance:
        self.require_credentials()
        region = spec.region or self.default_region()
        image = spec.image or self.default_image()
        if image.startswith("/"):  # 未解析的 SSM 参数路径
            image = self._resolve_ami_from_ssm(image)

        payload: Dict[str, Any] = {
            "ImageId": image,
            "InstanceType": spec.size or self.default_size(),
            "MinCount": "1",
            "MaxCount": "1",
            "TagSpecification.1.ResourceType": "instance",
            "TagSpecification.1.Tag.1.Key": "Name",
            "TagSpecification.1.Tag.1.Value": spec.name,
        }
        extra_tags = list(spec.tags or [])
        for index, tag in enumerate(extra_tags, start=2):
            if "=" in tag:
                key, value = tag.split("=", 1)
            else:
                key, value = tag, ""
            payload[f"TagSpecification.1.Tag.{index}.Key"] = key
            payload[f"TagSpecification.1.Tag.{index}.Value"] = value

        key_name = spec.key_name or self.settings.aws.key_name
        if key_name:
            payload["KeyName"] = key_name
        security_groups = list(spec.security_group_ids or self.settings.aws.security_group_ids)
        for index, group in enumerate(security_groups, start=1):
            payload[f"SecurityGroupId.{index}"] = group
        subnet_id = spec.subnet_id or self.settings.aws.subnet_id
        if subnet_id:
            payload["SubnetId"] = subnet_id
        if self.settings.aws.iam_instance_profile:
            payload["IamInstanceProfile.Name"] = self.settings.aws.iam_instance_profile
        if spec.user_data:
            # EC2 要求 UserData 必须是 base64（且不能超过 16KB）
            encoded = base64.b64encode(spec.user_data.encode("utf-8")).decode("ascii")
            payload["UserData"] = encoded
            if len(spec.user_data) > 16 * 1024:
                raise ValidationError(params={"limit": "16KB"}, key="error.user_data_too_large")

        root = self.client(region=region).call("RunInstances", payload)
        node = root.find("./instancesSet/item")
        if node is None:
            raise CloudError(
                params={"provider": self.display_name, "detail": "响应缺少实例信息"},
                key="error.unexpected_response",
                provider=self.name,
            )
        instance = self._to_instance(node, region)
        if not instance.id:
            raise CloudError(
                params={"provider": self.display_name, "detail": "响应缺少 instanceId"},
                key="error.unexpected_response",
                provider=self.name,
            )
        return instance

    def destroy_instance(self, instance_id: str) -> None:
        self.require_credentials()
        last_error: Optional[Exception] = None
        for region in self.target_regions():
            try:
                self.client(region=region).call("TerminateInstances", {"InstanceId.1": instance_id})
                return
            except CloudError as exc:
                last_error = exc
                if "InvalidInstanceID" not in str(exc) and "not found" not in str(exc).lower():
                    raise
        if last_error is not None:
            raise NotFound(params={"target": instance_id, "provider": self.display_name},
                           key="error.instance_not_found")

    def sizes_catalog(self) -> str:
        return ", ".join(COMMON_INSTANCE_TYPES)

    def regions_catalog(self) -> str:
        regions = self.available_regions()
        return ", ".join(regions[:16]) if regions else ", ".join(self.target_regions())
