"""配置页 Schema 的完整性测试。

只验证「所有配置项都在页面上、都有中文标签与提示、页签覆盖全部分块」，
不校验具体默认值（那是配置模型自己的事）。
"""

from __future__ import annotations

from typing import Set, Tuple
from unittest import TestCase

from emoji_label_bridge.plugin import EmojiLabelBridgePlugin

EXPECTED_FIELDS: Set[Tuple[str, str]] = {
    ("plugin", "enabled"),
    ("plugin", "config_version"),
    ("behavior", "miss_policy"),
    ("behavior", "dedupe_with_existing_emoji"),
    ("behavior", "max_tags_per_message"),
    ("match", "local_threshold"),
    ("match", "enable_vector"),
    ("match", "vector_threshold"),
    ("match", "vector_timeout_ms"),
    ("match", "only_whole_message"),
    ("match", "require_char_overlap"),
    ("match", "candidate_limit"),
    ("index", "enabled"),
    ("index", "embedding_task"),
    ("index", "sync_interval_seconds"),
    ("index", "startup_delay_seconds"),
    ("index", "bootstrap_interval_seconds"),
    ("index", "batch_size"),
    ("index", "max_items_per_sync"),
    ("index", "max_concurrent"),
    ("index", "description_max_chars"),
    ("index", "cache_size"),
    ("index", "cache_ttl_seconds"),
}


class ConfigSchemaTest(TestCase):
    """WebUI 配置页 Schema。"""

    def setUp(self) -> None:
        self.schema = EmojiLabelBridgePlugin.build_config_schema()

    def test_all_fields_are_shown(self) -> None:
        """配置模型里的每个字段都必须出现在页面上。"""

        sections = self.schema["sections"]
        shown = {(section["name"], name) for section in sections.values() for name in section["fields"]}
        self.assertEqual(shown, EXPECTED_FIELDS)

    def test_every_field_has_chinese_label_and_hint(self) -> None:
        """每个字段都要有非空的中文标签与提示。"""

        for section in self.schema["sections"].values():
            for name, field in section["fields"].items():
                self.assertTrue(field.get("label"), f"{name} 缺 label")
                self.assertTrue(field.get("hint"), f"{name} 缺 hint")
                self.assertTrue(field.get("description"), f"{name} 缺 description")

    def test_miss_policy_is_select(self) -> None:
        """miss_policy 必须是下拉选择，选项与模型定义一致。"""

        field = self.schema["sections"]["behavior"]["fields"]["miss_policy"]
        self.assertEqual(field["choices"], ["drop", "keep", "abort"])
        self.assertEqual(field["widget"], "select")

    def test_config_version_is_disabled(self) -> None:
        """配置版本由插件维护，页面上置灰。"""

        field = self.schema["sections"]["plugin_info"]["fields"]["config_version"]
        self.assertTrue(field.get("disabled"))

    def test_tabs_cover_all_sections(self) -> None:
        """每个展示分块都要挂在某个页签下，不能有孤儿。"""

        tabbed = {section_id for tab in self.schema["layout"]["tabs"] for section_id in tab["sections"]}
        self.assertEqual(tabbed, set(self.schema["sections"]))

    def test_source_sections_keep_real_names(self) -> None:
        """拆成多个展示块后，name 仍指向真实配置节。"""

        sections = self.schema["sections"]
        self.assertEqual(sections["match_core"]["name"], "match")
        self.assertEqual(sections["match_adv"]["name"], "match")
        self.assertEqual(sections["index_adv"]["name"], "index")
