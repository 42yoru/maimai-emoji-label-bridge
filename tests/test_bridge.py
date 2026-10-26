"""向量索引与发送层 Hook 的端到端单测。

用假的嵌入函数代替宿主能力，验证三档匹配、未命中策略、组件重建
与增量同步行为。测试不启动宿主，也不需要网络。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence, Tuple
from unittest import IsolatedAsyncioTestCase

import logging
import tempfile

from emoji_label_bridge.emoji_library import EmojiLibrary, EmojiRecord
from emoji_label_bridge.label_text import normalize_tag_text, split_tags
from emoji_label_bridge.plugin import EmojiLabelBridgePlugin
from emoji_label_bridge.vector_index import VectorIndex

LOGGER = logging.getLogger("emoji_label_bridge.test")


def _record(description: str, emoji_hash: str) -> EmojiRecord:
    """构造测试用表情记录。"""

    return EmojiRecord(
        emoji_hash=emoji_hash,
        description=description,
        tags=split_tags(description),
        search_text=normalize_tag_text(description),
    )


def _vector_for(text: str) -> List[float]:
    """假的语义空间：亲亲/撒娇 与 生气/愤怒 分成两个方向。"""

    if any(keyword in text for keyword in ("亲亲", "撒娇", "摸摸头", "摸头")):
        return [1.0, 0.0]
    return [0.0, 1.0]


class _FakeEmbedder:
    """记录调用次数的假嵌入服务。"""

    def __init__(self, model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self.text_calls: List[str] = []
        self.batch_calls: List[List[str]] = []

    async def embed_text(self, text: str) -> List[float]:
        self.text_calls.append(text)
        return _vector_for(text)

    async def embed_texts(self, texts: List[str], max_concurrent: int) -> Tuple[List[List[float]], str]:
        del max_concurrent
        self.batch_calls.append(list(texts))
        return [_vector_for(text) for text in texts], self.model_name


def _make_message(*components: Dict[str, Any]) -> Dict[str, Any]:
    """构造一条出站消息字典。"""

    return {
        "message_id": "m-1",
        "session_id": "s-1",
        "raw_message": [dict(component) for component in components],
    }


def _text(data: str) -> Dict[str, Any]:
    """文本组件。"""

    return {"type": "text", "data": data}


def _emoji(data: str = "[表情包: 亲亲]") -> Dict[str, Any]:
    """表情组件。"""

    return {"type": "emoji", "data": data, "hash": "existing-hash"}


def _build_plugin(data_dir: Path, **config_overrides: Any) -> EmojiLabelBridgePlugin:
    """构造注入了假上下文的插件实例。"""

    plugin = EmojiLabelBridgePlugin()
    context = SimpleNamespace(logger=LOGGER, paths=SimpleNamespace(data_dir=data_dir))
    plugin._set_context(context)
    config: Dict[str, Any] = {"plugin": {"enabled": True, "config_version": "0.1.0"}}
    config.update(config_overrides)
    plugin.set_plugin_config(config)
    return plugin


async def _build_indexed_plugin(
    data_dir: Path,
    records: Sequence[EmojiRecord],
    **config_overrides: Any,
) -> Tuple[EmojiLabelBridgePlugin, _FakeEmbedder]:
    """构造带已就绪向量索引的插件。"""

    embedder = _FakeEmbedder()
    index = VectorIndex(data_dir / "emoji_vectors.json", data_dir / "label_cache.json", logger_=LOGGER)
    index.load()
    stats = await index.sync(
        records,
        embedder.embed_texts,
        batch_size=32,
        max_items=32,
        max_concurrent=2,
        cache_ttl_seconds=3600,
        cache_size=16,
    )
    assert stats.ready

    plugin = _build_plugin(data_dir, **config_overrides)
    plugin._library = EmojiLibrary(records)
    plugin._index = index
    plugin._embed_text = embedder.embed_text  # type: ignore[method-assign]
    return plugin, embedder


class _BridgeTestBase(IsolatedAsyncioTestCase):
    """共用临时目录。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)

    @property
    def data_dir(self) -> Path:
        """当前用例的临时数据目录。"""

        return Path(self._tmpdir.name)


