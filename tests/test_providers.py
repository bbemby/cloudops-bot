"""多云适配层测试：注册表 / Mock Cloud / DigitalOcean（桩 HTTP）/ AWS EC2（桩 XML）。

这些用例**完全不联网**：DO 与 AWS 的传输层被替换成本地桩，
因此可以在 CI 里稳定覆盖"响应映射、分页、别名解析、错误归一化"等易错逻辑。
"""

from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from support import ROOT  # noqa: F401
from support import make_settings

from cloudops.cloud import (
    AwsEc2Provider,
    DigitalOceanProvider,
    MockProvider,
    available_provider_names,
    canonical_name,
    create_provider,
    get_provider_class,
    is_known,
    provider_names,
    providers_summary,
    register_provider,
)
from cloudops.cloud.aws_ec2 import AwsEc2Provider as AwsEc2ProviderDirect  # noqa: F401
from cloudops.cloud.aws_ec2 import AwsQueryClient, _strip_namespace
from cloudops.cloud.aws_sigv4 import AwsCredentials
from cloudops.cloud.base import CloudProvider, CreateSpec
from cloudops.cloud.http import HttpClient
from cloudops.errors import CloudError, CredentialError, NotFound, ValidationError


# --------------------------------------------------------------------------- #
# 桩传输层
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.content = self.text.encode("utf-8")
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class StubHttp:
    """替换 :class:`HttpClient`：按 URL 路由返回预设响应。

    与真实实现保持同样的"状态码契约"：不在 ``expect`` 内就抛
    :class:`CloudError`（并带上 ``status_code``），否则用例会误判成功路径。
    """

    def __init__(self, routes) -> None:
        self.routes = routes
        self.calls = []
        self.closed = False

    def request(self, method, url, *, headers=None, params=None, json_body=None,
                data=None, expect=None, allow_status=()):
        self.calls.append({"method": method, "url": url, "params": params, "json": json_body,
                           "headers": headers, "data": data})
        route = self.routes.get(url)
        response = route(params, json_body) if callable(route) else route
        if response is None:
            response = FakeResponse(404, {"errors": [{"message": f"no route for {url}"}]})

        expected = set(expect) if expect is not None else None
        allowed = set(allow_status)
        status = response.status_code
        ok = (status in expected or status in allowed) if expected is not None \
            else (200 <= status < 300 or status in allowed)
        if not ok:
            raise CloudError(
                HttpClient._describe_error(response),
                provider="stub", status_code=status, retryable=status in (429, 500, 502, 503, 504),
            )
        return response

    def close(self) -> None:
        self.closed = True


class StubQueryClient:
    """替换 :class:`AwsQueryClient`：按 Action 返回预设 XML。

    同时实现真实客户端的便捷方法（``caller_identity`` / ``get_parameter``），
    让上层代码无需感知自己拿到的其实是桩。
    """

    def __init__(self, responses) -> None:
        self.responses = responses
        self.calls = []

    def call(self, action, params=None) -> ET.Element:
        self.calls.append((action, params))
        factory = self.responses.get(action)
        if factory is None:
            raise CloudError(f"unexpected action {action}", provider="aws")
        xml = factory(params) if callable(factory) else factory
        # 与真实 AwsQueryClient 一致：解析后剥掉 XML 命名空间
        return _strip_namespace(ET.fromstring(xml))

    def caller_identity(self):
        root = self.call("GetCallerIdentity")
        return {
            "account": root.findtext(".//Account") or "",
            "arn": root.findtext(".//Arn") or "",
            "user_id": root.findtext(".//UserId") or "",
        }

    def get_parameter(self, name):
        root = self.call("GetParameter", {"Name": name})
        return root.findtext(".//Parameter/Value")

    def close(self) -> None:
        pass


