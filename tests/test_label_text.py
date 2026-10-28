"""正文标签解析与标签切分的单测。"""

from __future__ import annotations

from unittest import TestCase

from emoji_label_bridge.label_text import (
    TAG_KIND,
    TEXT_KIND,
    build_tag_text,
    normalize_tag_text,
    parse_segments,
    split_tags,
)


class LabelTextTest(TestCase):
    """标签解析的边界情况。"""

    def test_parse_whole_tag(self) -> None:
        """整条就是一条标签时，应只产出一个标签片段。"""

        segments = parse_segments("[表情包: 亲亲,卖萌,害羞,撒娇,喜爱]")
        self.assertEqual([segment.kind for segment in segments], [TAG_KIND])
        self.assertEqual(segments[0].text, "亲亲,卖萌,害羞,撒娇,喜爱")

    def test_parse_tag_between_text(self) -> None:
        """标签夹在正文中间时，文字与标签都要保留原顺序。"""

        segments = parse_segments("好的呀[表情包: 害羞]那就这样")
        self.assertEqual([segment.kind for segment in segments], [TEXT_KIND, TAG_KIND, TEXT_KIND])
        self.assertEqual(segments[0].text, "好的呀")
        self.assertEqual(segments[1].text, "害羞")
        self.assertEqual(segments[2].text, "那就这样")

    def test_parse_full_width_colon_and_empty_desc(self) -> None:
        """全角冒号与无描述标签都要能识别。"""

        self.assertEqual(parse_segments("[表情包：卖萌]")[0].text, "卖萌")
        empty = parse_segments("[表情包]")
        self.assertEqual(len(empty), 1)
        self.assertTrue(empty[0].is_tag)
        self.assertEqual(empty[0].text, "")

    def test_parse_supports_spaces_inside_brackets(self) -> None:
        """标签里带空格也要识别。"""

        segments = parse_segments("[ 表情包 : 亲亲 ]")
        self.assertTrue(segments[0].is_tag)
        self.assertEqual(segments[0].text, "亲亲")

    def test_parse_ignores_normal_brackets(self) -> None:
        """普通方括号内容不能被误判成标签。"""

        segments = parse_segments("[笑] 好的")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].kind, TEXT_KIND)

    def test_split_tags_dedup_and_normalize(self) -> None:
        """标签切分要去空、去重并统一全角逗号。"""

        self.assertEqual(split_tags("亲亲，卖萌、亲亲,  、害羞"), ("亲亲", "卖萌", "害羞"))
        self.assertEqual(split_tags("   "), ())

    def test_normalize_tag_text(self) -> None:
        """归一化只影响比较，不改变展示。"""

        self.assertEqual(normalize_tag_text(" 亲亲，  卖萌 "), "亲亲, 卖萌")

    def test_build_tag_text(self) -> None:
        """表情组件的可见文本要按宿主入站渲染格式拼出来。"""

        self.assertEqual(build_tag_text("亲亲"), "[表情包: 亲亲]")
        self.assertEqual(build_tag_text(""), "[表情包]")