class HookTest(_BridgeTestBase):
    """发送层 Hook 的组件改写行为。"""

    async def test_plain_message_untouched(self) -> None:
        """没有标签的消息完全不动，也不回传 payload。"""

        plugin = _build_plugin(self.data_dir)
        result = await plugin.bridge_after_build_message(message=_make_message(_text("晚安")), stream_id="s-1")
        self.assertEqual(result, {"action": "continue"})

    async def test_exact_tag_becomes_emoji(self) -> None:
        """精确命中时组件换成表情，hash 与可见文本都要正确。"""

        record = _record("亲亲,卖萌,害羞,撒娇,喜爱", "hash-real")
        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([record])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 亲亲,卖萌,害羞,撒娇,喜爱]")),
            stream_id="s-1",
        )

        self.assertEqual(result["action"], "continue")
        message = result["modified_kwargs"]["message"]
        components = message["raw_message"]
        self.assertEqual(
            components,
            [{"type": "emoji", "data": "[表情包: 亲亲,卖萌,害羞,撒娇,喜爱]", "hash": "hash-real"}],
        )
        # 整条消息只剩一个表情：宿主才会按表情消息落库
        self.assertTrue(message["is_emoji"])
        # 历史里的纯文本也要换成真表情的渲染格式，不能留着假标签
        self.assertEqual(message["processed_plain_text"], "[表情包: 亲亲,卖萌,害羞,撒娇,喜爱]")

    async def test_mixed_message_is_not_marked_as_emoji(self) -> None:
        """正文里夹表情的混合消息，is_emoji 要保持 False，但假标签仍要被换掉。"""

        record = _record("害羞", "hash-real")
        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([record])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("好呀[表情包: 害羞]")),
            stream_id="s-1",
        )

        message = result["modified_kwargs"]["message"]
        self.assertFalse(message["is_emoji"])
        self.assertEqual(message["raw_message"][0]["data"], "好呀")
        self.assertEqual(message["raw_message"][1]["type"], "emoji")

    async def test_plain_text_untouched_when_at_present(self) -> None:
        """带 at 组件的消息不接管 processed_plain_text，交给宿主自己拼。"""

        record = _record("害羞", "hash-real")
        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([record])
        message = _make_message(_text("[表情包: 害羞]"), {"type": "at", "data": "123456"})
        message["processed_plain_text"] = "原文"

        result = await plugin.bridge_after_build_message(message=message, stream_id="s-1")

        self.assertEqual(result["modified_kwargs"]["message"]["processed_plain_text"], "原文")

    async def test_fullwidth_brackets_are_supported(self) -> None:
        """全角方括号写法的假标签也要能接住。"""

        record = _record("害羞", "hash-real")
        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([record])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("［表情包: 害羞］")),
            stream_id="s-1",
        )

        self.assertEqual(result["action"], "continue")
        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components[0]["type"], "emoji")
        self.assertEqual(components[0]["hash"], "hash-real")

    async def test_local_tag_becomes_emoji(self) -> None:
        """标签部分重叠也能命中本地档。"""

        record = _record("亲亲,卖萌,害羞", "hash-local")
        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([record])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 亲亲,卖萌]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components[0]["type"], "emoji")
        self.assertEqual(components[0]["hash"], "hash-local")

    async def test_text_around_tag_kept_in_order(self) -> None:
        """句中标签命中后，文字与表情按原顺序重新拼起来。"""

        record = _record("害羞", "hash-blush")
        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([record])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("先说一句[表情包: 害羞]再说")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual([component["type"] for component in components], ["text", "emoji", "text"])
        self.assertEqual(components[0]["data"], "先说一句")
        self.assertEqual(components[2]["data"], "再说")

    async def test_miss_drop_removes_tag(self) -> None:
        """未命中 + drop：标签消失，文字照常发送。"""

        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("好的[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components, [{"type": "text", "data": "好的"}])

    async def test_miss_keep_keeps_tag_text(self) -> None:
        """未命中 + keep：保留原标签文字。"""

        plugin = _build_plugin(self.data_dir, behavior={"miss_policy": "keep"})
        plugin._library = EmojiLibrary([])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components, [{"type": "text", "data": "[表情包: 摸摸头]"}])

    async def test_miss_abort_cancels_message(self) -> None:
        """未命中 + abort：整条消息取消，且不回传 payload。"""

        plugin = _build_plugin(self.data_dir, behavior={"miss_policy": "abort"})
        plugin._library = EmojiLibrary([])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("前半段[表情包: 摸摸头]后半段")),
            stream_id="s-1",
        )

        self.assertEqual(result, {"action": "abort"})

    async def test_only_tag_left_aborts_message(self) -> None:
        """整条消息只有一个未命中标签时，没有内容可发，直接取消。"""

        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        self.assertEqual(result, {"action": "abort"})

    async def test_dedupe_with_existing_emoji(self) -> None:
        """消息里已有真表情时把标签删掉，避免同一句发两张图。"""

        plugin = _build_plugin(self.data_dir)
        plugin._library = EmojiLibrary([_record("害羞", "hash-blush")])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("这句带标签[表情包: 害羞]"), _emoji()),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual([component["type"] for component in components], ["text", "emoji"])
        self.assertEqual(components[0]["data"], "这句带标签")

    async def test_max_tags_per_message(self) -> None:
        """超出上限的标签直接丢弃，不再逐个匹配。"""

        plugin = _build_plugin(self.data_dir, behavior={"max_tags_per_message": 1})
        plugin._library = EmojiLibrary([_record("害羞", "hash-blush")])

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 害羞][表情包: 害羞]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["type"], "emoji")


