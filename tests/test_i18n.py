"""i18n 一致性测试（静态审计 + 渲染校验）。

用户最容易被这种东西伤到：代码里用了 ``t("pending.hint", …)``，词表里却没这条，
界面就直接显示 ``pending.hint`` 这个 key。这里用"扫源码 + 比对词表"的方式把它们
在提交前全部挖出来。
"""

from __future__ import annotations

import re
import string
import unittest
from pathlib import Path

from support import ROOT

from cloudops.i18n import DEFAULT_LANGUAGE, MESSAGES, SUPPORTED_LANGUAGES, get_translator

PACKAGE = Path(ROOT) / "cloudops"

#: 匹配 t("xxx") / translate("xxx") / _("xxx") 这类调用
CALL_PATTERN = re.compile(r"""(?:\bt|\btranslate|\b_)\s*\(\s*(['"])([a-zA-Z0-9_.\-]+)\1""")
#: 匹配 key="xxx" / default_key = "xxx"
KEY_PATTERN = re.compile(r"""\b(?:key|default_key)\s*=\s*(['"])([a-zA-Z0-9_.\-]+)\1""")

#: 这些不是 i18n key，属于误报
IGNORED = {
    "key",              # t(key=...) 动态
    "utf-8",            # 编码名
    "text/plain",
}


def _iter_source_files():
    for path in sorted(PACKAGE.rglob("*.py")):
        yield path


def _collect_keys() -> "dict[str, list[str]]":
    """收集源码里引用到的 i18n key。

    ``i18n.py`` 自身只放词表，里面的 ``key="…"`` 都出现在文档字符串的调用示例中，
    所以只对非词表文件应用 ``KEY_PATTERN``（避免把示例里的形参名当成 key）。
    """
    found: dict[str, list[str]] = {}
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8")
        patterns = [CALL_PATTERN] if path.name == "i18n.py" else [CALL_PATTERN, KEY_PATTERN]
        for pattern in patterns:
            for _quote, key in pattern.findall(text):
                if key in IGNORED:
                    continue
                found.setdefault(key, []).append(path.relative_to(PACKAGE).as_posix())
    return found


class VocabularyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.zh = MESSAGES["zh"]
        self.en = MESSAGES["en"]
        self.used = _collect_keys()

    def test_supported_languages_are_present(self) -> None:
        self.assertEqual(SUPPORTED_LANGUAGES, ("zh", "en"))
        self.assertEqual(DEFAULT_LANGUAGE, "zh")
        for lang in SUPPORTED_LANGUAGES:
            self.assertIn(lang, MESSAGES)

    def test_zh_and_en_have_identical_key_sets(self) -> None:
        only_zh = sorted(set(self.zh) - set(self.en))
        only_en = sorted(set(self.en) - set(self.zh))
        self.assertEqual(only_zh, [], f"仅中文有：{only_zh}")
        self.assertEqual(only_en, [], f"仅英文有：{only_en}")

    def test_every_used_key_exists(self) -> None:
        missing = {key: files for key, files in self.used.items() if key not in self.zh}
        self.assertEqual(missing, {}, f"代码里用到但词表缺失：{sorted(missing)}")

    def test_placeholders_match_across_languages(self) -> None:
        def fields(template: str) -> set:
            return {
                name for _literal, name, _spec, _conv
                in string.Formatter().parse(template) if name
            }

        mismatched = {
            key: (sorted(fields(self.zh[key])), sorted(fields(self.en[key])))
            for key in set(self.zh) & set(self.en)
            if fields(self.zh[key]) != fields(self.en[key])
        }
        self.assertEqual(mismatched, {}, f"中英文占位符不一致：{mismatched}")

    def test_no_orphan_keys(self) -> None:
        """词表里的 key 至少要被用一次（防止死文案越积越多）。"""
        orphans = sorted(key for key in self.zh if key not in self.used)
        self.assertEqual(orphans, [], f"未被引用的文案：{orphans}")

    def test_templates_are_non_empty_strings(self) -> None:
        # 少数"标记"类文案故意是空白（例如凭证列表里表示"未启用"的空位）
        allow_blank = {"creds.inactive_marker"}
        for language, table in MESSAGES.items():
            for key, template in table.items():
                self.assertIsInstance(template, str, key)
                if key in allow_blank:
                    continue
                self.assertTrue(template.strip(), f"{language}:{key} 是空文案")


class TranslatorTest(unittest.TestCase):
    def test_falls_back_to_default_language(self) -> None:
        zh = get_translator("zh")
        self.assertEqual(zh("error.generic", detail="x"), MESSAGES["zh"]["error.generic"].format(detail="x"))

    def test_unknown_language_uses_default(self) -> None:
        self.assertEqual(get_translator("fr")("error.generic", detail="x"),
                         MESSAGES["zh"]["error.generic"].format(detail="x"))

    def test_english_translation_is_used(self) -> None:
        en = get_translator("en")
        self.assertNotEqual(en("error.generic", detail="x"), MESSAGES["zh"]["error.generic"])

    def test_key_with_literal_key_placeholder(self) -> None:
        """文案里允许出现名为 ``key`` 的占位符（曾与形参名冲突过）。"""
        text = get_translator("zh")("status.line", key="region", value="sgp1")
        self.assertIn("region", text)
        self.assertIn("sgp1", text)

    def test_unknown_key_returns_key(self) -> None:
        self.assertEqual(get_translator("zh")("nope.not.here"), "nope.not.here")

    def test_missing_format_parameter_degrades(self) -> None:
        """占位符没给全时不抛异常（宁可显示模板，也不要把用户打挂）。"""
        text = get_translator("zh")("error.generic")
        self.assertIsInstance(text, str)
        self.assertTrue(text)


class ErrorRenderTest(unittest.TestCase):
    """异常渲染：用户不该看到 key，也不该看到未填充的 {placeholder}。"""

    def test_default_validation_error_renders_detail(self) -> None:
        from cloudops.errors import ValidationError

        t = get_translator("zh")
        text = ValidationError(params={"value": "moon1", "parameter": "区域",
                                       "options": "sgp1, fra1"}).render(t)
        self.assertNotIn("{", text)
        self.assertIn("moon1", text)

    def test_bare_error_falls_back_to_generic(self) -> None:
        from cloudops.errors import CloudOpsError

        t = get_translator("zh")
        text = CloudOpsError("boom").render(t)
        self.assertEqual(text, "boom")

    def test_unknown_key_never_leaks(self) -> None:
        from cloudops.errors import CloudOpsError

        text = CloudOpsError(key="totally.unknown.key").render(get_translator("zh"))
        self.assertNotEqual(text.strip(), "totally.unknown.key")
        self.assertNotIn("{", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