DROPLET = {
    "id": 3164494,
    "name": "web-01",
    "status": "active",
    "created_at": "2026-01-02T03:04:05Z",
    "networks": {
        "v4": [
            {"ip_address": "10.10.0.5", "type": "private"},
            {"ip_address": "203.0.113.5", "type": "public"},
        ],
        "v6": [{"ip_address": "2001:db8::5", "type": "public"}],
    },
    "region": {"slug": "sgp1"},
    "size": {"slug": "s-1vcpu-1gb", "memory": 1024, "vcpus": 1, "price_monthly": 6.0},
    "image": {"slug": "debian-12-x64", "name": "Debian 12"},
    "tags": ["demo"],
}


class RegistryTest(unittest.TestCase):
    def test_builtin_providers_registered(self) -> None:
        names = provider_names()
        for expected in ("digitalocean", "aws", "mock"):
            self.assertIn(expected, names)

    def test_aliases_resolve_to_canonical_names(self) -> None:
        self.assertEqual(canonical_name("do"), "digitalocean")
        self.assertEqual(canonical_name("ec2"), "aws")
        self.assertEqual(canonical_name("demo"), "mock")
        self.assertEqual(canonical_name("unknown"), "unknown")
        self.assertTrue(is_known("digitalocean"))
        self.assertFalse(is_known("aliyun"))

    def test_get_provider_class_and_unknown(self) -> None:
        self.assertIs(get_provider_class("do"), DigitalOceanProvider)
        with self.assertRaises(KeyError):
            get_provider_class("aliyun")

    def test_create_provider_unknown_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            create_provider("aliyun", {}, None)

    def test_register_rejects_name_conflict_and_missing_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))

            class Conflict(CloudProvider):
                name = "digitalocean"
                display_name = "conflict"

                def validate_credentials(self):  # pragma: no cover
                    return ""

                def list_instances(self):  # pragma: no cover
                    return []

                def create_instance(self, spec):  # pragma: no cover
                    raise NotImplementedError

                def destroy_instance(self, instance_id):  # pragma: no cover
                    raise NotImplementedError

                def get_instance(self, instance_id):  # pragma: no cover
                    raise NotImplementedError

            with self.assertRaises(ValueError):
                register_provider(Conflict)

            class NoName(Conflict):
                name = ""

            with self.assertRaises(ValueError):
                register_provider(NoName)

    def test_available_provider_names_hides_mock_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), enable_mock_provider=False)
            self.assertNotIn("mock", available_provider_names(settings))
            settings.enable_mock_provider = True
            self.assertIn("mock", available_provider_names(settings))

    def test_providers_summary(self) -> None:
        summary = dict(providers_summary())
        self.assertEqual(summary["digitalocean"], "DigitalOcean")
        self.assertIn(summary["aws"], ("AWS EC2", "AWS EC2"))


