"""插件清单 `_manifest.json` 的静态校验。"""

from __future__ import annotations

from unittest import TestCase

import json

from . import PLUGIN_DIR

MANIFEST = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))


class ManifestTest(TestCase):
    """清单字段与市场提交要求保持一致。"""

    def test_required_v2_fields(self) -> None:
        """v2 必需字段一个都不能少。"""

        required = {
            "manifest_version",
            "id",
            "version",
            "name",
            "description",
            "author",
            "license",
            "urls",
            "host_application",
            "sdk",
            "capabilities",
            "i18n",
        }
        self.assertTrue(required.issubset(MANIFEST.keys()), required - set(MANIFEST.keys()))
        self.assertEqual(MANIFEST["manifest_version"], 2)

    def test_plugin_type_matches_function(self) -> None:
        """发送层媒体处理应归为 media 类型。"""

        self.assertEqual(MANIFEST["plugin_type"], "media")

    def test_capabilities_are_minimal(self) -> None:
        """只声明实际用到的两项能力。"""

        self.assertEqual(sorted(MANIFEST["capabilities"]), ["database.query", "llm.embed"])

    def test_repository_url_is_https(self) -> None:
        """仓库地址必须是公开 HTTPS 地址。"""

        repository = MANIFEST["urls"]["repository"]
        self.assertTrue(repository.startswith("https://github.com/"))
        self.assertFalse(repository.endswith(".git"))

    def test_icon_is_local_png(self) -> None:
        """本地图标文件必须真实存在，扩展名受宿主支持。"""

        icon = MANIFEST["display"]["icon"]
        self.assertEqual(icon["type"], "local")
        self.assertEqual(icon["value"], "icon.png")
        self.assertTrue((PLUGIN_DIR / icon["value"]).is_file())

    def test_version_is_semver(self) -> None:
        """版本必须是三段式，市场校验按此匹配 Release Tag。"""

        parts = MANIFEST["version"].split(".")
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.isdigit() for part in parts))
