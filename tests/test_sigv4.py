"""AWS SigV4 签名测试。

其中 ``test_aws_known_answer_get_vanilla`` 是**已知答案测试（KAT）**：
签名值取自 AWS 官方签名测试套件 / 文档中公开的 ``get-vanilla`` 用例，
因此它校验的不只是"我们的代码稳定"，而是"我们的签名与 AWS 实现一致"。
"""

from __future__ import annotations

import unittest

from support import ROOT  # noqa: F401

from cloudops.cloud.aws_sigv4 import (
    AwsCredentials,
    SigV4Signer,
    build_canonical_request,
    canonical_headers,
    canonical_query_string,
    derive_signing_key,
    sign_request,
    uri_encode,
)

# AWS 官方示例凭证与用例参数（公开文档，非真实密钥）
EXAMPLE_CREDENTIALS = AwsCredentials("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
EXAMPLE_AMZ_DATE = "20150830T123600Z"


class EncodingTest(unittest.TestCase):
    def test_uri_encode_keeps_unreserved_characters(self) -> None:
        self.assertEqual(uri_encode("aZ0-_.~"), "aZ0-_.~")

    def test_uri_encode_escapes_reserved_and_space(self) -> None:
        self.assertEqual(uri_encode("a b"), "a%20b")
        self.assertEqual(uri_encode("/a/b"), "%2Fa%2Fb")
        self.assertEqual(uri_encode("/a/b", encode_slash=False), "/a/b")

    def test_canonical_query_string_sorts_and_encodes(self) -> None:
        self.assertEqual(
            canonical_query_string({"b": "2", "a": "1", "c": "x y"}),
            "a=1&b=2&c=x%20y",
        )
        self.assertEqual(canonical_query_string({"k": ["b", "a"]}), "k=a&k=b")
        self.assertEqual(canonical_query_string(None), "")
        self.assertEqual(canonical_query_string({"skip": None}), "")

    def test_canonical_headers_lowercase_sorted_and_collapsed(self) -> None:
        text, names = canonical_headers({"X-Amz-Date": "20150830T123600Z",
                                         "Host": "example.amazonaws.com",
                                         "X-Amz-Meta": "  a   b  "})
        self.assertEqual(names, "host;x-amz-date;x-amz-meta")
        self.assertEqual(text, "host:example.amazonaws.com\n"
                                "x-amz-date:20150830T123600Z\n"
                                "x-amz-meta:a b\n")

    def test_canonical_headers_ignores_authorization(self) -> None:
        _text, names = canonical_headers({"Host": "h", "Authorization": "old"})
        self.assertEqual(names, "host")

    def test_derive_signing_key_is_stable(self) -> None:
        key = derive_signing_key(EXAMPLE_CREDENTIALS.secret_access_key,
                                "20150830", "us-east-1", "service")
        self.assertEqual(len(key), 32)
        self.assertEqual(key, derive_signing_key(EXAMPLE_CREDENTIALS.secret_access_key,
                                                "20150830", "us-east-1", "service"))
        self.assertNotEqual(key, derive_signing_key(EXAMPLE_CREDENTIALS.secret_access_key,
                                                   "20150830", "us-west-1", "service"))


class KnownAnswerTest(unittest.TestCase):
    """AWS 官方 ``get-vanilla`` 用例。"""

    def test_aws_known_answer_get_vanilla(self) -> None:
        headers = sign_request(
            method="GET",
            url="https://example.amazonaws.com/",
            region="us-east-1",
            service="service",
            credentials=EXAMPLE_CREDENTIALS,
            headers={"Host": "example.amazonaws.com"},
            amz_date=EXAMPLE_AMZ_DATE,
            content_sha256_header=False,   # 该用例的 SignedHeaders 仅 host;x-amz-date
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 "
            "Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        )

    def test_canonical_request_shape(self) -> None:
        request, signed = build_canonical_request(
            "GET", "/", "", {"host": "example.amazonaws.com",
                             "x-amz-date": EXAMPLE_AMZ_DATE},
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(signed, "host;x-amz-date")
        self.assertEqual(request.splitlines()[:3], ["GET", "/", ""])


class SignRequestTest(unittest.TestCase):
    def test_authorization_header_structure(self) -> None:
        signer = SigV4Signer(EXAMPLE_CREDENTIALS, "ap-northeast-1", "ec2")
        headers = signer.sign("POST", "https://ec2.ap-northeast-1.amazonaws.com/",
                              headers={"content-type": "application/x-www-form-urlencoded"},
                              body="Action=DescribeInstances&Version=2016-11-15",
                              amz_date=EXAMPLE_AMZ_DATE)
        authorization = headers["Authorization"]
        self.assertTrue(authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/"))
        self.assertIn("/ap-northeast-1/ec2/aws4_request", authorization)
        self.assertIn("SignedHeaders=", authorization)
        self.assertIn("Signature=", authorization)
        self.assertEqual(headers["x-amz-date"], EXAMPLE_AMZ_DATE)
        self.assertIn("x-amz-content-sha256", headers)

    def test_session_token_is_signed_in(self) -> None:
        temporary = AwsCredentials("AKIDEXAMPLE", "secret", session_token="SESSIONTOKEN")
        headers = sign_request(method="POST", url="https://ec2.us-east-1.amazonaws.com/",
                               region="us-east-1", service="ec2", credentials=temporary,
                               amz_date=EXAMPLE_AMZ_DATE)
        self.assertEqual(headers["x-amz-security-token"], "SESSIONTOKEN")
        self.assertIn("x-amz-security-token", headers["Authorization"])

    def test_body_change_changes_signature(self) -> None:
        def signature(body: str) -> str:
            headers = sign_request(method="POST", url="https://ec2.us-east-1.amazonaws.com/",
                                   region="us-east-1", service="ec2",
                                   credentials=EXAMPLE_CREDENTIALS, body=body,
                                   amz_date=EXAMPLE_AMZ_DATE)
            return headers["Authorization"].rsplit("Signature=", 1)[1]

        self.assertNotEqual(signature("Action=A"), signature("Action=B"))

    def test_region_change_changes_signature(self) -> None:
        common = dict(method="POST", url="https://ec2.us-east-1.amazonaws.com/",
                      service="ec2", credentials=EXAMPLE_CREDENTIALS,
                      amz_date=EXAMPLE_AMZ_DATE)
        east = sign_request(region="us-east-1", **common)["Authorization"]
        west = sign_request(region="us-west-2", **common)["Authorization"]
        self.assertNotEqual(east, west)

    def test_content_sha256_header_is_optional(self) -> None:
        without = sign_request(method="GET", url="https://example.amazonaws.com/",
                               region="us-east-1", service="service",
                               credentials=EXAMPLE_CREDENTIALS, amz_date=EXAMPLE_AMZ_DATE,
                               content_sha256_header=False)
        self.assertNotIn("x-amz-content-sha256", without)


class CredentialsTest(unittest.TestCase):
    def test_missing_fields_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AwsCredentials("", "secret")
        with self.assertRaises(ValueError):
            AwsCredentials("AKIA", "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