class VectorTest(_BridgeTestBase):
    """向量兜底档位。"""

    async def test_vector_fallback_hits(self) -> None:
        """前两档未命中、整条纯标签时，用嵌入向量兜底命中。"""

        records = [_record("摸头杀前置帧,赛博萌妹求rua", "hash-a"), _record("生气,愤怒", "hash-b")]
        plugin, embedder = await _build_indexed_plugin(self.data_dir, records)

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components[0]["type"], "emoji")
        self.assertEqual(components[0]["hash"], "hash-a")
        self.assertEqual(embedder.text_calls, ["摸摸头"])

    async def test_vector_skipped_when_tag_in_sentence(self) -> None:
        """标签夹在正文里时不做向量兜底，保护正文发送速度。"""

        records = [_record("摸头杀前置帧", "hash-a")]
        plugin, embedder = await _build_indexed_plugin(self.data_dir, records)

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("先说一句[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components, [{"type": "text", "data": "先说一句"}])
        self.assertEqual(embedder.text_calls, [])

    async def test_vector_cache_avoids_second_request(self) -> None:
        """同一个标签第二次出现时直接用缓存，不再请求嵌入。"""

        records = [_record("摸头杀前置帧", "hash-a")]
        plugin, embedder = await _build_indexed_plugin(self.data_dir, records)

        for _ in range(2):
            await plugin.bridge_after_build_message(
                message=_make_message(_text("[表情包: 摸摸头]")),
                stream_id="s-1",
            )

        self.assertEqual(embedder.text_calls, ["摸摸头"])

    async def test_vector_requires_char_overlap(self) -> None:
        """向量分数虽然高，但标签与描述毫无字面重叠时要拒绝。"""

        records = [_record("亲亲,撒娇", "hash-a")]
        plugin, embedder = await _build_indexed_plugin(self.data_dir, records)

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        self.assertEqual(result, {"action": "abort"})
        # 一个字符都对不上，字面门槛必然过不了：连嵌入都不该请求
        self.assertEqual(embedder.text_calls, [])

    async def test_vector_char_overlap_disabled(self) -> None:
        """关掉字面门槛后，同样的输入就允许向量命中。"""

        records = [_record("亲亲,撒娇", "hash-a")]
        plugin, _ = await _build_indexed_plugin(self.data_dir, records, match={"require_char_overlap": False})

        result = await plugin.bridge_after_build_message(
            message=_make_message(_text("[表情包: 摸摸头]")),
            stream_id="s-1",
        )

        components = result["modified_kwargs"]["message"]["raw_message"]
        self.assertEqual(components[0]["type"], "emoji")
        self.assertEqual(components[0]["hash"], "hash-a")


class IndexSyncTest(_BridgeTestBase):
    """向量索引的增量同步。"""

    async def test_sync_is_incremental(self) -> None:
        """重复同步不重复计算，新增与移除按清单 diff。"""

        embedder = _FakeEmbedder()
        index = VectorIndex(self.data_dir / "emoji_vectors.json", self.data_dir / "label_cache.json", logger_=LOGGER)
        index.load()

        first = [_record("亲亲", "hash-a"), _record("生气", "hash-b")]
        stats = await index.sync(
            first,
            embedder.embed_texts,
            batch_size=8,
            max_items=8,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertEqual(stats.added, 2)
        self.assertTrue(stats.ready)

        again = await index.sync(
            first,
            embedder.embed_texts,
            batch_size=8,
            max_items=8,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertEqual(again.added, 0)
        self.assertTrue(again.ready)

        extended = [*first, _record("害羞", "hash-c")]
        stats = await index.sync(
            extended,
            embedder.embed_texts,
            batch_size=8,
            max_items=8,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertEqual(stats.added, 1)

        stats = await index.sync(
            [extended[0]],
            embedder.embed_texts,
            batch_size=8,
            max_items=8,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertEqual(stats.removed, 2)
        self.assertEqual(index.size, 1)

        reloaded = VectorIndex(self.data_dir / "emoji_vectors.json", self.data_dir / "label_cache.json", logger_=LOGGER)
        reloaded.load()
        self.assertEqual(reloaded.size, 1)

    async def test_sync_respects_max_items_budget(self) -> None:
        """单轮只补预算内的条数，其余留给下一轮。"""

        embedder = _FakeEmbedder()
        index = VectorIndex(self.data_dir / "emoji_vectors.json", self.data_dir / "label_cache.json", logger_=LOGGER)
        index.load()

        records = [_record("亲亲", f"hash-{index_}") for index_ in range(5)]
        stats = await index.sync(
            records,
            embedder.embed_texts,
            batch_size=2,
            max_items=2,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertEqual(stats.added, 2)
        self.assertEqual(stats.pending, 3)
        self.assertFalse(stats.ready)

    async def test_ready_flag_survives_reload(self) -> None:
        """索引补齐后重启，应直接从盘里恢复就绪状态。"""

        embedder = _FakeEmbedder()
        index = VectorIndex(self.data_dir / "emoji_vectors.json", self.data_dir / "label_cache.json", logger_=LOGGER)
        index.load()
        await index.sync(
            [_record("亲亲", "hash-a")],
            embedder.embed_texts,
            batch_size=8,
            max_items=8,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertTrue(index.ready)

        reloaded = VectorIndex(self.data_dir / "emoji_vectors.json", self.data_dir / "label_cache.json", logger_=LOGGER)
        reloaded.load()
        self.assertTrue(reloaded.ready)
        self.assertEqual(reloaded.size, 1)

    async def test_dimension_mismatch_rejected(self) -> None:
        """查询向量维度与索引不一致时要拒绝，而不是算出错误相似度。"""

        embedder = _FakeEmbedder()
        index = VectorIndex(self.data_dir / "emoji_vectors.json", self.data_dir / "label_cache.json", logger_=LOGGER)
        index.load()
        records = [_record("亲亲", "hash-a")]
        await index.sync(
            records,
            embedder.embed_texts,
            batch_size=8,
            max_items=8,
            max_concurrent=2,
            cache_ttl_seconds=3600,
            cache_size=8,
        )

        async def wrong_dimension(_text: str) -> List[float]:
            return [1.0, 0.0, 0.0]

        hit = await index.query(
            "摸摸头",
            records,
            wrong_dimension,
            threshold=0.1,
            timeout_ms=500,
            cache_ttl_seconds=3600,
            cache_size=8,
        )
        self.assertIsNone(hit)


class LibraryRefreshTest(_BridgeTestBase):
    """表情库刷新时的可观测性。"""

    async def test_empty_library_warns(self) -> None:
        """读到 0 条可用表情时必须告警，不能安静地按 drop 处理。"""

        plugin = _build_plugin(self.data_dir)

        async def empty_query(*_args: Any, **_kwargs: Any) -> list:
            return []

        plugin.ctx.db = SimpleNamespace(query=empty_query)
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            refreshed = await plugin._refresh_library()

        self.assertTrue(refreshed)
        self.assertEqual(plugin._library.size, 0)
        self.assertTrue(any("0 条可用表情" in line for line in captured.output), captured.output)

    async def test_all_rows_filtered_warns(self) -> None:
        """行存在但全被当成脏数据过滤掉时同样要告警。"""

        plugin = _build_plugin(self.data_dir)

        async def dirty_query(*_args: Any, **_kwargs: Any) -> list:
            return [{"image_hash": "a", "description": "[表情包：,<|begin_of_box|>脏描述"}]

        plugin.ctx.db = SimpleNamespace(query=dirty_query)
        with self.assertLogs(LOGGER, level="WARNING") as captured:
            refreshed = await plugin._refresh_library()

        self.assertTrue(refreshed)
        self.assertEqual(plugin._library.size, 0)
        self.assertTrue(any("0 条可用表情" in line for line in captured.output), captured.output)
