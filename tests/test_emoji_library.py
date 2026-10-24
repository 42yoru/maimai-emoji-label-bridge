"""表情库记录构造与本地匹配的单测。"""

from __future__ import annotations

from unittest import TestCase

from emoji_label_bridge.emoji_library import (
    STAGE_EXACT,
    STAGE_LOCAL,
    EmojiLibrary,
    EmojiRecord,
    build_records,
    has_char_overlap,
)
from emoji_label_bridge.label_text import normalize_tag_text, split_tags


def _record(description: str, emoji_hash: str = "") -> EmojiRecord:
    """构造一条测试用表情记录。"""

    return EmojiRecord(
        emoji_hash=emoji_hash or f"hash-{description}",
        description=description,
        tags=split_tags(description),
        search_text=normalize_tag_text(description),
    )


class BuildRecordsTest(TestCase):
    """数据库行到表情记录的转换。"""

    def test_filters_invalid_rows(self) -> None:
        """缺 hash、空描述、脏描述、文件丢失的记录都要跳过。"""

        rows = [
            {"image_hash": "a", "description": "亲亲,卖萌"},
            {"image_hash": "", "description": "没有 hash"},
            {"image_hash": "b", "description": "   "},
            {"image_hash": "c", "description": "文件没了", "no_file_flag": True},
            {"image_hash": "d", "description": "[表情包：,<|begin_of_box|>这张表情包是**"},
        ]
        records = build_records(rows)
        self.assertEqual([record.emoji_hash for record in records], ["a"])
        self.assertEqual(records[0].tags, ("亲亲", "卖萌"))


class ExactMatchTest(TestCase):
    """精确档位。"""

    def test_match_exact_by_full_description(self) -> None:
        """标签串与描述完全一致时命中精确档。"""

        library = EmojiLibrary([_record("亲亲,卖萌,害羞,撒娇,喜爱", "hash-a")])
        match = library.match_exact("亲亲，卖萌，害羞，撒娇，喜爱")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.stage, STAGE_EXACT)
        self.assertEqual(match.record.emoji_hash, "hash-a")

    def test_match_exact_by_single_tag(self) -> None:
        """只写一个标签时按标签索引命中。"""

        library = EmojiLibrary([_record("亲亲,卖萌", "hash-a")])
        match = library.match_exact("卖萌")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.record.emoji_hash, "hash-a")

    def test_match_exact_missing(self) -> None:
        """库为空时精确匹配返回 None。"""

        self.assertIsNone(EmojiLibrary([]).match_exact("亲亲"))


class LocalMatchTest(TestCase):
    """本地标签重叠档位。"""

    def test_match_local_above_threshold(self) -> None:
        """标签重叠度越过阈值时命中本地档。"""

        library = EmojiLibrary([_record("亲亲,卖萌,害羞", "hash-a")])
        match = library.match_local("亲亲,卖萌", threshold=0.5)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.stage, STAGE_LOCAL)
        self.assertEqual(match.record.emoji_hash, "hash-a")

    def test_match_local_below_threshold(self) -> None:
        """毫无关系的标签不能靠本地档瞎命中。"""

        library = EmojiLibrary([_record("生气,愤怒,摔桌", "hash-a")])
        self.assertIsNone(library.match_local("亲亲,卖萌", threshold=0.5))


class CandidateAndLookupTest(TestCase):
    """粗筛候选与 hash 回查。"""

    def test_coarse_candidates_prefers_overlap(self) -> None:
        """粗筛候选按字符重叠度排序，且完全没有重叠时返回空。"""

        library = EmojiLibrary(
            [
                _record("可可爱爱摇头晃脑", "hash-a"),
                _record("角落里的猫猫", "hash-b"),
                _record("生气摔桌", "hash-c"),
            ]
        )
        candidates = library.coarse_candidates("猫猫", limit=2)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].emoji_hash, "hash-b")
        self.assertEqual(library.coarse_candidates("zzz", limit=2), [])

    def test_find_by_hash(self) -> None:
        """按 hash 回查记录，未知 hash 返回 None。"""

        library = EmojiLibrary([_record("亲亲", "hash-a")])
        self.assertIsNotNone(library.find_by_hash("hash-a"))
        self.assertIsNone(library.find_by_hash("hash-x"))
        self.assertIsNone(library.find_by_hash(""))

    def test_has_char_overlap(self) -> None:
        """字符重叠判断要能挡住完全无关的字符串。"""

        self.assertTrue(has_char_overlap("傲娇脸红到不行", "害羞,可爱,开心,脸红,卖萌"))
        self.assertFalse(has_char_overlap("今天天气真好啊", "摸头杀前置帧"))