class AwsQueryClientTest(unittest.TestCase):
    """真实 :class:`AwsQueryClient` 的行为（签名 + 编码 + 命名空间剥离）。"""

    NAMESPACED = """
    <DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
      <reservationSet>
        <item>
          <instancesSet>
            <item><instanceId>i-0abc</instanceId>
                  <instanceState><name>running</name></instanceState></item>
          </instancesSet>
        </item>
      </reservationSet>
    </DescribeInstancesResponse>
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = make_settings(Path(self._tmp.name))
        self.http = StubHttp({"https://ec2.us-east-1.amazonaws.com/":
                              FakeResponse(200, None, text=self.NAMESPACED)})
        self.client = AwsQueryClient(
            AwsCredentials("AKIAEXAMPLE", "secret"), "us-east-1", service="ec2",
            api_version="2016-11-15", settings=self.settings, http=self.http,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_call_signs_and_strips_namespace(self) -> None:
        root = self.client.call("DescribeInstances", {"Filter.1.Name": "instance-state-name"})
        items = root.findall("./reservationSet/item/instancesSet/item")
        self.assertEqual([item.findtext("instanceId") for item in items], ["i-0abc"])

        sent = self.http.calls[-1]
        self.assertTrue(sent["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256 "))
        self.assertEqual(sent["headers"]["content-type"],
                         "application/x-www-form-urlencoded; charset=utf-8")
        self.assertIn(b"Action=DescribeInstances", sent["data"])
        self.assertIn(b"Version=2016-11-15", sent["data"])

    def test_call_percent_encodes_spaces_and_slashes(self) -> None:
        self.client.call("GetParameter", {"Name": "/aws/service/ami latest"})
        body = self.http.calls[-1]["data"]
        self.assertIn(b"%2Faws%2Fservice%2Fami%20latest", body)
        self.assertNotIn(b"+", body)

    def test_call_surfaces_http_error(self) -> None:
        self.http.routes["https://ec2.us-east-1.amazonaws.com/"] = FakeResponse(
            400, {"errors": [{"message": "InvalidInstanceID.NotFound"}]})
        with self.assertRaises(CloudError) as ctx:
            self.client.call("DescribeInstances", {"InstanceId.1": "i-missing"})
        self.assertIn("InvalidInstanceID", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 400)


# --------------------------------------------------------------------------- #
class MockProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings = make_settings(self.tmp_path)
        self.provider = MockProvider({"token": "demo", "delay": "0"}, self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_validate_credentials_requires_token(self) -> None:
        self.assertIn("本地模拟账户", self.provider.validate_credentials())
        with self.assertRaises(CredentialError):
            MockProvider({}, self.settings).validate_credentials()

    def test_create_list_and_find(self) -> None:
        spec = CreateSpec(name="web-01", region="mock-sgp1", size="mock-1vcpu-1gb",
                          image="mock-debian-12", count=1)
        created = self.provider.create_instance(spec)
        self.assertTrue(created.id.startswith("mock-"))
        # delay=0 → 下一次查询即可拿到公网 IP
        ready = self.provider.get_instance(created.id)
        self.assertEqual(ready.status, "running")
        self.assertTrue(ready.ready)
        self.assertEqual(self.provider.find_instance(ready.public_ip).id, created.id)
        self.assertEqual(self.provider.find_instance("web-01").id, created.id)
        self.assertEqual(len(self.provider.list_instances()), 1)

    def test_batch_create_numbers_names(self) -> None:
        spec = CreateSpec(name="node", count=3)
        created = self.provider.create_instances(spec)
        self.assertEqual([item.name for item in created], ["node-1", "node-2", "node-3"])

    def test_destroy_and_unknown_instance(self) -> None:
        created = self.provider.create_instance(CreateSpec(name="gone"))
        self.provider.destroy_instance(created.id)
        self.assertEqual(self.provider.list_instances(), [])
        with self.assertRaises(NotFound):
            self.provider.destroy_instance("mock-9999")

    def test_wait_until_ready_and_failure_status(self) -> None:
        created = self.provider.create_instance(CreateSpec(name="wait"))
        final = self.provider.wait_until_ready(created.id, timeout=5, interval=1)
        self.assertEqual(final.status, "running")

        broken = self.provider.create_instance(CreateSpec(name="broken"))
        self.provider.inject_failure(broken.id)
        with self.assertRaises(CloudError):
            self.provider.wait_until_ready(broken.id, timeout=5, interval=1)

    def test_describe_options_mentions_delay_and_state_file(self) -> None:
        text = self.provider.describe_options()
        self.assertIn("状态文件", text)
        self.assertIn("Mock Cloud", text)


# --------------------------------------------------------------------------- #
class DigitalOceanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings = make_settings(self.tmp_path)
        self.provider = DigitalOceanProvider({"token": "dop_v1_fake", "region": "sgp1"},
                                             self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stub(self, routes) -> StubHttp:
        stub = StubHttp(routes)
        self.provider._http = stub
        return stub

    def test_validate_credentials(self) -> None:
        self._stub({"https://api.digitalocean.com/v2/account":
                    FakeResponse(200, {"account": {"email": "ops@example.com", "status": "active"}})})
        self.assertEqual(self.provider.validate_credentials(), "ops@example.com")

    def test_validate_credentials_maps_401_to_credential_error(self) -> None:
        self._stub({"https://api.digitalocean.com/v2/account":
                    FakeResponse(401, {"id": "unauthorized", "message": "bad token"})})
        with self.assertRaises(CredentialError) as ctx:
            self.provider.validate_credentials()
        self.assertEqual(ctx.exception.key, "error.invalid_credential")

    def test_list_instances_follows_pagination(self) -> None:
        def droplets(params, body):
            page = int((params or {}).get("page", 1))
            if page == 1:
                return FakeResponse(200, {"droplets": [DROPLET],
                                          "links": {"pages": {"next": "https://api.digitalocean.com/v2/droplets?page=2"}}})
            return FakeResponse(200, {"droplets": [dict(DROPLET, id=2, name="web-02")]})

        stub = self._stub({"https://api.digitalocean.com/v2/droplets": droplets})
        instances = self.provider.list_instances()
        self.assertEqual([item.name for item in instances], ["web-01", "web-02"])
        self.assertEqual(len(stub.calls), 2)

        first = instances[0]
        self.assertEqual(first.public_ip, "203.0.113.5")
        self.assertEqual(first.private_ip, "10.10.0.5")
        self.assertEqual(first.ipv6, "2001:db8::5")
        self.assertEqual(first.status, "running")
        self.assertEqual(first.monthly_cost, 6.0)
        self.assertEqual(first.created_at, "2026-01-02T03:04:05Z")

    def test_create_instance_sends_expected_payload(self) -> None:
        stub = self._stub({
            "https://api.digitalocean.com/v2/droplets": FakeResponse(
                202, {"droplet": dict(DROPLET, status="new", networks={}, root_password="pwd-1234")}),
        })
        spec = CreateSpec(name="web-09", region="sgp1", size="s-1vcpu-1gb",
                          image="debian-12-x64", tags=["demo"], user_data="#cloud-config")
        instance = self.provider.create_instance(spec)
        payload = stub.calls[-1]["json"]
        self.assertEqual(payload["name"], "web-09")
        self.assertEqual(payload["region"], "sgp1")
        self.assertEqual(payload["user_data"], "#cloud-config")
        self.assertEqual(payload["tags"], ["demo"])
        self.assertEqual(instance.status, "provisioning")
        self.assertEqual(instance.password, "pwd-1234")

    def test_create_instance_without_droplet_raises(self) -> None:
        self._stub({"https://api.digitalocean.com/v2/droplets": FakeResponse(200, {})})
        with self.assertRaises(CloudError) as ctx:
            self.provider.create_instance(CreateSpec(name="bad"))
        self.assertEqual(ctx.exception.key, "error.unexpected_response")

    def test_destroy_instance_calls_delete(self) -> None:
        stub = self._stub({"https://api.digitalocean.com/v2/droplets/123": FakeResponse(204, None)})
        self.provider.destroy_instance("123")
        self.assertEqual(stub.calls[-1]["method"], "DELETE")

    def test_size_alias_resolution(self) -> None:
        self._stub({"https://api.digitalocean.com/v2/sizes": FakeResponse(200, {"sizes": [
            {"slug": "s-1vcpu-1gb", "memory": 1024, "vcpus": 1, "price_monthly": 6.0},
            {"slug": "s-2vcpu-4gb", "memory": 4096, "vcpus": 2, "price_monthly": 24.0},
        ]})})
        self.assertEqual(self.provider.resolve_size("1gb"), "s-1vcpu-1gb")
        self.assertEqual(self.provider.resolve_size("2vcpu-4gb"), "s-2vcpu-4gb")
        with self.assertRaises(ValidationError) as ctx:
            self.provider.resolve_size("128gb")
        self.assertEqual(ctx.exception.key, "error.unknown_option")

    def test_region_alias_and_ambiguity(self) -> None:
        self._stub({"https://api.digitalocean.com/v2/regions": FakeResponse(200, {"regions": [
            {"slug": "sgp1"}, {"slug": "fra1"}, {"slug": "fra2"}, {"slug": "nyc3"},
        ]})})
        self.assertEqual(self.provider.resolve_region("sgp1"), "sgp1")
        self.assertEqual(self.provider.resolve_region("nyc"), "nyc3")   # 唯一前缀补全
        with self.assertRaises(ValidationError) as ctx:
            self.provider.resolve_region("fra")   # fra1 / fra2 有歧义
        self.assertEqual(ctx.exception.key, "error.ambiguous_option")
        with self.assertRaises(ValidationError):
            self.provider.resolve_region("moon1")

    def test_image_alias_shortcut_without_api(self) -> None:
        self._stub({})
        self.assertEqual(self.provider.resolve_image("ubuntu"), "ubuntu-24-04-x64")
        self.assertEqual(self.provider.resolve_image("debian"), "debian-12-x64")


# --------------------------------------------------------------------------- #
class AwsEc2Test(unittest.TestCase):
    DESCRIBE_XML = """
    <DescribeInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">
      <reservationSet>
        <item>
          <instancesSet>
            <item>
              <instanceId>i-0abc123</instanceId>
              <imageId>ami-0deadbeef</imageId>
              <instanceState><code>16</code><name>running</name></instanceState>
              <privateIpAddress>10.0.0.10</privateIpAddress>
              <ipAddress>198.51.100.10</ipAddress>
              <instanceType>t3.micro</instanceType>
              <launchTime>2026-01-02T03:04:05.000Z</launchTime>
              <placement><availabilityZone>us-east-1a</availabilityZone></placement>
              <tagSet>
                <item><key>Name</key><value>aws-web-01</value></item>
                <item><key>env</key><value>prod</value></item>
              </tagSet>
            </item>
            <item>
              <instanceId>i-0terminated</instanceId>
              <instanceState><name>terminated</name></instanceState>
            </item>
          </instancesSet>
        </item>
      </reservationSet>
    </DescribeInstancesResponse>
    """

    RUN_XML = """
    <RunInstancesResponse>
      <instancesSet>
        <item>
          <instanceId>i-0newinstance</instanceId>
          <imageId>ami-0123456789abcdef0</imageId>
          <instanceState><name>pending</name></instanceState>
          <instanceType>t3.micro</instanceType>
          <placement><availabilityZone>us-east-1a</availabilityZone></placement>
        </item>
      </instancesSet>
    </RunInstancesResponse>
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.settings = make_settings(self.tmp_path)
        self.provider = AwsEc2Provider({
            "access_key_id": "AKIAEXAMPLE",
            "secret_access_key": "secret",
            "region": "us-east-1",
        }, self.settings)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _inject(self, service: str, client: StubQueryClient, region: str = "us-east-1") -> None:
        self.provider._clients[f"{service}:{region}"] = client

    def test_validate_credentials_reports_account(self) -> None:
        client = StubQueryClient({"GetCallerIdentity": """
            <GetCallerIdentityResponse><GetCallerIdentityResult>
              <Arn>arn:aws:iam::123456789012:user/ops</Arn>
              <UserId>AIDAEXAMPLE</UserId>
              <Account>123456789012</Account>
            </GetCallerIdentityResult></GetCallerIdentityResponse>"""})
        self._inject("sts", client)
        identity = self.provider.validate_credentials()
        self.assertIn("123456789012", identity)
        self.assertIn("长期 IAM 凭证", identity)

    def test_list_instances_maps_xml_and_skips_terminated(self) -> None:
        client = StubQueryClient({"DescribeInstances": self.DESCRIBE_XML})
        self._inject("ec2", client)
        instances = self.provider.list_instances()
        self.assertEqual(len(instances), 1)
        instance = instances[0]
        self.assertEqual(instance.id, "i-0abc123")
        self.assertEqual(instance.name, "aws-web-01")
        self.assertEqual(instance.public_ip, "198.51.100.10")
        self.assertEqual(instance.region, "us-east-1")
        self.assertEqual(instance.status, "running")
        self.assertEqual(instance.tags, ["prod"])
        # 过滤条件确实下发了
        action, params = client.calls[-1]
        self.assertEqual(action, "DescribeInstances")
        self.assertEqual(params["Filter.1.Value.2"], "running")

    def test_get_instance_not_found(self) -> None:
        self._inject("ec2", StubQueryClient({"DescribeInstances": "<DescribeInstancesResponse/>"}))
        with self.assertRaises(NotFound):
            self.provider.get_instance("i-missing")

    def test_create_instance_payload_and_name_tag(self) -> None:
        client = StubQueryClient({"RunInstances": self.RUN_XML})
        self._inject("ec2", client)
        spec = CreateSpec(name="aws-web-02", region="us-east-1", size="t3.micro",
                          image="ami-0123456789abcdef0", tags=["env=prod"], user_data="#!/bin/sh")
        instance = self.provider.create_instance(spec)
        self.assertEqual(instance.id, "i-0newinstance")
        self.assertEqual(instance.status, "provisioning")
        _action, params = client.calls[-1]
        self.assertEqual(params["TagSpecification.1.Tag.1.Key"], "Name")
        self.assertEqual(params["TagSpecification.1.Tag.1.Value"], "aws-web-02")
        self.assertEqual(params["TagSpecification.1.Tag.2.Key"], "env")
        self.assertEqual(params["TagSpecification.1.Tag.2.Value"], "prod")
        self.assertEqual(params["UserData"], "IyEvYmluL3No")   # base64
        self.assertEqual(params["ImageId"], "ami-0123456789abcdef0")

    def test_create_instance_rejects_oversized_user_data(self) -> None:
        self._inject("ec2", StubQueryClient({"RunInstances": self.RUN_XML}))
        spec = CreateSpec(name="big", image="ami-0123456789abcdef0",
                          user_data="x" * (16 * 1024 + 1))
        with self.assertRaises(ValidationError) as ctx:
            self.provider.create_instance(spec)
        self.assertEqual(ctx.exception.key, "error.user_data_too_large")

    def test_resolve_image_alias_via_ssm(self) -> None:
        self._inject("ssm", StubQueryClient({"GetParameter": """
            <GetParameterResponse><GetParameterResult><Parameter>
              <Value>ami-0abcdef1234567890</Value>
            </Parameter></GetParameterResult></GetParameterResponse>"""}))
        self.assertEqual(self.provider.resolve_image("ubuntu"), "ami-0abcdef1234567890")
        self.assertEqual(self.provider.resolve_image("ami-direct"), "ami-direct")

    def test_resolve_image_alias_failure(self) -> None:
        self._inject("ssm", StubQueryClient({"GetParameter": "<GetParameterResponse/>"}))
        with self.assertRaises(ValidationError) as ctx:
            self.provider.resolve_image("ubuntu")
        self.assertEqual(ctx.exception.key, "error.ami_lookup_failed")
        self.assertIn("AWS_DEFAULT_AMI", ctx.exception.params["options"])

    def test_resolve_region_and_size_validation(self) -> None:
        self.assertEqual(self.provider.resolve_region("ap-northeast-1"), "ap-northeast-1")
        with self.assertRaises(ValidationError):
            self.provider.resolve_region("tokyo")
        self.assertEqual(self.provider.resolve_size("t3.small"), "t3.small")
        with self.assertRaises(ValidationError):
            self.provider.resolve_size("small")

    def test_destroy_instance_terminates(self) -> None:
        client = StubQueryClient({"TerminateInstances": "<TerminateInstancesResponse/>"})
        self._inject("ec2", client)
        self.provider.destroy_instance("i-0abc123")
        action, params = client.calls[-1]
        self.assertEqual(action, "TerminateInstances")
        self.assertEqual(params["InstanceId.1"], "i-0abc123")

    def test_multi_region_listing_collects_warnings(self) -> None:
        self.provider.secrets["regions"] = "us-east-1,eu-west-1"
        self._inject("ec2", StubQueryClient({"DescribeInstances": self.DESCRIBE_XML}))
        self._inject("ec2", StubQueryClient({}), region="eu-west-1")
        instances = self.provider.list_instances()
        self.assertEqual(len(instances), 1)
        self.assertTrue(self.provider.warnings)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
